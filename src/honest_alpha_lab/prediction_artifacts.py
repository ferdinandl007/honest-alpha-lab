"""Content-bound numerical predictions and dated execution handoff.

These are retrospective development predictions, not contemporaneously generated
live signals or evidence that source provenance has independently been approved.
"""
from __future__ import annotations

import io
import json

import numpy as np
import pandas as pd

from .contracts import ContractError


def store_predictions(snapshot, panel, predictions, strategy_id, artifacts):
    """Archive predictions and a CSV accepted by the portfolio research workflow.

    Preserve the exact supplied decision clocks; never invent a close timestamp.
    Execution applies its own conservative next-date/next-bar delay afterward.
    """
    snapshot.verify()
    panel.require_execution_timing()
    if panel.snapshot_hash != snapshot.snapshot_hash:
        raise ContractError("prediction panel must be bound to the supplied snapshot")
    values = np.asarray(predictions, dtype=float)
    if values.shape != panel.shape or np.isinf(values).any() or not strategy_id:
        raise ContractError("prediction handoff requires aligned scores and a strategy identity")
    sessions = pd.read_parquet(snapshot.directory / "sessions.parquet")
    sessions["session"] = pd.to_datetime(sessions["session"]).dt.date
    if sessions.session.duplicated().any():
        raise ContractError("prediction calendar has duplicate sessions")
    clocks = {}
    opens = {}
    for row in sessions.itertuples():
        stamp = pd.Timestamp(row.decision_at)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ContractError("prediction decision clocks must be timezone-aware")
        clocks[row.session] = stamp.isoformat()
        opens[row.session] = pd.Timestamp(row.open_at)
    if not set(panel.dates) <= set(clocks):
        raise ContractError("prediction sessions absent from snapshot calendar")
    if any(pd.Timestamp(clocks[day]) != decision or opens[day] != opening
           for day, decision, opening in zip(panel.dates, panel.decision_at, panel.open_at)):
        raise ContractError("prediction calendar differs from its bound panel")
    frame = pd.DataFrame({"session": np.repeat(panel.dates, len(panel.assets)),
                          "asset": np.tile(panel.assets, len(panel.dates)),
                          "score": values.ravel()})
    frame["available_at"] = frame.session.map(clocks)
    frame["strategy_id"] = strategy_id
    frame["snapshot_hash"] = snapshot.snapshot_hash
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    predictions_hash = artifacts.put(buffer.getvalue())
    finite = frame[np.isfinite(frame.score)].copy()
    signals_hash = artifacts.put(finite.to_csv(index=False).encode("utf-8"))
    return {"predictions_artifact_hash": predictions_hash,
            "signals_artifact_hash": signals_hash,
            "strategy_id": strategy_id,
            "prediction_timing": "retrospective_development_replay",
            "timing_contract": "decision_before_next_open",
            "input_provenance": json.loads(panel.provenance_json),
            "signal_rows": len(finite)}
