"""Deterministic synthetic accounting cases, not evidence of investment returns."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from itertools import permutations

import pytest

from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.portfolio import (
    AllocationPolicy, ExecutionPolicy, HistoricalDataset, MarketBar,
    PointInTimeSignal, PortfolioBacktester, TraditionalAllocator,
    _LimitOrder, _Position, _latest_signals,
)
from honest_alpha_lab.strategies import StrategyDefinition, StrategyFamily, StrategySide


SNAPSHOT = "synthetic-integrity-fixture"
UTC = timezone.utc


def day(number):
    return date(2020, 1, 1) + timedelta(days=number - 1)


def bar(number, asset="A", *, open=100, high=101, low=99, close=100,
        volume=1_000_000, borrow=True):
    return MarketBar(day(number), asset, open, high, low, close, volume, borrow)


def signal(number=0, asset="A", score=1, strategy="s", *, available_at=None):
    timestamp = available_at or datetime.combine(day(number), datetime.min.time(), UTC).replace(hour=16)
    return PointInTimeSignal(strategy, asset, score, timestamp, SNAPSHOT)


def strategy(name="s", *, interval=1, side=StrategySide.LONG_ONLY, horizon=60):
    return StrategyDefinition(name, name, StrategyFamily.EVENT, "synthetic fixture",
                              ("score",), (horizon,), side, interval)


def simulator(*, strategies=None, **policy):
    return PortfolioBacktester(strategies or {"s": strategy()}, ExecutionPolicy(
        **{"limit_offset_bps": 0, "max_asset_weight": 1, "participation_rate": 1, **policy},
    ))


def run(bars, signals=None, *, sim=None, allocation=None, cash=1000):
    return (sim or simulator()).run(
        HistoricalDataset(SNAPSHOT, True, tuple(bars), tuple(signals or [signal()])),
        allocation or {"s": 1}, initial_cash=cash,
    )


@pytest.mark.parametrize("side", [StrategySide.LONG_ONLY, StrategySide.SHORT_ONLY])
def test_missing_held_price_aborts_instead_of_dropping_asset_from_nav(side):
    sim = simulator(strategies={"s": strategy(side=side)})
    with pytest.raises(ContractError, match="2020-01-03: missing price.*A"):
        run([bar(1), bar(2), bar(3, "B")], sim=sim)


def test_target_sizing_also_refuses_missing_held_price():
    with pytest.raises(ContractError, match="missing price.*A"):
        simulator()._orders_for_targets(0, {}, {"B": bar(1, "B")},
                                        {"A": _Position(1, 100)}, 900, [])


def test_missing_unheld_price_does_not_create_a_fill():
    result = run([bar(1), bar(2, "B"), bar(3, "B")])
    assert result.fills == ()
    assert [value for _, value in result.nav_by_day] == [1000, 1000, 1000]


def test_latest_eligible_score_per_asset_drives_selection_regardless_of_input_order():
    signals = [signal(0, "A", 100), signal(1, "A", -10), signal(0, "B", 5)]
    bars = [bar(d, asset) for d in (3, 4) for asset in ("A", "B")]
    for ordered in permutations(signals):
        result = run(bars, ordered)
        assert [(fill.asset, fill.quantity) for fill in result.fills] == [("B", 10)]


def test_dated_duplicates_do_not_multiply_an_assets_weight():
    result = run([bar(d, asset) for d in (3, 4) for asset in ("A", "B")],
                 [signal(0, "A", 1), signal(1, "A", 2), signal(0, "B", 3)],
                 sim=simulator(long_quantile=1))
    assert [(fill.asset, fill.quantity) for fill in result.fills] == [("A", 5), ("B", 5)]


def test_intraday_latest_score_is_selected_before_cross_date_selection():
    older = signal(0, score=100)
    newer = replace(older, score=-1, available_at=older.available_at + timedelta(hours=1))
    result = run([bar(d, asset) for d in (1, 2) for asset in ("A", "B")],
                 [newer, older, signal(0, "B", 2)])
    assert [fill.asset for fill in result.fills] == ["B"]


def test_conflicting_simultaneous_scores_fail_even_if_superseded():
    first = signal(0)
    conflict = replace(first, score=2)
    newer = replace(first, available_at=first.available_at + timedelta(hours=1))
    for ordered in permutations([first, conflict, newer]):
        with pytest.raises(ContractError, match="conflicting scores"):
            _latest_signals(ordered, {"s": strategy()})


def test_signal_dates_use_utc_and_equivalent_instants_have_identical_fills():
    local = datetime(2020, 1, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    bars = [bar(d) for d in range(1, 5)]
    left = run(bars, [signal(available_at=local)])
    right = run(bars, [signal(available_at=local.astimezone(UTC))])
    assert left.fills == right.fills
    assert left.fills[0].day == day(4)


def test_same_day_and_future_signals_cannot_affect_next_bar_order_early():
    bars = [bar(d, asset) for d in range(1, 4) for asset in ("A", "B")]
    result = run(bars, [signal(1), signal(3, "B", 100)])
    assert [(fill.day, fill.asset) for fill in result.fills] == [(day(3), "A")]


def test_limits_are_next_bar_and_do_not_use_execution_day_close_for_sizing():
    bars = [bar(1), bar(2, open=95, high=110, low=94, close=109)]
    result = run(bars, sim=simulator(stop_loss_fraction=0.5, take_profit_fraction=1),
                 allocation={"s": 0.5})
    assert [(fill.day, fill.quantity, fill.price) for fill in result.fills] == [(day(2), 5, 95)]
    assert result.nav_by_day[-1][1] == 1070


def test_deselected_holdings_are_liquidated_at_next_bar():
    result = run([bar(d, asset) for d in range(1, 4) for asset in ("A", "B")],
                 [signal(0, "A", 10), signal(0, "B", 1), signal(1, "B", 20)],
                 allocation={"s": 0.5})
    assert [(fill.day, fill.asset, fill.quantity) for fill in result.fills] == [
        (day(2), "A", 5), (day(3), "A", -5), (day(3), "B", 5),
    ]


def test_expired_signal_flattens_holdings_on_rebalance():
    result = run([bar(d) for d in range(1, 4)],
                 sim=simulator(strategies={"s": strategy(horizon=1)}))
    assert [(fill.day, fill.quantity) for fill in result.fills] == [(day(2), 10), (day(3), -10)]


def test_sleeves_keep_their_targets_between_their_own_rebalance_dates():
    sim = simulator(strategies={"slow": strategy("slow", interval=2), "fast": strategy("fast")})
    result = run([bar(d, asset) for d in range(1, 5) for asset in ("A", "B", "C")],
                 [signal(0, "A", 1, "slow"), signal(1, "B", 2, "slow"), signal(0, "C", 1, "fast")],
                 sim=sim, allocation={"slow": 0.4, "fast": 0.4})
    a_fills = [fill for fill in result.fills if fill.asset == "A"]
    assert [(fill.day, fill.quantity) for fill in a_fills] == [(day(2), 4), (day(4), -4)]
    assert not [fill for fill in result.fills if fill.day == day(3)]


def test_inactive_sleeve_is_not_resized_by_another_sleeves_rebalance_after_price_drift():
    sim = simulator(strategies={"slow": strategy("slow", interval=5), "fast": strategy("fast")},
                    take_profit_fraction=1)
    bars = [bar(1, "A"), bar(1, "B"), bar(2, "A", high=110, close=110), bar(2, "B"),
            bar(3, "A", open=110, high=111, low=109, close=110), bar(3, "B")]
    result = run(bars, [signal(0, "A", 1, "slow"), signal(0, "B", 1, "fast")],
                 sim=sim, allocation={"slow": 0.4, "fast": 0.4})
    assert [(fill.day, fill.quantity) for fill in result.fills if fill.asset == "A"] == [(day(2), 4)]
    assert [fill.quantity for fill in result.fills if fill.asset == "B"] == pytest.approx([4, 0.16])


def test_rebalance_replaces_partial_orders_and_shared_volume_is_capped():
    result = run([bar(d, volume=100) for d in range(1, 7)],
                 sim=simulator(limit_valid_days=10), allocation={"s": 0.4})
    assert sum(fill.quantity for fill in result.fills) == 4
    assert [(fill.day, fill.quantity) for fill in result.fills] == [(day(d), 1) for d in range(2, 6)]


def test_order_expiry_advances_even_when_asset_bar_is_missing():
    rejected = []
    _, fills, remaining = simulator()._fill_orders(day(3), 2, {},
        [_LimitOrder("A", 1, 100, 1, "rebalance")], {}, 1000, rejected)
    assert fills == remaining == []
    assert rejected == ["A: limit order expired"]


@pytest.mark.parametrize("bars", [{}, {"A": bar(2, open=105, high=106, low=104, close=105)}])
def test_unfilled_orders_expire_at_the_close_of_the_last_valid_bar(bars):
    rejected = []
    _, fills, remaining = simulator()._fill_orders(day(2), 1, bars,
        [_LimitOrder("A", 1, 100, 1, "rebalance")], {}, 1000, rejected)
    assert fills == remaining == []
    assert rejected == ["A: limit order expired"]


@pytest.mark.parametrize("side,open,high,low,close", [
    (StrategySide.LONG_ONLY, 80, 85, 75, 82),
    (StrategySide.SHORT_ONLY, 120, 125, 115, 122),
])
def test_stop_gaps_fill_at_observed_open_not_the_untraded_stop(side, open, high, low, close):
    result = run([bar(1), bar(2), bar(3, open=open, high=high, low=low, close=close)],
                 sim=simulator(strategies={"s": strategy(side=side)}))
    exits = [fill for fill in result.fills if fill.reason == "stop_loss"]
    assert len(exits) == 1
    assert exits[0].price == open
    assert result.nav_by_day[-1][1] == 800


@pytest.mark.parametrize("side,high,low", [
    (StrategySide.LONG_ONLY, 120, 90), (StrategySide.SHORT_ONLY, 110, 80),
])
def test_new_positions_can_stop_out_on_entry_day_with_stop_precedence(side, high, low):
    result = run([bar(1), bar(2, high=high, low=low)],
                 sim=simulator(strategies={"s": strategy(side=side)}))
    assert [fill.reason for fill in result.fills] == ["rebalance", "stop_loss"]
    assert result.fills[0].day == result.fills[1].day == day(2)
    assert result.nav_by_day[-1][1] == pytest.approx(920)


def test_intraday_entry_does_not_claim_a_target_that_could_precede_entry():
    result = run([bar(1), bar(2, open=100, high=120, low=95, close=100)],
                 sim=simulator(limit_offset_bps=500, take_profit_fraction=0.1))
    assert [fill.reason for fill in result.fills] == ["rebalance"]
    assert result.fills[0].price == 95


def test_intraday_entry_can_take_profit_when_close_proves_a_later_crossing():
    result = run([bar(1), bar(2, open=100, high=120, low=95, close=110)],
                 sim=simulator(limit_offset_bps=500, take_profit_fraction=0.1))
    assert [fill.reason for fill in result.fills] == ["rebalance", "take_profit"]
    assert result.fills[-1].price == pytest.approx(104.5)


def test_open_target_precedes_later_intraday_stop():
    result = run([bar(1), bar(2), bar(3, open=120, high=121, low=90, close=100)])
    assert result.fills[-1].reason == "take_profit"
    assert result.fills[-1].price == 120


def test_stop_cancels_partial_entry_residual_instead_of_reopening():
    # The residual buy at 100 is not marketable at the open; intraday stop
    # priority must still cancel it before another intraday entry can occur.
    result = run([bar(1), bar(2, volume=100), bar(3, open=105, high=106, low=90)],
                 sim=simulator(strategies={"s": strategy(interval=5)}, limit_valid_days=5))
    assert [(fill.day, fill.reason, fill.quantity) for fill in result.fills] == [
        (day(2), "rebalance", 1), (day(3), "stop_loss", -1),
    ]


def test_zero_volume_stop_has_no_fictional_fill_and_remains_pending():
    result = run([bar(1), bar(2), bar(3, low=90, volume=0), bar(4)])
    assert [(fill.day, fill.reason) for fill in result.fills] == [
        (day(2), "rebalance"), (day(4), "stop_loss"),
    ]
    assert result.nav_by_day[2][1] == 1000
    assert result.fills[-1].price == 100


def test_entry_and_protection_share_daily_capacity_then_exit_at_next_open():
    result = run([bar(1), bar(2, low=90, volume=100), bar(3)],
                 sim=simulator(strategies={"s": strategy(interval=5)}))
    assert [(fill.day, fill.quantity, fill.reason) for fill in result.fills] == [
        (day(2), 1, "rebalance"), (day(3), -1, "stop_loss"),
    ]


def test_increased_position_uses_new_basis_for_same_day_protection():
    result = run([bar(1), bar(2, high=110, close=110, volume=200),
                  bar(3, open=110, high=111, low=95, close=100)])
    assert [(fill.day, fill.reason) for fill in result.fills] == [
        (day(2), "rebalance"), (day(3), "rebalance"), (day(3), "stop_loss"),
    ]
    assert result.nav_by_day[-1][1] == pytest.approx(920)


@pytest.mark.parametrize("quantity,trade", [(10, -4), (-10, 4)])
def test_partial_reductions_preserve_cost_basis(quantity, trade):
    positions = {"A": _Position(quantity, 90)}
    simulator()._fill_orders(day(2), 1, {"A": bar(2)},
        [_LimitOrder("A", trade, 100, 1, "rebalance")], positions, 1000, [])
    assert positions["A"].quantity == quantity + trade
    assert positions["A"].entry_price == 90


def test_position_reversal_resets_basis_to_new_entry_price():
    positions = {"A": _Position(2, 90)}
    simulator()._fill_orders(day(2), 1, {"A": bar(2)},
        [_LimitOrder("A", -4, 100, 1, "rebalance")], positions, 1000, [])
    assert positions["A"].quantity == -2
    assert positions["A"].entry_price == 100


def test_borrow_is_rechecked_on_fill_day():
    result = run([bar(1), bar(2, borrow=False)],
                 sim=simulator(strategies={"s": strategy(side=StrategySide.SHORT_ONLY)}))
    assert result.fills == ()
    assert any("short fill rejected" in message for message in result.rejected_orders)


def test_lost_borrow_does_not_prevent_closing_long_side_of_reversal():
    positions = {"A": _Position(2, 100)}
    rejected = []
    _, fills, residual = simulator()._fill_orders(day(2), 1, {"A": bar(2, borrow=False)},
        [_LimitOrder("A", -4, 100, 1, "rebalance")], positions, 1000, rejected)
    assert fills[0].quantity == -2
    assert positions == {}
    assert residual == []


def test_short_quantile_and_long_short_gross_budget_are_respected():
    bars = [bar(d, asset) for d in (1, 2) for asset in ("A", "B", "C", "D")]
    signals = [signal(0, asset, i) for i, asset in enumerate(("A", "B", "C", "D"))]
    shorts = run(bars, signals, sim=simulator(short_quantile=0.5,
        strategies={"s": strategy(side=StrategySide.SHORT_ONLY)}))
    assert [(fill.asset, fill.quantity) for fill in shorts.fills] == [("C", -5), ("D", -5)]
    both = run(bars, signals, sim=simulator(long_quantile=1, short_quantile=1,
        strategies={"s": strategy(side=StrategySide.LONG_SHORT)}))
    assert sum(abs(fill.quantity * fill.price) for fill in both.fills) == pytest.approx(1000)
    assert len({fill.asset for fill in both.fills}) == len(both.fills)


def test_commissions_are_charged_on_entry_and_exit_and_nav_reconciles():
    result = run([bar(1), bar(2), bar(3, low=90)],
                 sim=simulator(commission_bps=100), allocation={"s": 0.5})
    # Entry costs resize the next target: the opening reduction of .025 shares
    # precedes the later stop on the remaining 4.975 shares.
    assert [fill.commission for fill in result.fills] == pytest.approx([5, .025, 4.577])
    assert result.nav_by_day[-1][1] == pytest.approx(950.598)
    assert result.execution_policy.commission_bps == 100


def test_buy_quantity_is_cash_limited_including_commission():
    result = run([bar(1), bar(2)], sim=simulator(commission_bps=100))
    fill = result.fills[0]
    assert fill.quantity == pytest.approx(1000 / 101)
    assert fill.quantity * fill.price + fill.commission == pytest.approx(1000)
    assert result.nav_by_day[-1][1] == pytest.approx(1000 - fill.commission)


def test_same_bar_sale_cannot_fund_a_purchase_with_unknown_intraday_order():
    result = run([bar(d, asset) for d in range(1, 5) for asset in ("A", "B")],
                 [signal(0, "A", 10), signal(0, "B", 1), signal(1, "B", 20)])
    assert [(fill.day, fill.asset, fill.quantity) for fill in result.fills] == [
        (day(2), "A", 10), (day(3), "A", -10), (day(4), "B", 10),
    ]


def test_slippage_is_adverse_but_never_breaks_a_limit():
    result = run([bar(1), bar(2, open=95, high=101, low=94)],
                 sim=simulator(slippage_bps=100), allocation={"s": 0.5})
    assert result.fills[0].price == pytest.approx(95.95)
    capped = run([bar(1), bar(2)], sim=simulator(slippage_bps=100))
    assert capped.fills[0].price == 100


def test_borrow_cost_uses_carried_short_and_elapsed_calendar_days():
    result = run([bar(1), bar(2), bar(5)],
                 sim=simulator(annual_borrow_rate=0.365,
                               strategies={"s": strategy(side=StrategySide.SHORT_ONLY, interval=10)}))
    assert result.borrow_cost == pytest.approx(3)
    assert result.nav_by_day[-1][1] == pytest.approx(997)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_data_and_cash_are_rejected(value):
    with pytest.raises(ContractError, match="finite"):
        bar(1, close=value)
    with pytest.raises(ContractError, match="finite"):
        signal(score=value)
    with pytest.raises(ContractError, match="finite"):
        run([bar(1)], cash=value)
    with pytest.raises(ContractError):
        run([bar(1)], allocation={"s": value})
    with pytest.raises(ContractError, match="finite"):
        TraditionalAllocator(AllocationPolicy(max_sleeve_weight=1)).allocate({"s": [0, value]})


@pytest.mark.parametrize("settings", [
    {"limit_offset_bps": 10_000}, {"limit_valid_days": 1.5},
    {"commission_bps": -1}, {"slippage_bps": 10_000}, {"annual_borrow_rate": -0.1},
    {"commission_bps": float("nan")}, {"take_profit_fraction": float("inf")},
])
def test_invalid_execution_settings_are_rejected(settings):
    with pytest.raises(ContractError):
        simulator(**settings)


@pytest.mark.parametrize("side,opening,high,low,close,expected", [
    (StrategySide.LONG_ONLY, 105, 120, 100, 110, 1025),
    (StrategySide.SHORT_ONLY, 95, 100, 80, 90, 1025),
])
def test_opening_liquidation_precedes_later_profit_target(side, opening, high, low, close, expected):
    result = run(
        [bar(1), bar(2), bar(3, open=opening, high=high, low=low, close=close)],
        sim=simulator(strategies={"s": strategy(side=side, horizon=1)}),
        allocation={"s": .5},
    )
    assert [fill.reason for fill in result.fills] == ["rebalance", "rebalance"]
    assert result.fills[-1].price == opening
    assert result.fills[-1].quantity == -result.fills[0].quantity
    assert result.nav_by_day[-1][1] == expected


def test_opening_partial_reduction_leaves_only_remainder_for_intraday_stop():
    sim = simulator()
    positions = {"A": _Position(10, 100)}
    bars = {"A": bar(3, open=105, high=106, low=90, close=100, volume=2000)}
    used = {}
    cash, opening, remaining = sim._fill_orders(day(3), 2, bars,
        [_LimitOrder("A", -4, 100, 2, "rebalance")], positions, 0, [], used, open_only=True)
    cash, exits = sim._apply_protective_exits(day(3), bars, positions, cash, used)
    assert remaining == []
    assert [(f.quantity, f.price) for f in opening + exits] == [(-4, 105), (-6, 92)]
    assert positions == {}
    assert cash == 972
    assert used["A"] == 972


@pytest.mark.parametrize("interval", [1, 10])
@pytest.mark.parametrize("price,volume", [(200, 1e6), (250, 1e6), (250, 0)])
def test_insolvency_fails_on_every_day_independent_of_rebalance(interval, price, volume):
    with pytest.raises(ContractError, match="NAV is nonpositive"):
        run([bar(1), bar(2), bar(3, open=price, high=price, low=price, close=price, volume=volume)],
            sim=simulator(strategies={"s": strategy(side=StrategySide.SHORT_ONLY, interval=interval)}))


def test_opening_residual_entry_precedes_later_stop_with_shared_capacity():
    result = run([bar(1), bar(2, volume=100), bar(3, low=90, volume=1000), bar(4)],
                 sim=simulator(strategies={"s": strategy(interval=5)}, limit_valid_days=5))
    third_day = [fill for fill in result.fills if fill.day == day(3)]
    assert third_day[0].reason == "rebalance"
    assert third_day[0].quantity == 9
    assert third_day[1].reason == "stop_loss"
    assert sum(abs(fill.quantity * fill.price) for fill in third_day) == pytest.approx(1000)
    assert result.fills[-1].day == day(4)
    assert result.fills[-1].reason == "stop_loss"
    assert sum(fill.quantity for fill in result.fills) == pytest.approx(0)


def test_opening_and_intraday_buys_share_cash_without_sale_proceeds():
    result = run([bar(1, "A"), bar(1, "B"), bar(2, "A"),
                  bar(2, "B", open=105, high=106, low=99, close=100)],
                 [signal(0, "A", 1), signal(0, "B", 1)],
                 sim=simulator(long_quantile=1, commission_bps=100))
    assert sum(fill.quantity * fill.price + fill.commission for fill in result.fills) == pytest.approx(1000)
    assert result.nav_by_day[-1][1] == pytest.approx(1000 / 1.01)


def test_paper_ingest_rolls_back_insolvent_nonrebalance_day(tmp_path):
    from honest_alpha_lab import paper_trading as paper
    from test_paper_trading import make_paper_config, signal_event, bar_event

    config = make_paper_config()
    config["strategies"]["s"].update(side="short_only", rebalance_days=10)
    config["execution_policy"].update(commission_bps=0, slippage_bps=0, annual_borrow_rate=0)
    path = tmp_path / "insolvency.sqlite"
    paper.create_book(path, "b", config)
    paper.ingest_book(path, "b", signal_event())
    paper.ingest_book(path, "b", bar_event(2))
    before = paper.ingest_book(path, "b", bar_event(3))
    with pytest.raises(ContractError, match="NAV is nonpositive"):
        paper.ingest_book(path, "b", bar_event(4, open=250, high=250, low=250, close=250))
    assert paper.book_status(path, "b") == before
