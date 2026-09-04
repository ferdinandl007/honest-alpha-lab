"""Research-only multi-sleeve portfolio allocation and execution simulation.

The module accepts real, point-in-time bars and signals.  It intentionally has no
brokerage adapter, order-routing capability, or live-market connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite, sqrt
from statistics import mean
from typing import Mapping, Sequence

from .contracts import ContractError, canonical_hash
from .strategies import StrategyDefinition, StrategySide


@dataclass(frozen=True, slots=True)
class MarketBar:
    day: date
    asset: str
    open: float
    high: float
    low: float
    close: float
    dollar_volume: float
    borrow_available: bool = False

    def __post_init__(self) -> None:
        if not all(isfinite(value) for value in (self.open, self.high, self.low, self.close, self.dollar_volume)):
            raise ContractError("bar prices and dollar volume must be finite")
        if not self.asset or min(self.open, self.high, self.low, self.close) <= 0:
            raise ContractError("bars require an asset and positive OHLC prices")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ContractError("bar high/low must bound open and close")
        if self.dollar_volume < 0:
            raise ContractError("dollar volume cannot be negative")


@dataclass(frozen=True, slots=True)
class PointInTimeSignal:
    strategy_id: str
    asset: str
    score: float
    available_at: datetime
    snapshot_hash: str

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.asset or not self.snapshot_hash:
            raise ContractError("signals require strategy, asset, and immutable snapshot")
        if self.available_at.utcoffset() is None:
            raise ContractError("signal availability must be timezone-aware")
        if not isfinite(self.score):
            raise ContractError("signal score must be finite")


@dataclass(frozen=True, slots=True)
class HistoricalDataset:
    snapshot_hash: str
    point_in_time_verified: bool
    bars: tuple[MarketBar, ...]
    signals: tuple[PointInTimeSignal, ...]

    def __post_init__(self) -> None:
        if not self.snapshot_hash or not self.point_in_time_verified:
            raise ContractError("backtesting requires a verified immutable PIT snapshot")
        if not self.bars or not self.signals:
            raise ContractError("backtesting requires real bars and point-in-time signals")
        if any(signal.snapshot_hash != self.snapshot_hash for signal in self.signals):
            raise ContractError("signals do not match the declared dataset snapshot")


@dataclass(frozen=True, slots=True)
class AllocationPolicy:
    method: str = "minimum_variance"
    max_sleeve_weight: float = 0.20
    covariance_shrinkage: float = 0.25
    iterations: int = 400

    def __post_init__(self) -> None:
        if self.method not in {"minimum_variance", "inverse_volatility", "equal_weight"}:
            raise ContractError("unsupported allocation method")
        if not 0 < self.max_sleeve_weight <= 1:
            raise ContractError("maximum sleeve weight must be in (0, 1]")
        if not 0 <= self.covariance_shrinkage <= 1 or self.iterations <= 0:
            raise ContractError("allocation shrinkage or iterations are invalid")


class TraditionalAllocator:
    """Long-only equal, inverse-volatility, or shrinkage minimum-variance allocation."""

    def __init__(self, policy: AllocationPolicy | None = None) -> None:
        self.policy = policy or AllocationPolicy()

    def allocate(self, sleeve_returns: Mapping[str, Sequence[float]]) -> Mapping[str, float]:
        names = tuple(sorted(sleeve_returns))
        if not names or self.policy.max_sleeve_weight * len(names) < 1:
            raise ContractError("sleeve cap cannot fund a fully invested portfolio")
        rows = [tuple(float(value) for value in sleeve_returns[name]) for name in names]
        if any(not isfinite(value) for row in rows for value in row):
            raise ContractError("allocation returns must be finite")
        if any(len(row) < 2 for row in rows) or len({len(row) for row in rows}) != 1:
            raise ContractError("allocation needs aligned training returns with at least two periods")
        if self.policy.method == "equal_weight":
            weights = _project_capped_simplex([1.0] * len(names), self.policy.max_sleeve_weight)
        elif self.policy.method == "inverse_volatility":
            inverse_vol = [1 / max(_std(row), 1e-8) for row in rows]
            weights = _project_capped_simplex(inverse_vol, self.policy.max_sleeve_weight)
        else:
            covariance = _shrink_covariance(rows, self.policy.covariance_shrinkage)
            weights = _minimum_variance(covariance, self.policy.max_sleeve_weight, self.policy.iterations)
        return dict(zip(names, weights))


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Daily OHLC model; zero cost rates explicitly request frictionless results.

    Signals must precede the decision's UTC date; close-sized limits activate on
    the next observed market date. Missing held-asset bars abort the run. Volume
    limits are ex-post daily capacity approximations, not intraday liquidity.
    Rebalances and expiry count observed market dates; signal age is in calendar
    days. Borrow accrues on prior-close shorts using actual calendar days / 365.
    """

    limit_offset_bps: float = 10.0
    limit_valid_days: int = 1
    stop_loss_fraction: float = 0.08
    take_profit_fraction: float = 0.16
    max_asset_weight: float = 0.04
    long_quantile: float = 0.10
    short_quantile: float = 0.10
    participation_rate: float = 0.05
    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    annual_borrow_rate: float = 0.0

    def __post_init__(self) -> None:
        numeric = (self.limit_offset_bps, self.stop_loss_fraction, self.take_profit_fraction,
                   self.max_asset_weight, self.long_quantile, self.short_quantile,
                   self.participation_rate, self.commission_bps, self.slippage_bps,
                   self.annual_borrow_rate)
        if not all(isfinite(value) for value in numeric):
            raise ContractError("execution settings must be finite")
        if not 0 <= self.limit_offset_bps < 10_000 or not isinstance(self.limit_valid_days, int) or self.limit_valid_days <= 0:
            raise ContractError("limit-order settings are invalid")
        if not 0 < self.stop_loss_fraction < 1 or self.take_profit_fraction <= 0:
            raise ContractError("stop/take-profit settings are invalid")
        if not 0 < self.max_asset_weight <= 1 or not 0 < self.participation_rate <= 1:
            raise ContractError("asset cap or participation rate is invalid")
        if not 0 < self.long_quantile <= 1 or not 0 < self.short_quantile <= 1:
            raise ContractError("selection quantiles are invalid")
        if self.commission_bps < 0 or not 0 <= self.slippage_bps < 10_000 or self.annual_borrow_rate < 0:
            raise ContractError("execution costs must be nonnegative and slippage below 10000 bps")


