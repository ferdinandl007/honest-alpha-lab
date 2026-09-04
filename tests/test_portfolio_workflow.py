"""Synthetic accounting fixtures; these do not establish historical PIT quality."""
import copy
import csv
import hashlib
import json
from datetime import UTC

import pytest

from honest_alpha_lab.artifacts import LocalArtifactStore
from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.portfolio import AllocationPolicy, TraditionalAllocator
from honest_alpha_lab.portfolio_workflow import main, run_portfolio_workflow


def write_csv(path, fields, rows):
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields.split(","))
        writer.writerows(rows)
    return str(path)


def make_workflow_request(tmp_path):
    """Reusable on-disk synthetic request for parent workflow integration tests."""
    bars = write_csv(tmp_path / "bars.csv", "day,asset,open,high,low,close,dollar_volume,borrow_available",
                     [[f"2020-02-0{day}", "A", 100, 102, 98, 101, 1e7, "true"] for day in (3, 4, 5)])
    signals = write_csv(tmp_path / "signals.csv", "strategy_id,asset,score,available_at,snapshot_hash,session",
                        [[name, "A", 1, "2020-02-02T20:00:00+00:00", "source-label", "2020-02-02"]
                         for name in ("formula-a", "formula-b")])
    training = write_csv(tmp_path / "training.csv", "day,strategy_id,return,available_at",
                         [[f"2020-01-0{day}", name, value * scale, f"2020-01-0{day}T20:00:00+00:00"]
                          for name, scale in (("formula-a", 1), ("formula-b", 2))
                          for day, value in ((2, -.01), (3, .01), (4, 0))])
    return {"scope": "development", "bars_csv": bars, "signals_csv": signals,
            "training_returns_csv": training,
            "development_start": "2020-01-01", "development_end": "2020-01-31",
            "test_start": "2020-02-03", "test_end": "2020-02-05",
            "strategies": {name: {"name": name, "family": "symbolic", "signal_description": "fixture",
                                  "required_inputs": ["score"], "horizon_days": [20],
                                  "side": "long_only", "rebalance_days": 1}
                           for name in ("formula-a", "formula-b")},
            "initial_cash": 1000, "execution_policy": {
                "commission_bps": 10, "slippage_bps": 5, "annual_borrow_rate": .02,
                "limit_offset_bps": 0, "max_asset_weight": 1, "participation_rate": 1},
            "output_directory": str(tmp_path / "artifacts")}


@pytest.fixture
def request_data(tmp_path):
    return make_workflow_request(tmp_path)


def test_complete_workflow_artifacts_and_accounting(request_data):
    report = run_portfolio_workflow(request_data)
    assert not report["point_in_time_verified"]
    assert report["scope"] == "development"
    assert len(report["comparisons"]) == 3
    json.dumps(report, allow_nan=False)
    store = LocalArtifactStore(request_data["output_directory"])
    saved = json.loads(store.get(report["artifacts"]["results"]))
    assert saved["comparisons"] == report["comparisons"]
    for name in ("bars", "signals", "training_returns"):
        blob = store.get(report["artifacts"][name + "_csv"])
        assert hashlib.sha256(blob).hexdigest() == report["input_hashes"][name]
    for comparison in report["comparisons"].values():
        result, metrics = comparison["result"], comparison["metrics"]
        assert result["fills"]
        assert min(fill["day"] for fill in result["fills"]) == "2020-02-04"
        assert metrics["commission_cost"] == pytest.approx(sum(f["commission"] for f in result["fills"]))
        assert metrics["total_return"] == pytest.approx(metrics["final_nav"] / 1000 - 1)
        assert metrics["gross_turnover"] > 0
        assert metrics["slippage_cost"] is None
    request_data["input_hashes"] = report["input_hashes"]
    run_portfolio_workflow(request_data)
    request_data["input_hashes"]["bars"] = "0" * 64
    with pytest.raises(ContractError, match="input_hashes"):
        run_portfolio_workflow(request_data)


@pytest.mark.parametrize("change,match", [
    ({"scope": "sealed"}, "development scope"),
    ({"development_end": "2020-02-03"}, "dates require"),
    ({"initial_cash": float("nan")}, "initial_cash"),
    ({"execution_policy": {}}, "explicitly"),
    ({"allocation_policy": {"max_sleeve_weight": .4}}, "cap"),
    ({"input_hashes": {}}, "input_hashes"),
    ({"strategy_ids": ["missing"]}, "known"),
    ({"typo": 1}, "unknown"),
])
def test_rejects_bad_request(request_data, change, match):
    request_data.update(change)
    with pytest.raises(ContractError, match=match):
        run_portfolio_workflow(request_data)


