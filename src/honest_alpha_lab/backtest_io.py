"""Legacy CSV readers; parsing is not independent historical-data approval.

Use portfolio_workflow for dated training, content binding and audited execution.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path

from .contracts import ContractError
from .portfolio import HistoricalDataset, MarketBar, PointInTimeSignal


def load_csv_dataset(bars_path: str | Path, signals_path: str | Path, snapshot_hash: str) -> HistoricalDataset:
    bars = []
    with Path(bars_path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            bars.append(MarketBar(
                date.fromisoformat(_required(row, "day")), _required(row, "asset"),
                float(_required(row, "open")), float(_required(row, "high")),
                float(_required(row, "low")), float(_required(row, "close")),
                float(_required(row, "dollar_volume")), _bool(row.get("borrow_available", "false")),
            ))
    signals = []
    with Path(signals_path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if _required(row, "snapshot_hash") != snapshot_hash:
                raise ContractError("signal CSV does not match the supplied snapshot hash")
            signals.append(PointInTimeSignal(
                _required(row, "strategy_id"), _required(row, "asset"), float(_required(row, "score")),
                datetime.fromisoformat(_required(row, "available_at")), snapshot_hash,
            ))
    return HistoricalDataset(snapshot_hash, False, tuple(bars), tuple(signals), allow_unverified=True)


def load_training_returns(path: str | Path) -> Mapping[str, tuple[float, ...]]:
    grouped: dict[str, list[float]] = {}
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            grouped.setdefault(_required(row, "strategy_id"), []).append(float(_required(row, "return")))
    if not grouped:
        raise ContractError("training-return CSV is empty")
    return {strategy_id: tuple(values) for strategy_id, values in grouped.items()}


def _required(row: Mapping[str, str], name: str) -> str:
    value = row.get(name, "")
    if not value:
        raise ContractError(f"CSV requires column {name!r}")
    return value


def _bool(value: str) -> bool:
    if value.lower() not in {"true", "false"}:
        raise ContractError("borrow_available must be true or false")
    return value.lower() == "true"
