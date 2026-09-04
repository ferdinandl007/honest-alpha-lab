"""Full CLI plumbing fixture, explicitly ineligible as financial evidence."""
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from honest_alpha_lab.artifacts import LocalArtifactStore
from honest_alpha_lab.snapshots import ParquetSnapshot, SnapshotDeclaration


def make_numerical_snapshot(tmp_path):
    rng = np.random.default_rng(54)
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(110)]
    assets = [f"A{i}" for i in range(8)]
    stock = 100 * np.exp(np.cumsum(rng.normal(0, .01, (110, 8)), axis=0))
    market = 100 * np.exp(np.cumsum(rng.normal(0, .01, (110, 1)), axis=0))
    sector = 100 * np.exp(np.cumsum(rng.normal(0, .01, (110, 1)), axis=0))
    features = {"x": rng.normal(size=(110, 8))}
    for suffix in ("open", "close"):
        features[f"total_return_{suffix}"] = stock
        features[f"sector_total_return_{suffix}"] = np.repeat(sector, 8, axis=1)
        features[f"market_total_return_{suffix}"] = np.repeat(market, 8, axis=1)
    rows = [(str(day), asset, field, float(values[i, j]), f"{day}T20:00:00Z", f"{i}:{j}:{field}")
            for field, values in features.items() for i, day in enumerate(dates)
            for j, asset in enumerate(assets)]
    pd.DataFrame(rows, columns=["session", "asset", "field", "value", "available_at", "source_row_id"]).to_parquet(tmp_path / "observations.parquet")
    pd.DataFrame({"asset": assets, "valid_from": str(dates[0]), "valid_to": None,
                  "known_at": "2019-01-01T00:00:00Z"}).to_parquet(tmp_path / "universe.parquet")
    pd.DataFrame({"session": [str(d) for d in dates],
                  "decision_at": [f"{d}T21:00:00Z" for d in dates]}).to_parquet(tmp_path / "sessions.parquet")
    declaration = SnapshotDeclaration("test", "fixture://input", "test-only", "fixture://rights",
        "fixture://availability", "fixture://universe", "test-total-return", "test-only",
        "test-id", "2020-12-01T00:00:00Z", purpose="correctness_fixture")
    snapshot = ParquetSnapshot.create(tmp_path / "snapshots",
        observations=tmp_path / "observations.parquet", universe=tmp_path / "universe.parquet",
        sessions=tmp_path / "sessions.parquet", declaration=declaration)
    return snapshot, dates


def test_parquet_to_cli_to_content_verified_artifacts(tmp_path):
    snapshot, dates = make_numerical_snapshot(tmp_path)
    command = [sys.executable, "-m", "honest_alpha_lab", "evaluate-formula",
        "--snapshot", str(snapshot.directory), "--formula", "rank(x)", "--field", "x",
        "--development-end", str(dates[-1]), "--beta-window", "5", "--horizon", "5",
        "--train-days", "10", "--test-days", "10", "--min-assets", "4",
        "--bootstrap-samples", "99", "--output-directory", str(tmp_path / "results")]
    import os
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    refused = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=60, check=False)
    assert refused.returncode != 0
    assert "not financial benchmarks" in refused.stderr
    result = subprocess.run([*command, "--allow-correctness-fixture"], capture_output=True,
                             text=True, env=environment, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["scope"] == "development"
    assert output["snapshot_purpose"] == "correctness_fixture"
    assert output["financial_alpha_verified"] is False
    report = json.loads(LocalArtifactStore(tmp_path / "results").get(Path(output["report_artifact"]).name))
    assert report["report"]["snapshot_hash"] == snapshot.snapshot_hash
    assert report["registry_status"] == "not_promoted"
    assert report["provenance_status"] == "declared_not_independently_approved"
    assert len(report["report"]["folds"]) >= 2
