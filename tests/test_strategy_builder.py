"""Synthetic accounting and trust-boundary fixtures, not alpha evidence."""
import copy
import csv
import hashlib
import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from honest_alpha_lab.cli_agents import CliAgentSpec, CliSubagentWorker, FileTaskStore
from honest_alpha_lab.contracts import AgentKind, ContractError
from honest_alpha_lab.strategy_builder import (
    LockedStrategyContext,
    StrategyBlueprint,
    build_strategy_request,
    run_strategy_agent,
)


def write_csv(path, header, rows):
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header.split(","))
        writer.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def context(tmp_path):
    bars = tmp_path / "bars.csv"
    digest = write_csv(bars, "day,asset,open,high,low,close,dollar_volume,borrow_available", [
        [f"2020-01-{day:02}", asset, 100 + day, 105 + day, 95 + day, 101 + day, 1e8, "true"]
        for day in (2, 3, 4, 5, 8, 9, 10, 11) for asset in ("A", "B")])
    signals = tmp_path / "raw.csv"
    signal_hash = write_csv(signals, "signal_id,asset,score,available_at,snapshot_hash", [
        [name, asset, score, f"2020-01-{day:02}T20:00:00+00:00", "synthetic"]
        for name, scores in (("a", (1, 3)), ("b", (5, 1)))
        for asset, score in zip(("A", "B"), scores, strict=True) for day in (1, 7)])
    return {"portfolio_request": {
        "bars_csv": str(bars), "scope": "development", "development_start": "2020-01-02",
        "development_end": "2020-01-05", "test_start": "2020-01-08", "test_end": "2020-01-11",
        "initial_cash": 1000, "execution_policy": {
            "commission_bps": 10, "slippage_bps": 5, "annual_borrow_rate": .02,
            "limit_offset_bps": 0, "max_asset_weight": 1, "participation_rate": 1}},
        "bars_sha256": digest,
        "signal_catalog": {name: {"csv_path": str(signals), "sha256": signal_hash} for name in ("a", "b")},
        "allowed_horizons": [20], "allowed_sides": ["long_only", "long_short", "short_only"],
        "allowed_rebalance_days": [1, 2], "signal_lag_days": 0, "max_staleness_days": 10}


def proposal(**changes):
    return {"blueprints": [{"strategy_id": "blend", "name": "Equal blend", "signal_ids": ["a", "b"],
                            "combination": "linear_equal", "horizon_days": 20, "side": "long_only",
                            "rebalance_days": 1, "execution": "next_bar_limit", "rationale": "Fixture",
                            **changes}]}


def rows(path):
    with Path(path).open() as handle:
        return list(csv.DictReader(handle))


def test_complete_build_compiles_signals_generates_training_and_backtests(context, tmp_path):
    original = copy.deepcopy(context)
    build = build_strategy_request(proposal(), context, tmp_path / "built")
    assert context == original
    assert build.blueprints[0].coefficients == (.5, .5)
    compiled = rows(build.request["signals_csv"])
    assert [float(r["score"]) for r in compiled[:2]] == [3, 2]
    assert all(r["available_at"] < "2020-01-11" for r in compiled)
    training = rows(build.request["training_returns_csv"])
    assert len(training) == 4
    assert any(float(r["return"]) != 0 for r in training)
    assert all(r["available_at"] < "2020-01-08" for r in training)
    report = build.run_backtest()
    assert set(report["comparisons"]) == {"equal_weight", "inverse_volatility", "minimum_variance"}
    for comparison in report["comparisons"].values():
        assert comparison["result"]["fills"]
        assert min(f["day"] for f in comparison["result"]["fills"]) == "2020-01-09"
    assert report["request"]["execution_policy"] == context["portfolio_request"]["execution_policy"]
    assert not report["point_in_time_verified"]
    assert build_strategy_request(proposal(), context, tmp_path / "built") == build


def test_rank_equal_and_permutation_are_deterministic(context, tmp_path):
    build = build_strategy_request(proposal(combination="rank_equal"), context, tmp_path / "rank")
    assert all(float(row["score"]) == 0 for row in rows(build.request["signals_csv"]))
    permuted = build_strategy_request(proposal(combination="rank_equal", signal_ids=["b", "a"]),
                                      context, tmp_path / "rank")
    assert build == permuted


@pytest.mark.parametrize("change", [
    {"weights": [1, 0]}, {"coefficients": [1, 0]}, {"execution_policy": {}},
    {"training_returns_csv": "elsewhere.csv"}, {"signal_ids": ["unknown"]},
    {"horizon_days": 5}, {"rebalance_days": 3}, {"execution": "buy_before_close"},
    {"signal_ids": ["a", "a"]}, {"horizon_days": True},
])
def test_rejects_untrusted_weights_policy_and_unknown_inputs(context, tmp_path, change):
    with pytest.raises(ContractError):
        build_strategy_request(proposal(**change), context, tmp_path / "built")
    assert not (tmp_path / "built").exists()


def test_context_is_deep_frozen_and_rejects_generated_input_overrides(context):
    locked = LockedStrategyContext.from_payload(context)
    context["portfolio_request"]["execution_policy"]["commission_bps"] = 999
    assert locked.payload["portfolio_request"]["execution_policy"]["commission_bps"] == 10
    with pytest.raises(FrozenInstanceError):
        locked.payload_json = "{}"
    changed = locked.payload
    changed["portfolio_request"]["training_returns_csv"] = "unsafe.csv"
    with pytest.raises(ContractError):
        LockedStrategyContext.from_payload(changed)