@dataclass(slots=True)
class _Position:
    quantity: float
    entry_price: float
    exit_reason: str | None = None


@dataclass(slots=True)
class _LimitOrder:
    asset: str
    quantity: float
    limit_price: float
    expires_on_index: int
    source: str


@dataclass(frozen=True, slots=True)
class Fill:
    day: date
    asset: str
    quantity: float
    price: float
    reason: str
    commission: float = 0.0


@dataclass(frozen=True, slots=True)
class BacktestResult:
    dataset_hash: str
    allocation: Mapping[str, float]
    nav_by_day: tuple[tuple[date, float], ...]
    fills: tuple[Fill, ...]
    rejected_orders: tuple[str, ...]
    execution_policy: ExecutionPolicy | None = None
    borrow_cost: float = 0.0


class PortfolioBacktester:
    """Next-bar limits with conservative, explicitly ordered daily execution.

    Carried protective exits take priority over (and cancel) that asset's limits.
    New or increased entries can stop out on their entry bar. An intraday entry takes profit
    that day only if the close proves a subsequent target crossing. Triggered
    exits remain active until liquidated, subject to the shared participation
    cap. Daily bars cannot reconstruct actual intraday order sequencing.
    """

    def __init__(
        self,
        strategies: Mapping[str, StrategyDefinition],
        execution: ExecutionPolicy | None = None,
    ) -> None:
        self.strategies = dict(strategies)
        self.execution = execution or ExecutionPolicy()

    def run(
        self,
        dataset: HistoricalDataset,
        sleeve_allocation: Mapping[str, float],
        initial_cash: float = 1_000_000.0,
    ) -> BacktestResult:
        if not isfinite(initial_cash) or initial_cash <= 0:
            raise ContractError("initial cash must be finite and positive")
        if not sleeve_allocation or set(sleeve_allocation) - set(self.strategies):
            raise ContractError("allocation names an unknown strategy")
        if any(not isfinite(weight) or weight < 0 for weight in sleeve_allocation.values()) or sum(sleeve_allocation.values()) > 1.000001:
            raise ContractError("sleeve allocation must be long-only and sum to at most one")
        bars = _bars_by_day(dataset.bars)
        days = tuple(sorted(bars))
        latest_signal = _latest_signals(dataset.signals, self.strategies)
        cash, positions, orders = initial_cash, {}, []
        fills, rejected, nav_history = [], [], []
        prior_close: dict[str, float] = {}
        # Desired shares stay fixed until that sleeve's own scheduled rebalance.
        sleeve_quantities: dict[str, Mapping[str, float]] = {}
        borrow_cost = 0.0
        for index, day in enumerate(days):
            today = bars[day]
            _nav(cash, positions, today, day)  # Fail before trading with incomplete marks.
            if index:
                charge = sum(-position.quantity * prior_close[asset]
                             for asset, position in positions.items() if position.quantity < 0)
                charge *= self.execution.annual_borrow_rate * (day - days[index - 1]).days / 365
                cash -= charge
                borrow_cost += charge
            used_notional: dict[str, float] = {}
            # Same-bar sale proceeds cannot finance an earlier intraday purchase.
            buying_power = max(0.0, cash)
            cash, stop_fills = self._apply_protective_exits(day, today, positions, cash, used_notional)
            fills.extend(stop_fills)
            blocked = {fill.asset for fill in stop_fills} | {
                asset for asset, position in positions.items() if position.exit_reason
            }
            orders = [order for order in orders if order.asset not in blocked]
            before_entries = {asset: position.quantity for asset, position in positions.items()}
            cash, order_fills, orders = self._fill_orders(
                day, index, today, orders, positions, cash, rejected, used_notional,
                min(buying_power, max(0.0, cash)),
            )
            fills.extend(order_fills)
            new_entries = {
                fill.asset: fill for fill in order_fills
                if fill.asset in positions and (
                    before_entries.get(fill.asset, 0) * positions[fill.asset].quantity <= 0
                    or before_entries.get(fill.asset, 0) * fill.quantity > 0
                )
            }
            cash, entry_exits = self._apply_protective_exits(
                day, today, positions, cash, used_notional, new_entries,
            )
            fills.extend(entry_exits)
            blocked.update(fill.asset for fill in entry_exits)
            blocked.update(asset for asset, position in positions.items() if position.exit_reason)
            orders = [order for order in orders if order.asset not in blocked]
            for strategy_id, quantities in tuple(sleeve_quantities.items()):
                sleeve_quantities[strategy_id] = {asset: quantity for asset, quantity in quantities.items() if asset not in blocked}
            due = [name for name in sorted(sleeve_allocation) if index % self.strategies[name].rebalance_days == 0]
            if due:
                nav = _nav(cash, positions, today, day)
                for name in due:
                    sleeve_quantities[name] = {
                        asset: weight * nav / today[asset].close for asset, weight in self._targets(
                            index, day, latest_signal, {name: sleeve_allocation[name]}, today,
                        ).items() if asset not in blocked
                    }
                targets: dict[str, float] = {}
                for quantities in sleeve_quantities.values():
                    for asset, quantity in quantities.items():
                        targets[asset] = targets.get(asset, 0.0) + quantity
                # The aggregate asset cap is a portfolio risk override, including
                # when price drift raises an inactive sleeve above the cap.
                targets = {
                    asset: max(-self.execution.max_asset_weight * nav / today[asset].close,
                               min(self.execution.max_asset_weight * nav / today[asset].close, quantity))
                    if asset in today else quantity for asset, quantity in targets.items()
                }
                # Replace residual limits using actual holdings; never stack stale deltas.
                orders = self._orders_for_targets(index, targets, today, positions, cash, rejected, target_quantities=True)
            for asset, bar in today.items():
                prior_close[asset] = bar.close
            nav_history.append((day, _nav(cash, positions, today, day)))
        return BacktestResult(canonical_hash(dataset), dict(sleeve_allocation), tuple(nav_history), tuple(fills), tuple(rejected), self.execution, borrow_cost)

    def _targets(self, index: int, day: date, signals, allocation, current_bars) -> Mapping[str, float]:
        targets: dict[str, float] = {}
        for strategy_id, sleeve_weight in allocation.items():
            strategy = self.strategies[strategy_id]
            if index % strategy.rebalance_days:
                continue
            eligible = {}
            for (signal_day, signal_strategy, asset), score in signals.items():
                if (signal_strategy == strategy_id and signal_day < day
                        and (day - signal_day).days <= max(strategy.horizon_days)
                        and asset in current_bars):
                    if asset not in eligible or signal_day > eligible[asset][0]:
                        eligible[asset] = (signal_day, score)
            candidates = [(asset, value[1]) for asset, value in eligible.items()]
            if not candidates or not sleeve_weight:
                continue
            candidates.sort(key=lambda item: (item[1], item[0]))
            quantile = self.execution.short_quantile if strategy.side == StrategySide.SHORT_ONLY else self.execution.long_quantile
            count = max(1, int(len(candidates) * quantile))
            if strategy.side == StrategySide.LONG_SHORT:
                if len(candidates) < 2:
                    continue
                count = min(count, len(candidates) - 1)
                sleeve_weight /= 2  # Each leg shares the sleeve's gross budget.
            if strategy.side != StrategySide.SHORT_ONLY:
                longs = candidates[-count:]
                for asset, _ in longs:
                    targets[asset] = targets.get(asset, 0.0) + sleeve_weight / len(longs)
            if strategy.side == StrategySide.LONG_SHORT:
                short_count = min(len(candidates) - count, max(1, int(len(candidates) * self.execution.short_quantile)))
                shorts = candidates[:short_count]
                for asset, _ in shorts:
                    targets[asset] = targets.get(asset, 0.0) - sleeve_weight / len(shorts)
            elif strategy.side == StrategySide.SHORT_ONLY:
                shorts = candidates[-count:]
                for asset, _ in shorts:
                    targets[asset] = targets.get(asset, 0.0) - sleeve_weight / len(shorts)
        return targets

    def _orders_for_targets(self, index, targets, bars, positions, cash, rejected, *, target_quantities=False):
        nav = _nav(cash, positions, bars)
        if nav <= 0:
            raise ContractError("portfolio NAV is nonpositive; financing/insolvency is not modeled")
        orders = []
        for asset in sorted(set(targets) | set(positions)):
            target_value = targets.get(asset, 0.0)
            bar = bars.get(asset)
            if bar is None:
                rejected.append(f"{asset}: target rejected because current price is missing")
                continue
            if asset in positions and positions[asset].exit_reason:
                continue
            target_quantity = target_value if target_quantities else target_value * nav / bar.close
            current_quantity = positions.get(asset, _Position(0.0, 0.0)).quantity
            quantity = target_quantity - current_quantity
            if abs(quantity) < 1e-12:
                continue
            if target_quantity < min(current_quantity, 0.0) and not bar.borrow_available:
                rejected.append(f"{asset}: short target rejected because borrow was unavailable")
                quantity = -max(current_quantity, 0.0)
                if not quantity:
                    continue
            offset = self.execution.limit_offset_bps / 10_000
            limit = bar.close * (1 - offset if quantity > 0 else 1 + offset)
            orders.append(_LimitOrder(asset, quantity, limit, index + self.execution.limit_valid_days, "rebalance"))
        return orders

    def _fill_orders(self, day, index, bars, orders, positions, cash, rejected,
                     used_notional=None, buying_power=None):
        used_notional = {} if used_notional is None else used_notional
        buying_power = max(0.0, cash) if buying_power is None else buying_power
        retained, fills = [], []
        for order in orders:
            if index > order.expires_on_index:
                rejected.append(f"{order.asset}: limit order expired")
                continue
            bar = bars.get(order.asset)
            if bar is None:
                retained.append(order)
                continue
            eligible = bar.low <= order.limit_price if order.quantity > 0 else bar.high >= order.limit_price
            if not eligible:
                retained.append(order)
                continue
            price = min(bar.open, order.limit_price) if order.quantity > 0 else max(bar.open, order.limit_price)
            slipped = price * (1 + self.execution.slippage_bps / 10_000 * (1 if order.quantity > 0 else -1))
            # A limit cannot fill beyond its price, or outside the observed range.
            price = min(order.limit_price, bar.high, slipped) if order.quantity > 0 else max(order.limit_price, bar.low, slipped)
            maximum = max(0.0, bar.dollar_volume * self.execution.participation_rate - used_notional.get(order.asset, 0.0)) / price
            quantity = max(-maximum, min(maximum, order.quantity))
            existing = positions.get(order.asset)
            prior_quantity = existing.quantity if existing else 0.0
            borrow_rejected = quantity < 0 and prior_quantity + quantity < min(prior_quantity, 0.0) and not bar.borrow_available
            if borrow_rejected:
                rejected.append(f"{order.asset}: short fill rejected because borrow was unavailable")
                quantity = -min(max(prior_quantity, 0.0), abs(quantity))
            if quantity > 0:
                affordable = max(0.0, min(cash, buying_power)) / (price * (1 + self.execution.commission_bps / 10_000))
                if quantity > affordable:
                    rejected.append(f"{order.asset}: buy limited by available cash including commission")
                    quantity = affordable
            if abs(quantity) < 1e-12:
                if not borrow_rejected:
                    retained.append(order)
                continue
            commission = abs(quantity * price) * self.execution.commission_bps / 10_000
            cash -= quantity * price + commission
            buying_power = max(0.0, buying_power - max(quantity * price, 0.0) - commission)
            used_notional[order.asset] = used_notional.get(order.asset, 0.0) + abs(quantity * price)
            new_quantity = prior_quantity + quantity
            if abs(new_quantity) < 1e-12:
                positions.pop(order.asset, None)
            else:
                if not existing or prior_quantity * new_quantity < 0:
                    entry = price
                elif prior_quantity * quantity > 0:
                    entry = (abs(prior_quantity) * existing.entry_price + abs(quantity) * price) / abs(new_quantity)
                else:
                    entry = existing.entry_price  # Reductions preserve the remaining lot's basis.
                positions[order.asset] = _Position(new_quantity, entry)
            fills.append(Fill(day, order.asset, quantity, price, order.source, commission))
            remaining = order.quantity - quantity
            if abs(remaining) > 1e-12 and not borrow_rejected:
                retained.append(_LimitOrder(order.asset, remaining, order.limit_price, order.expires_on_index, order.source))
        # Validity includes this bar, but its residual is dead at today's close,
        # even if the dataset ends here or the asset had no bar today.
        for order in retained:
            if index >= order.expires_on_index:
                rejected.append(f"{order.asset}: limit order expired")
        retained = [order for order in retained if index < order.expires_on_index]
        return cash, fills, retained

    def _apply_protective_exits(self, day, bars, positions, cash, used_notional=None, entry_fills=None):
        used_notional = {} if used_notional is None else used_notional
        fills = []
        for asset, position in tuple(positions.items()):
            if entry_fills is not None and asset not in entry_fills:
                continue
            bar = bars.get(asset)
            if bar is None:
                raise ContractError(f"{day}: missing price for held asset {asset}; no exit can be simulated")
            entry_fill = entry_fills.get(asset) if entry_fills is not None else None
            # For a limit reached intraday, the high/low on the profitable side
            # might precede entry. The close can prove a later crossing.
            intraday_entry = entry_fill is not None and (
                (position.quantity > 0 and entry_fill.price < bar.open)
                or (position.quantity < 0 and entry_fill.price > bar.open)
            )
            reason, price = None, None
            if position.quantity > 0:
                stop, target = position.entry_price * (1 - self.execution.stop_loss_fraction), position.entry_price * (1 + self.execution.take_profit_fraction)
                if position.exit_reason:
                    reason, price = position.exit_reason, bar.open
                elif entry_fill is None and bar.open >= target:
                    reason, price = "take_profit", bar.open
                elif bar.low <= stop:
                    reason, price = "stop_loss", stop if entry_fill else min(bar.open, stop)
                elif (bar.close if intraday_entry else bar.high) >= target:
                    reason, price = "take_profit", target if entry_fill else max(bar.open, target)
            else:
                stop, target = position.entry_price * (1 + self.execution.stop_loss_fraction), position.entry_price * (1 - self.execution.take_profit_fraction)
                if position.exit_reason:
                    reason, price = position.exit_reason, bar.open
                elif entry_fill is None and target > 0 and bar.open <= target:
                    reason, price = "take_profit", bar.open
                elif bar.high >= stop:
                    reason, price = "stop_loss", stop if entry_fill else max(bar.open, stop)
                elif target > 0 and (bar.close if intraday_entry else bar.low) <= target:
                    reason, price = "take_profit", target if entry_fill else min(bar.open, target)
            if reason:
                position.exit_reason = reason
                direction = -1 if position.quantity > 0 else 1
                price *= 1 + direction * self.execution.slippage_bps / 10_000
                price = min(bar.high, max(bar.low, price))
                capacity = max(0.0, bar.dollar_volume * self.execution.participation_rate - used_notional.get(asset, 0.0))
                quantity = direction * min(abs(position.quantity), capacity / price)
                if abs(quantity) < 1e-12:
                    continue
                commission = abs(quantity * price) * self.execution.commission_bps / 10_000
                cash -= quantity * price + commission
                used_notional[asset] = used_notional.get(asset, 0.0) + abs(quantity * price)
                position.quantity += quantity
                if abs(position.quantity) < 1e-12:
                    positions.pop(asset)
                fills.append(Fill(day, asset, quantity, price, reason, commission))
        return cash, fills


