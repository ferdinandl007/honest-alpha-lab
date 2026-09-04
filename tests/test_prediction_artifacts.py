"""Software fixtures, not historical alpha evidence."""
import io

import numpy as np
import pandas as pd
import pytest
from test_numerical_cli import make_numerical_snapshot

from honest_alpha_lab.artifacts import LocalArtifactStore
from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.prediction_artifacts import store_predictions


def test_prediction_handoff_preserves_decision_clock_and_missingness(tmp_path):
    snapshot, _ = make_numerical_snapshot(tmp_path)
    panel = snapshot.panel(fields=("x",))
    predictions = np.full(panel.shape, np.nan)
    predictions[-1, 2] = .7
    store = LocalArtifactStore(tmp_path / "outputs")
    result = store_predictions(snapshot, panel, predictions, "a-formula", store)
    signals = pd.read_csv(io.BytesIO(store.get(result["signals_artifact_hash"])))
    assert len(signals) == result["signal_rows"] == 1
    assert signals.iloc[0].asset == panel.assets[2]
    assert signals.iloc[0].available_at == f"{panel.dates[-1]}T21:00:00+00:00"
    assert signals.iloc[0].snapshot_hash == snapshot.snapshot_hash
    parquet = pd.read_parquet(io.BytesIO(store.get(result["predictions_artifact_hash"])))
    assert len(parquet) == predictions.size
    assert parquet.score.notna().sum() == 1
    assert result["prediction_timing"] == "retrospective_development_replay"
    assert result == store_predictions(snapshot, panel, predictions, "a-formula", store)


def test_handoff_rejects_changed_calendar_bytes(tmp_path):
    snapshot, _ = make_numerical_snapshot(tmp_path)
    panel = snapshot.panel(fields=("x",))
    calendar = pd.read_parquet(snapshot.directory / "sessions.parquet")
    calendar["decision_at"] = "1999-01-01T00:00:00Z"
    calendar.to_parquet(snapshot.directory / "sessions.parquet")
    with pytest.raises(ContractError):
        store_predictions(snapshot, panel, np.zeros(panel.shape), "a", LocalArtifactStore(tmp_path / "out"))


@pytest.mark.parametrize("bad", [np.zeros((2, 3)), np.full((110, 8), np.inf)])
def test_invalid_prediction_shape_or_infinity_rejected(tmp_path, bad):
    snapshot, _ = make_numerical_snapshot(tmp_path)
    panel = snapshot.panel(fields=("x",))
    with pytest.raises(ContractError):
        store_predictions(snapshot, panel, bad, "a", LocalArtifactStore(tmp_path / "out"))