def test_changed_input_hash_is_rejected(context, tmp_path):
    Path(context["portfolio_request"]["bars_csv"]).write_text("changed")
    with pytest.raises(ContractError, match="bytes changed"):
        build_strategy_request(proposal(), context, tmp_path / "built")


def test_missing_stale_and_lag_fail_explicitly(context, tmp_path):
    context["max_staleness_days"] = 1
    with pytest.raises(ContractError, match="missing/stale"):
        build_strategy_request(proposal(), context, tmp_path / "built")
    context["max_staleness_days"] = 10
    context["signal_lag_days"] = 1
    with pytest.raises(ContractError, match="missing/stale"):
        build_strategy_request(proposal(), context, tmp_path / "built")


def test_future_append_preserves_scores_and_training(context, tmp_path):
    first = build_strategy_request(proposal(), context, tmp_path / "first")
    path = Path(context["signal_catalog"]["a"]["csv_path"])
    with path.open("a") as handle:
        handle.write("a,A,1000,2020-01-09T12:00:00+00:00,late\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for source in context["signal_catalog"].values():
        source["sha256"] = digest
    second = build_strategy_request(proposal(), context, tmp_path / "second")
    before, after = rows(first.request["signals_csv"]), rows(second.request["signals_csv"])
    # The Jan 9 midday observation cannot affect Jan 8 or Jan 9 decisions.
    assert [r["score"] for r in before[:4]] == [r["score"] for r in after[:4]]
    assert before[4]["score"] != after[4]["score"]
    assert Path(first.request["training_returns_csv"]).read_bytes() == Path(second.request["training_returns_csv"]).read_bytes()


def test_operational_agent_uses_cli_protocol_then_trusted_compiler(context, tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "fake_strategy_agent.py"
    worker = CliSubagentWorker(AgentKind.STRATEGY_BUILDER,
                              CliAgentSpec("fake", sys.executable, (str(fixture),)),
                              FileTaskStore(tmp_path / "tasks"))
    result = run_strategy_agent(context, tmp_path / "built", worker=worker)
    assert result["status"] == "research_only"
    assert result["execution_scope"] == "daily_ohlc_next_bar_limit"
    assert result["orders_authorized"] is False
    assert result["backtest"]["comparisons"]["equal_weight"]["result"]["fills"]
    assert list((tmp_path / "tasks").glob("*/result.json"))
    assert StrategyBlueprint.from_payload(result["blueprints"][0]).strategy_id == "blend"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("extra,match", [
    ("a,A,1,2020-01-01T20:00:00+00:00,duplicate\n", "unique"),
    ("a,A,1,2020-01-01T20:00:00,naive\n", "timezone-aware"),
    ("a,A,nan,2020-01-06T20:00:00+00:00,missing\n", "finite"),
])
def test_invalid_observations_are_not_filled_or_accepted(context, tmp_path, extra, match):
    path = Path(context["signal_catalog"]["a"]["csv_path"])
    with path.open("a") as handle:
        handle.write(extra)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for source in context["signal_catalog"].values():
        source["sha256"] = digest
    with pytest.raises(ContractError, match=match):
        build_strategy_request(proposal(), context, tmp_path / "built")


def test_multiple_sleeves_produce_aligned_actual_training_returns(context, tmp_path):
    proposals = proposal()
    proposals["blueprints"].extend(proposal(strategy_id="second", side="long_short", rebalance_days=2)["blueprints"])
    build = build_strategy_request(proposals, context, tmp_path / "built")
    training = rows(build.request["training_returns_csv"])
    assert {r["day"] for r in training if r["strategy_id"] == "blend"} == {
        r["day"] for r in training if r["strategy_id"] == "second"}
    report = build.run_backtest()
    assert report["comparisons"]["equal_weight"]["result"]["allocation"] == {
        "blend": .5, "second": .5}


def test_same_day_midnight_observation_waits_until_next_decision(context, tmp_path):
    path = Path(context["signal_catalog"]["a"]["csv_path"])
    with path.open("a") as handle:
        handle.write("a,A,101,2020-01-08T00:00:00+00:00,boundary\n")
    for source in context["signal_catalog"].values():
        source["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    build = build_strategy_request(proposal(), context, tmp_path / "built")
    compiled = rows(build.request["signals_csv"])
    assert float(compiled[0]["score"]) == 3
    assert float(compiled[2]["score"]) == 53


def test_holdout_bars_cannot_change_generated_training_returns(context, tmp_path):
    first = build_strategy_request(proposal(), context, tmp_path / "first")
    path = Path(context["portfolio_request"]["bars_csv"])
    content = rows(path)
    for row in content:
        if row["day"] >= "2020-01-08":
            for key in ("open", "high", "low", "close"):
                row[key] = float(row[key]) * 10
    context["bars_sha256"] = write_csv(path, ",".join(content[0]), [list(r.values()) for r in content])
    second = build_strategy_request(proposal(), context, tmp_path / "second")
    assert Path(first.request["training_returns_csv"]).read_bytes() == Path(second.request["training_returns_csv"]).read_bytes()