def _nav(cash, positions, bars, day=None) -> float:
    missing = sorted(set(positions) - set(bars))
    if missing:
        raise ContractError(f"{day or 'valuation'}: missing price for held asset(s) {', '.join(missing)}; "
                            "NAV requires explicit bars/delisting data; stale marks and fictional fills are forbidden")
    value = cash + sum(position.quantity * bars[asset].close for asset, position in positions.items())
    if not isfinite(value):
        raise ContractError("portfolio NAV must be finite")
    return value


def _bars_by_day(bars: Sequence[MarketBar]) -> Mapping[date, Mapping[str, MarketBar]]:
    grouped: dict[date, dict[str, MarketBar]] = {}
    for bar in bars:
        if bar.asset in grouped.setdefault(bar.day, {}):
            raise ContractError("dataset has duplicate bar keys")
        grouped[bar.day][bar.asset] = bar
    return grouped


def _latest_signals(signals: Sequence[PointInTimeSignal], strategies: Mapping[str, StrategyDefinition]):
    latest = {}
    seen = {}
    for signal in signals:
        if signal.strategy_id not in strategies:
            raise ContractError("dataset signal names an unknown strategy")
        key = (signal.available_at.astimezone(timezone.utc).date(), signal.strategy_id, signal.asset)
        timestamp_key = (signal.available_at, signal.strategy_id, signal.asset)
        if timestamp_key in seen and signal.score != seen[timestamp_key]:
            raise ContractError("conflicting scores at the same strategy/asset availability timestamp")
        seen[timestamp_key] = signal.score
        previous = latest.get(key)
        if previous is None or signal.available_at > previous[0]:
            latest[key] = (signal.available_at, signal.score)
    return {key: value[1] for key, value in latest.items()}