@pytest.mark.parametrize("replacement,match", [
    ("2020-02-03T00:00:00+00:00", "precede test_start"),
    ("2020-01-02T20:00:00", "timezone-aware"),
    ("2019-12-31T20:00:00+00:00", "end date"),
])
def test_training_availability(request_data, replacement, match):
    from pathlib import Path
    path = Path(request_data["training_returns_csv"])
    path.write_text(path.read_text().replace("2020-01-02T20:00:00+00:00", replacement))
    with pytest.raises(ContractError, match=match):
        run_portfolio_workflow(request_data)


def test_training_alignment_checks_dates_not_lengths(request_data):
    from pathlib import Path
    path = Path(request_data["training_returns_csv"])
    path.write_text(path.read_text().replace("2020-01-02,formula-b", "2020-01-01,formula-b"))
    with pytest.raises(ContractError, match="aligned dates"):
        run_portfolio_workflow(request_data)


@pytest.mark.parametrize("key,needle,replacement,match", [
    ("bars_csv", "100,102", "nan,102", "finite"),
    ("bars_csv", "true", "maybe", "borrow_available"),
    ("signals_csv", "T20:00:00+00:00", "T20:00:00", "timezone-aware"),
    ("signals_csv", "2020-02-02T", "2020-02-06T", "after test_end"),
    ("signals_csv", "2020-02-02T", "2020-01-01T", "no usable"),
    ("training_returns_csv", "-0.01", "nan", "finite"),
])
def test_invalid_csv(request_data, key, needle, replacement, match):
    from pathlib import Path
    path = Path(request_data[key])
    path.write_text(path.read_text().replace(needle, replacement))
    with pytest.raises(ContractError, match=match):
        run_portfolio_workflow(request_data)


def test_allocation_proportions_and_caps():
    returns = {"a": [-.01, .01], "b": [-.02, .02], "c": [-.04, .04]}
    allocation = TraditionalAllocator(AllocationPolicy("inverse_volatility", 1)).allocate(returns)
    assert allocation == pytest.approx({"a": 4 / 7, "b": 2 / 7, "c": 1 / 7})
    capped = TraditionalAllocator(AllocationPolicy("inverse_volatility", .5)).allocate(returns)
    assert capped == pytest.approx({"a": .5, "b": 1 / 3, "c": 1 / 6})
    equal = TraditionalAllocator(AllocationPolicy("equal_weight", .5)).allocate(returns)
    assert equal == pytest.approx(dict.fromkeys(returns, 1 / 3))


def test_cli_json_output(request_data, tmp_path, capsys):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request_data))
    main(["--request", str(path), "--output-directory", str(tmp_path / "output")])
    assert json.loads(capsys.readouterr().out)["artifacts"]["results"]


def test_no_store_still_returns_json(request_data):
    request = copy.deepcopy(request_data)
    request.pop("output_directory")
    assert "artifacts" not in run_portfolio_workflow(request)


def test_unverified_data_requires_opt_in_at_construction_and_execution():
    from datetime import date, datetime

    from honest_alpha_lab.portfolio import (
        HistoricalDataset,
        MarketBar,
        PointInTimeSignal,
        PortfolioBacktester,
    )
    from honest_alpha_lab.strategies import research_strategy_catalog
    bars = (MarketBar(date(2020, 1, 2), "A", 100, 101, 99, 100, 10000),)
    signals = (PointInTimeSignal("exercise_retain", "A", 1,
                                datetime(2020, 1, 1, tzinfo=UTC), "fixture"),)
    with pytest.raises(ContractError, match="verified"):
        HistoricalDataset("fixture", False, bars, signals)
    dataset = HistoricalDataset("fixture", False, bars, signals, allow_unverified=True)
    engine = PortfolioBacktester(research_strategy_catalog())
    with pytest.raises(ContractError, match="allow_unverified"):
        engine.run(dataset, {"exercise_retain": 1})
    assert engine.run(dataset, {"exercise_retain": 1}, allow_unverified=True).nav_by_day


def test_truncated_csv_row_is_contract_error(request_data):
    from pathlib import Path
    Path(request_data["training_returns_csv"]).write_text("day,strategy_id,return,available_at\n2020-01-02\n")
    with pytest.raises(ContractError, match="missing required"):
        run_portfolio_workflow(request_data)
