"""Standalone research workflow; no prediction generation or market-data synthesis.

``run_portfolio_workflow(request, artifact_store=None)`` returns JSON-compatible
results. Required keys: bars_csv, signals_csv, training_returns_csv, strategy_ids,
development_start, development_end, test_start, test_end, execution_policy.
Also required: scope="development". Dates are inclusive ISO dates,
development_end < test_start; test denotes an internal development holdout only.
Custom ``strategies`` maps IDs to StrategyDefinition fields (strategy_id optional
inside each definition); strategy_ids may be omitted to use all custom entries.
Optional input_hashes maps bars, signals, training_returns to expected SHA256s;
when supplied all three are required and checked against consumed bytes.
Dates are inclusive ISO dates, development_end < test_start. Execution policy
must explicitly set commission_bps, slippage_bps, annual_borrow_rate (zero allowed).
Optional: initial_cash (1e6), allocation_policy (max_sleeve_weight defaults to 1,
covariance_shrinkage, iterations), output_directory, signal_handoff_hashes.

CSV schemas:
* bars: day,asset,open,high,low,close,dollar_volume,borrow_available (true/false)
* signals: strategy_id,asset,score,available_at,snapshot_hash
* training: day,strategy_id,return,available_at

Training day is the END of the realized return interval. Availability must be
timezone-aware, on/after that date and strictly before test_start UTC midnight.
Every sleeve must have exactly the same >=2 development dates. Training rows
outside development are rejected, not silently filtered. Allocations are frozen
before test; all three policies are reported without selecting a test winner.
Signals must have aware availability times; their source snapshot labels are
preserved as lineage, never interpreted as verification. The engine conservatively
uses signals dated before a decision day and executes limits on the next bar.
Test starts in cash, with no development holdings carried into it.

Actual input bytes, request, and complete results are stored content-addressed
when a store or output_directory is supplied. Handoff hashes are caller-declared
lineage, not a substitute for hashes of the CSV bytes actually consumed.
CLI: python -m honest_alpha_lab.portfolio_workflow --request request.json
     --output-directory artifacts
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, date, datetime
from math import isfinite
from pathlib import Path

from .artifacts import LocalArtifactStore
from .contracts import ContractError, canonical_hash
from .portfolio import (
    AllocationPolicy,
    ExecutionPolicy,
    HistoricalDataset,
    MarketBar,
    PointInTimeSignal,
    PortfolioBacktester,
    TraditionalAllocator,
)
from .strategies import StrategyDefinition, StrategyFamily, StrategySide, research_strategy_catalog


def portfolio_engine_identity():
    directory = Path(__file__).resolve().parent
    return {"python": sys.version, "source_hashes": {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("portfolio_workflow.py", "portfolio.py", "strategies.py", "contracts.py")}}


def _date(value):
    return date.fromisoformat(value)


def _timestamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ContractError("availability timestamps must be timezone-aware")
    return stamp.astimezone(UTC)


def _rows(content, columns):
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or not set(columns) <= set(headers):
        raise ContractError(f"CSV requires unique headers including {','.join(columns)}")
    rows = list(reader)
    if not rows:
        raise ContractError("CSV input is empty")
    for row in rows:
        if None in row or any(not isinstance(row.get(key), str) or not row[key].strip() for key in columns):
            raise ContractError("CSV has missing required values or malformed rows")
    return rows


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, default=lambda item: item.isoformat(),
                      separators=(",", ":")).encode()


def _metrics(result, initial_cash):
    peak = previous = initial_cash
    turnover = 0.0
    series = []
    for day, nav in result.nav_by_day:
        if nav <= 0:
            raise ContractError("nonpositive NAV: performance ratios are undefined")
        fills = [fill for fill in result.fills if fill.day == day]
        notional = sum(abs(fill.quantity * fill.price) for fill in fills)
        daily_turnover = notional / previous
        turnover += daily_turnover
        peak = max(peak, nav)
        series.append({"day": day.isoformat(), "nav": nav, "return": nav / previous - 1,
                       "drawdown": nav / peak - 1, "gross_turnover": daily_turnover,
                       "commission": sum(fill.commission for fill in fills)})
        previous = nav
    commissions = sum(fill.commission for fill in result.fills)
    return {"initial_cash": initial_cash, "final_nav": previous,
            "total_return": previous / initial_cash - 1,
            "max_drawdown": -min(item["drawdown"] for item in series),
            "gross_turnover": turnover, "commission_cost": commissions,
            "borrow_cost": result.borrow_cost,
            "explicit_cash_cost": commissions + result.borrow_cost,
            "slippage_cost": None,
            "cost_note": "Slippage is embedded in fill prices; engine does not separately attribute it. "
                         "Turnover is gross traded notional / prior NAV, not half-turnover. "
                         "Final holdings are marked, not forcibly liquidated.",
            "daily": series}


def run_portfolio_workflow(request: Mapping, artifact_store=None) -> dict:
    """Execute all three pre-test allocations; raise ContractError on invalid input.

    An optional store implements put(bytes)->sha256. The returned
    artifacts.results hash resolves to the complete report except its own
    self-referential artifacts field. No files are published until validation
    and all simulations have succeeded.
    """
    try:
        return _run(request, artifact_store)
    except ContractError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ContractError(f"invalid portfolio workflow input: {exc}") from exc


def _run(request, artifact_store):
    if not isinstance(request, Mapping):
        raise ContractError("request must be a mapping")
    required = {"bars_csv", "signals_csv", "training_returns_csv", "scope",
                "development_start", "development_end", "test_start", "test_end", "execution_policy"}
    optional = {"initial_cash", "allocation_policy", "output_directory", "signal_handoff_hashes",
                "strategies", "strategy_ids", "input_hashes"}
    if required - request.keys() or request.keys() - required - optional:
        raise ContractError(f"missing or unknown request keys: missing={sorted(required - request.keys())}, "
                            f"unknown={sorted(request.keys() - required - optional)}")
    if request["scope"] != "development":
        raise ContractError("portfolio workflow permits only explicit development scope")
    ds, de, ts, te = (_date(request[key]) for key in
                      ("development_start", "development_end", "test_start", "test_end"))
    if not ds <= de < ts < te:
        raise ContractError("dates require development_start <= development_end < test_start < test_end")
    cutoff = datetime.combine(ts, datetime.min.time(), UTC)
    catalog = dict(research_strategy_catalog())
    custom = request.get("strategies", {})
    if not isinstance(custom, Mapping):
        raise ContractError("strategies must map identifiers to strategy definitions")
    for name, definition in custom.items():
        if not isinstance(name, str) or not name or not isinstance(definition, Mapping):
            raise ContractError("custom strategy entries require a name and field mapping")
        fields = dict(definition)
        if fields.pop("strategy_id", name) != name:
            raise ContractError("strategy mapping key and strategy_id disagree")
        fields["family"] = StrategyFamily(fields["family"])
        fields["side"] = StrategySide(fields["side"])
        fields["required_inputs"] = tuple(fields["required_inputs"])
        fields["horizon_days"] = tuple(fields["horizon_days"])
        if (type(fields["rebalance_days"]) is not int
                or any(type(value) is not int for value in fields["horizon_days"])):
            raise ContractError("strategy horizon and rebalance days must be integers")
        catalog[name] = StrategyDefinition(strategy_id=name, **fields)
    names = request.get("strategy_ids", list(custom))
    if not isinstance(names, (list, tuple)) or not names or any(not isinstance(n, str) for n in names):
        raise ContractError("strategy_ids must be a nonempty list of catalog identifiers")
    if len(names) != len(set(names)) or set(names) - catalog.keys():
        raise ContractError("strategy_ids must be unique known catalog identifiers")
    names = sorted(names)
    execution_settings = request["execution_policy"]
    if not isinstance(execution_settings, Mapping) or not {
        "commission_bps", "slippage_bps", "annual_borrow_rate"
    } <= execution_settings.keys():
        raise ContractError("execution_policy must explicitly set commission_bps, slippage_bps, annual_borrow_rate")
    execution = ExecutionPolicy(**execution_settings)
    allocation_settings = request.get("allocation_policy", {})
    if not isinstance(allocation_settings, Mapping) or set(allocation_settings) - {
        "max_sleeve_weight", "covariance_shrinkage", "iterations"
    }:
        raise ContractError("allocation_policy accepts max_sleeve_weight, covariance_shrinkage, iterations")
    if type(allocation_settings.get("iterations", 400)) is not int:
        raise ContractError("allocation iterations must be an integer")
    initial_cash = float(request.get("initial_cash", 1_000_000))
    if not isfinite(initial_cash) or initial_cash <= 0:
        raise ContractError("initial_cash must be finite and positive")
    blobs = {key: Path(request[key]).read_bytes() for key in
             ("bars_csv", "signals_csv", "training_returns_csv")}
    hashes = {key: hashlib.sha256(value).hexdigest() for key, value in blobs.items()}
    hashes = {key.removesuffix("_csv"): value for key, value in hashes.items()}
    if "input_hashes" in request and request["input_hashes"] != hashes:
        raise ContractError("input_hashes must match actual bars, signals, training_returns bytes")
    identity = canonical_hash(hashes)
    bars, bar_keys = [], set()
    for row in _rows(blobs["bars_csv"], ("day", "asset", "open", "high", "low", "close", "dollar_volume", "borrow_available")):
        day, asset = _date(row["day"]), row["asset"]
        if (day, asset) in bar_keys:
            raise ContractError("duplicate bar keys")
        bar_keys.add((day, asset))
        if row["borrow_available"].lower() not in {"true", "false"}:
            raise ContractError("borrow_available must be true or false")
        bar = MarketBar(day, asset, *(float(row[k]) for k in ("open", "high", "low", "close", "dollar_volume")),
                        row["borrow_available"].lower() == "true")
        if ts <= day <= te:
            bars.append(bar)
    if len({bar.day for bar in bars}) < 2:
        raise ContractError("test interval requires at least two actual execution bar dates")
    assets = {bar.asset for bar in bars}
    signals, signal_keys, source_snapshots = [], set(), set()
    for row in _rows(blobs["signals_csv"], ("strategy_id", "asset", "score", "available_at", "snapshot_hash")):
        stamp = _timestamp(row["available_at"])
        key = (row["strategy_id"], row["asset"], stamp)
        if key in signal_keys:
            raise ContractError("duplicate signal keys")
        signal_keys.add(key)
        if row["strategy_id"] not in names or row["asset"] not in assets:
            raise ContractError("signal references unknown sleeve or asset without test bars")
        if stamp.date() > te:
            raise ContractError("signal is available after test_end")
        source_snapshots.add(row["snapshot_hash"])
        signals.append(PointInTimeSignal(row["strategy_id"], row["asset"], float(row["score"]), stamp, identity))
    training = {name: {} for name in names}
    for row in _rows(blobs["training_returns_csv"], ("day", "strategy_id", "return", "available_at")):
        day, name = _date(row["day"]), row["strategy_id"]
        stamp, value = _timestamp(row["available_at"]), float(row["return"])
        if name not in training or not ds <= day <= de:
            raise ContractError("training rows must belong to requested sleeves and development dates")
        if stamp >= cutoff or stamp.date() < day:
            raise ContractError("training return availability must follow its end date and precede test_start")
        if not isfinite(value) or value < -1:
            raise ContractError("training returns must be finite simple returns >= -1")
        if day in training[name]:
            raise ContractError("duplicate training date/sleeve")
        training[name][day] = value
    dates = sorted(training[names[0]])
    if len(dates) < 2 or any(sorted(training[name]) != dates for name in names):
        raise ContractError("training returns need at least two identical aligned dates per sleeve")
    aligned = {name: tuple(training[name][day] for day in dates) for name in names}
    dataset = HistoricalDataset(identity, False, tuple(bars), tuple(signals), allow_unverified=True)
    strategies = {name: catalog[name] for name in names}
    # Refuse a silently empty sleeve due to stale or late availability.
    decision_days = sorted({bar.day for bar in bars})[:-1]
    for name in names:
        strategy = strategies[name]
        if not any(signal.strategy_id == name and signal.available_at.date() < day
                   and (day - signal.available_at.date()).days <= max(strategy.horizon_days)
                   and (day, signal.asset) in bar_keys
                   for i, day in enumerate(decision_days) if i % strategy.rebalance_days == 0
                   for signal in signals):
            raise ContractError(f"no usable pre-decision signal for sleeve {name}")
    comparisons = {}
    for method in ("equal_weight", "inverse_volatility", "minimum_variance"):
        policy = AllocationPolicy(**{"max_sleeve_weight": 1.0, **allocation_settings}, method=method)
        allocation = TraditionalAllocator(policy).allocate(aligned)
        result = PortfolioBacktester(strategies, execution).run(dataset, allocation, initial_cash,
                                                               allow_unverified=True)
        comparisons[method] = {"allocation_policy": asdict(policy), "result": asdict(result),
                               "metrics": _metrics(result, initial_cash)}
    report = {"schema_version": 1, "scope": "development", "status": "research_only", "point_in_time_verified": False,
              "financial_alpha_verified": False, "engine": portfolio_engine_identity(),
              "limitations": ["Timestamp validation is not independent PIT verification.",
                              "Caller-supplied signal lineage is not independent research approval.",
                              "Daily OHLC execution and volume capacity are approximations."],
              "input_hashes": hashes, "dataset_identity": identity,
              "source_snapshot_labels": sorted(source_snapshots),
              "signal_handoff_hashes": request.get("signal_handoff_hashes", {}),
              "allocation_timing": "frozen_before_test", "training_dates": dates,
              "request": dict(request), "strategies": {k: asdict(v) for k, v in strategies.items()},
              "comparisons": comparisons}
    report = json.loads(_json(report))
    if artifact_store is None and request.get("output_directory"):
        artifact_store = LocalArtifactStore(request["output_directory"])
    if artifact_store is not None:
        artifacts = {key: artifact_store.put(value) for key, value in blobs.items()}
        artifacts["request"] = artifact_store.put(_json(dict(request)))
        artifacts["results"] = artifact_store.put(_json(report))
        report["artifacts"] = artifacts
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--output-directory", required=True)
    args = parser.parse_args(argv)
    try:
        request = json.loads(Path(args.request).read_text())
        if not isinstance(request, dict):
            raise ContractError("request JSON must be an object")
        request["output_directory"] = args.output_directory
        print(json.dumps(run_portfolio_workflow(request), sort_keys=True, allow_nan=False))
    except (ContractError, OSError, ValueError) as exc:
        parser.exit(2, f"portfolio workflow: {exc}\n")


if __name__ == "__main__":
    main()