def _std(values: Sequence[float]) -> float:
    average = mean(values)
    return sqrt(sum((value - average) ** 2 for value in values) / max(len(values) - 1, 1))


def _shrink_covariance(rows: Sequence[Sequence[float]], shrinkage: float) -> list[list[float]]:
    means = [mean(row) for row in rows]
    denominator = max(len(rows[0]) - 1, 1)
    matrix = [[sum((a - means[i]) * (b - means[j]) for a, b in zip(rows[i], rows[j])) / denominator for j in range(len(rows))] for i in range(len(rows))]
    return [[(1 - shrinkage) * matrix[i][j] + (shrinkage * matrix[i][i] if i == j else 0.0) for j in range(len(rows))] for i in range(len(rows))]


def _minimum_variance(covariance: Sequence[Sequence[float]], cap: float, iterations: int) -> list[float]:
    size = len(covariance)
    weights = [1 / size] * size
    scale = max(sum(abs(value) for row in covariance for value in row), 1e-8)
    for _ in range(iterations):
        gradient = [2 * sum(covariance[i][j] * weights[j] for j in range(size)) for i in range(size)]
        weights = _project_capped_simplex([weight - gradient[i] / scale for i, weight in enumerate(weights)], cap)
    return weights


def _project_capped_simplex(values: Sequence[float], cap: float) -> list[float]:
    lower, upper = min(values) - cap, max(values)
    for _ in range(80):
        threshold = (lower + upper) / 2
        total = sum(min(cap, max(0.0, value - threshold)) for value in values)
        if total > 1:
            lower = threshold
        else:
            upper = threshold
    return [min(cap, max(0.0, value - upper)) for value in values]
