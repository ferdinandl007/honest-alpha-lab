"""Content-verified local snapshots with explicit publication-time row eligibility.

Input is a long Parquet table of observations plus a dated, point-in-time universe.
This validates supplied provenance declarations; it does not confer data rights.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from .contracts import ContractError, canonical_hash
from .panel import ResearchPanel


@dataclass(frozen=True)
class SnapshotDeclaration:
    provider: str
    source_uri: str
    license_id: str
    rights_evidence_uri: str
    availability_evidence_uri: str
    universe_evidence_uri: str
    adjustment_convention: str
    delisting_coverage: str
    security_identifier: str
    retrieved_at: str
    purpose: str = "research"

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in asdict(self).values()):
            raise ContractError("all snapshot provenance declarations are required")
        parsed = datetime.fromisoformat(self.retrieved_at)
        if parsed.tzinfo is None:
            raise ContractError("retrieval timestamp must include timezone")
        if self.purpose not in {"research", "correctness_fixture"}:
            raise ContractError("snapshot purpose must be explicit")


def _digest(content: bytes):
    return hashlib.sha256(content).hexdigest()


def _exclusive_write(path: Path, content: bytes):
    try:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            import os
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != content:
            raise ContractError("immutable snapshot path already contains different bytes")


def _timestamps(series, name):
    # Parsing as UTC directly would silently localize naive provider timestamps.
    values = []
    for raw in series:
        value = pd.Timestamp(raw)
        if pd.isna(value) or value.tzinfo is None:
            raise ContractError(f"{name} must be timezone-aware and nonmissing")
        values.append(value.tz_convert("UTC"))
    return pd.Series(values, index=series.index, dtype="datetime64[us, UTC]")


class ParquetSnapshot:
    """Manifest and exact provider-export bytes bound by content hashes."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.manifest = json.loads((self.directory / "manifest.json").read_text())
        if set(self.manifest) != {"schema_version", "declaration", "files", "snapshot_hash"}:
            raise ContractError("invalid snapshot manifest fields")
        if self.manifest["schema_version"] != 1:
            raise ContractError("unsupported snapshot version")
        expected = canonical_hash({k: v for k, v in self.manifest.items() if k != "snapshot_hash"})
        if expected != self.manifest["snapshot_hash"]:
            raise ContractError("snapshot manifest hash mismatch")
        SnapshotDeclaration(**self.manifest["declaration"])
        if set(self.manifest["files"]) != {"observations.parquet", "universe.parquet", "sessions.parquet"}:
            raise ContractError("snapshot needs observations, universe and exchange sessions")
        self.verify()

    @classmethod
    def create(cls, root: str | Path, *, observations: str | Path, universe: str | Path,
               sessions: str | Path, declaration: SnapshotDeclaration):
        payloads = {name: Path(path).read_bytes() for name, path in (
            ("observations.parquet", observations), ("universe.parquet", universe),
            ("sessions.parquet", sessions))}
        manifest = {"schema_version": 1, "declaration": asdict(declaration),
                    "files": {name: _digest(content) for name, content in payloads.items()}}
        digest = canonical_hash(manifest)
        manifest["snapshot_hash"] = digest
        directory = Path(root).resolve() / digest
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in payloads.items():
            _exclusive_write(directory / name, content)
        _exclusive_write(directory / "manifest.json",
                         (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
        return cls(directory)

    @property
    def snapshot_hash(self):
        return self.manifest["snapshot_hash"]

    def verify(self):
        for name, digest in self.manifest["files"].items():
            if _digest((self.directory / name).read_bytes()) != digest:
                raise ContractError(f"snapshot content changed: {name}")

    def panel(self, *, fields: tuple[str, ...], start: date | None = None,
              end: date | None = None) -> ResearchPanel:
        """Exact-session values only. Later vintages are not backfilled.

        Observation columns: session, asset, field, value, available_at, source_row_id.
        Universe: asset, valid_from, valid_to (nullable), known_at.
        Sessions: session, decision_at. Every session comes from the supplied calendar.
        """
        self.verify()
        if not fields or len(set(fields)) != len(fields):
            raise ContractError("select unique nonempty fields")
        with duckdb.connect(":memory:") as connection:
            observations = connection.execute("SELECT * FROM read_parquet(?)",
                [str(self.directory / "observations.parquet")]).df()
            universe = connection.execute("SELECT * FROM read_parquet(?)",
                [str(self.directory / "universe.parquet")]).df()
            sessions = connection.execute("SELECT * FROM read_parquet(?) ORDER BY session",
                [str(self.directory / "sessions.parquet")]).df()
        requirements = (
            (observations, {"session", "asset", "field", "value", "available_at", "source_row_id"}),
            (universe, {"asset", "valid_from", "valid_to", "known_at"}),
            (sessions, {"session", "decision_at"}),
        )
        for frame, columns in requirements:
            if not columns <= set(frame):
                raise ContractError(f"missing Parquet columns: {sorted(columns - set(frame))}")
        if sessions.empty or sessions["session"].duplicated().any():
            raise ContractError("exchange session calendar must be nonempty and unique")
        sessions["session"] = pd.to_datetime(sessions["session"]).dt.date
        sessions["decision_at"] = _timestamps(sessions["decision_at"], "decision_at")
        if sessions["session"].isna().any() or not sessions.decision_at.is_monotonic_increasing:
            raise ContractError("session decisions must be nonmissing and chronological")
        observations["session"] = pd.to_datetime(observations["session"]).dt.date
        observations["available_at"] = _timestamps(observations["available_at"], "available_at")
        universe["known_at"] = _timestamps(universe["known_at"], "known_at")
        universe["valid_from"] = pd.to_datetime(universe["valid_from"]).dt.date
        universe["valid_to"] = pd.to_datetime(universe["valid_to"]).dt.date
        if observations[["asset", "field", "source_row_id"]].isna().any().any():
            raise ContractError("observation identity and source row identifiers cannot be missing")
        if universe[["asset", "valid_from"]].isna().any().any():
            raise ContractError("universe identity/start cannot be missing")
        if universe.duplicated(["asset", "valid_from", "known_at"]).any():
            raise ContractError("ambiguous historical universe vintage")
        for row in universe.itertuples():
            if pd.notna(row.valid_to) and row.valid_to < row.valid_from:
                raise ContractError("universe interval is inverted")
        if not set(observations["session"]) <= set(sessions["session"]):
            raise ContractError("observations contain sessions absent from exchange calendar")
        if not set(fields) <= set(observations["field"]):
            raise ContractError("requested field is not available in snapshot")
        if observations.duplicated(["session", "asset", "field", "available_at"]).any():
            raise ContractError("ambiguous observations share the same vintage timestamp")
        observations["value"] = pd.to_numeric(observations["value"], errors="raise")
        if not np.isfinite(observations["value"].to_numpy(dtype=float)).all():
            raise ContractError("observed values must be finite; absence is represented by no row")
        if start:
            sessions = sessions[sessions.session >= start]
        if end:
            sessions = sessions[sessions.session <= end]
        if sessions.empty:
            raise ContractError("selected snapshot period is empty")
        dates = tuple(sessions.session)
        assets = tuple(sorted(set(universe.asset)))
        if not assets:
            raise ContractError("universe is empty")
        if not set(observations.asset) <= set(assets):
            raise ContractError("observation security absent from historical universe registry")
        shape = len(dates), len(assets)
        eligible = np.zeros(shape, dtype=bool)
        values = {field: np.full(shape, np.nan) for field in fields}
        asset_index = {asset: i for i, asset in enumerate(assets)}
        date_index = {day: i for i, day in enumerate(dates)}
        for i, (day, decision) in enumerate(zip(dates, sessions.decision_at)):
            known = universe[(universe.valid_from <= day) & (universe.known_at <= decision)]
            latest_membership = known.sort_values(["valid_from", "known_at"]).drop_duplicates("asset", keep="last")
            for row in latest_membership.itertuples():
                if pd.isna(row.valid_to) or day <= row.valid_to:
                    eligible[i, asset_index[row.asset]] = True
        joined = observations.merge(sessions, on="session", how="inner", validate="many_to_one")
        joined = joined[(joined.available_at <= joined.decision_at) & joined.field.isin(fields)]
        latest = joined.sort_values("available_at").drop_duplicates(["session", "asset", "field"], keep="last")
        for row in latest.itertuples():
            values[row.field][date_index[row.session], asset_index[row.asset]] = float(row.value)
        return ResearchPanel(dates, assets, values, eligible)
