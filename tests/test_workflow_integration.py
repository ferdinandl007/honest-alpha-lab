"""Actual CLI and supervisor execution on labeled accounting fixtures."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_portfolio_workflow import make_workflow_request

from honest_alpha_lab.artifacts import LocalArtifactStore
from honest_alpha_lab.backtest_io import load_csv_dataset
from honest_alpha_lab.campaign_workers import execute
from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.supervisor import CampaignSpec, ResearchSupervisor, SupervisorStore


def frozen_request(tmp_path):
    request = make_workflow_request(tmp_path)
    request["input_hashes"] = {name: hashlib.sha256(Path(request[name + "_csv"]).read_bytes()).hexdigest()
                               for name in ("bars", "signals", "training_returns")}
    return request


def test_main_cli_archives_inputs_and_three_allocations(tmp_path):
    request = frozen_request(tmp_path)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    result = subprocess.run([sys.executable, "-m", "honest_alpha_lab", "backtest-portfolio",
                             "--request", str(path), "--output-directory", str(tmp_path / "out")],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert not output["financial_alpha_verified"]
    report = json.loads(Path(output["report_artifact"]).read_text())
    assert set(report["comparisons"]) == {"equal_weight", "inverse_volatility", "minimum_variance"}
    store = LocalArtifactStore(tmp_path / "out")
    for name in ("bars", "signals", "training_returns"):
        assert store.get(report["artifacts"][name + "_csv"]) == Path(request[name + "_csv"]).read_bytes()


def test_supervisor_runs_portfolio_in_bounded_child(tmp_path):
    request = frozen_request(tmp_path)
    store = SupervisorStore(tmp_path / "state.sqlite")
    store.register("portfolio", CampaignSpec("portfolio", {
        "request": request, "artifact_root": str(tmp_path / "out")}, timeout_seconds=30))
    ResearchSupervisor(store, tmp_path / "work").step()
    assert store.status()["jobs"][0]["state"] == "completed"
    result_path = next((tmp_path / "work" / "jobs").glob("*/result.json"))
    result = json.loads(result_path.read_text())
    report = json.loads(LocalArtifactStore(tmp_path / "out").get(result["report_artifact_hash"]))
    assert len(report["comparisons"]) == 3
    assert report["input_hashes"] == request["input_hashes"]


def test_portfolio_campaign_rejects_unfrozen_or_changed_inputs(tmp_path):
    request = frozen_request(tmp_path)
    store = SupervisorStore(tmp_path / "state.sqlite")
    store.register("portfolio", CampaignSpec("portfolio", {
        "request": request, "artifact_root": str(tmp_path / "out")}))
    store.enqueue()
    lease = store.claim()
    path = Path(request["bars_csv"])
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ContractError, match="input_hashes"):
        execute(lease, tmp_path / "work")
    request.pop("input_hashes")
    store.register("unfrozen", CampaignSpec("portfolio", {
        "request": request, "artifact_root": str(tmp_path / "out")}))
    store.enqueue()
    with pytest.raises(ContractError, match="frozen input hashes"):
        execute(store.claim(), tmp_path / "work")


def test_legacy_csv_reader_does_not_approve_its_input(tmp_path):
    request = make_workflow_request(tmp_path)
    dataset = load_csv_dataset(request["bars_csv"], request["signals_csv"], "source-label")
    assert dataset.point_in_time_verified is False
