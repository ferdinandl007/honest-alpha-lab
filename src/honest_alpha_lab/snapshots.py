"""Content-verified local snapshots with explicit publication-time row eligibility.

Input is a long Parquet table of observations plus a dated, point-in-time universe.
This validates supplied provenance declarations; it does not confer data rights.
"""
from __future__ import annotations

import hashlib
import bisect
import io
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


def _validate_feature_clocks(feature):
    """Check claimed clocks against the actual dependencies of every exported row.

    Content hashes alone cannot justify relabeling a retrospective extraction.
    This establishes internal consistency, not independent source authenticity.
    """
    from .features import FeaturePlan

    def stamp(raw):
        value = pd.Timestamp(raw)
        if pd.isna(value) or value.tzinfo is None:
            raise ContractError("feature dependency clocks must be explicit and timezone-aware")
        return value.tz_convert("UTC")

    try:
        inputs = feature["inputs"]
        plan = FeaturePlan(**feature["plan"])
        sources = {canonical_hash(source): source for source in inputs["sources"]}
        exposures = {canonical_hash(exposure): exposure for exposure in inputs["exposures"]}
        sessions = [(date.fromisoformat(s["session"]), stamp(s["decision_at"]))
                    for s in inputs["sessions"]]
        if not sessions or any(a[0] >= b[0] or a[1] >= b[1] for a, b in zip(sessions, sessions[1:])):
            raise ContractError("feature dependency sessions must be nonempty and chronological")
        by_session = {day: (index, clock) for index, (day, clock) in enumerate(sessions)}
        decisions = [clock for _, clock in sessions]
        for row in feature["row_provenance"]:
            day = date.fromisoformat(row["session"])
            index, decision = by_session[day]
            if stamp(row["available_at"]) != decision or not row["contributions"]:
                raise ContractError("feature rows need decision-aligned clocks and explicit dependencies")
            for contribution in row["contributions"]:
                source = sources[contribution["source_vintage_hash"]]
                exposure = exposures[contribution["exposure_vintage_hash"]]
                published, retrieved, extracted = (stamp(source[name]) for name in
                    ("published_at", "retrieved_at", "extracted_at"))
                effective = extracted if plan.clock == "as_run" else published
                if (not published <= retrieved <= extracted
                        or effective > decision or stamp(exposure["available_at"]) > decision
                        or source["dataset_id"] != plan.dataset_id
                        or source["source_key"] != exposure["source_key"]
                        or exposure["asset"] != row["asset"]):
                    raise ContractError("feature dependency clocks or identities contradict the declared plan")
                if (plan.lag_sessions and effective < decisions[0]
                        or bisect.bisect_left(decisions, effective) + plan.lag_sessions > index):
                    raise ContractError("feature dependency clock violates the declared session lag")
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"invalid feature dependency evidence: {exc}") from exc


class ParquetSnapshot:
    """Manifest and exact provider-export bytes bound by content hashes."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.manifest = json.loads((self.directory / "manifest.json").read_text())
        schema = self.manifest.get("schema_version")
        keys = {"schema_version", "declaration", "files", "snapshot_hash"}
        if schema == 2:
            keys.add("feature_manifests")
        if set(self.manifest) != keys:
            raise ContractError("invalid snapshot manifest fields")
        if schema not in {1, 2}:
            raise ContractError("unsupported snapshot version")
        expected = canonical_hash({k: v for k, v in self.manifest.items() if k != "snapshot_hash"})
        if expected != self.manifest["snapshot_hash"]:
            raise ContractError("snapshot manifest hash mismatch")
        SnapshotDeclaration(**self.manifest["declaration"])
        expected_files = {"observations.parquet", "universe.parquet", "sessions.parquet"}
        if schema == 2:
            for name in self.manifest["files"]:
                if name.startswith("feature-artifacts/") and len(name.split("/")) == 2:
                    digest = name.split("/")[1]
                    if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
                        expected_files.add(name)
        if set(self.manifest["files"]) != expected_files:
            raise ContractError("snapshot needs observations, universe and exchange sessions")
        self.verify()

    @classmethod
    def create(cls, root: str | Path, *, observations: str | Path, universe: str | Path,
               sessions: str | Path, declaration: SnapshotDeclaration,
               feature_manifests: dict[str, str] | None = None, feature_artifacts=None):
        payloads = {name: Path(path).read_bytes() for name, path in (
            ("observations.parquet", observations), ("universe.parquet", universe),
            ("sessions.parquet", sessions))}
        calendar = pd.read_parquet(io.BytesIO(payloads["sessions.parquet"]))
        if "open_at" not in calendar:
            raise ContractError("new snapshots require explicit open_at clocks; reimport with an execution calendar")
        bindings = dict(feature_manifests or {})
        for digest in bindings.values():
            if feature_artifacts is None:
                raise ContractError("feature manifest binding requires its artifact store")
            content = feature_artifacts.get(digest)
            feature = json.loads(content)
            payloads[f"feature-artifacts/{digest}"] = content
            observation_hash = feature["observations_hash"]
            payloads[f"feature-artifacts/{observation_hash}"] = feature_artifacts.get(observation_hash)
        manifest = {"schema_version": 2, "declaration": asdict(declaration),
                    "feature_manifests": bindings,
                    "files": {name: _digest(content) for name, content in payloads.items()}}
        digest = canonical_hash(manifest)
        manifest["snapshot_hash"] = digest
        directory = Path(root).resolve() / digest
        directory.mkdir(parents=True, exist_ok=True)
        if bindings:
            (directory / "feature-artifacts").mkdir(exist_ok=True)
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
        if self.manifest["schema_version"] == 2:
            self._feature_provenance()

    def _feature_provenance(self):
        """Bind exact feature rows, including after Parquet reserialization/merging.

        Unbound fields are unknown provider declarations, never inferred as-run.
        This checks supplied evidence consistency, not independent authenticity.
        """
        observations = pd.read_parquet(self.directory / "observations.parquet")
        result = {str(name): {"clock": "unknown", "kind": "provider_declared"}
                  for name in observations.field.unique()}
        if self.manifest["schema_version"] == 1:
            return {"status": "legacy_unknown", "fields": result}

        def artifact(digest):
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ContractError("invalid feature artifact hash")
            name = f"feature-artifacts/{digest}"
            if self.manifest["files"].get(name) != digest:
                raise ContractError("feature artifact missing from snapshot commitment")
            content = (self.directory / name).read_bytes()
            if _digest(content) != digest:
                raise ContractError("feature artifact content changed")
            return content

        def rows(frame):
            columns = ["session", "asset", "field", "value", "available_at", "source_row_id"]
            if not set(columns) <= set(frame):
                raise ContractError("feature observations missing binding columns")
            frame = frame[columns].copy()
            frame["session"] = pd.to_datetime(frame.session).dt.date
            frame["available_at"] = _timestamps(frame.available_at, "feature available_at")
            frame["value"] = pd.to_numeric(frame.value, errors="raise").astype(float)
            return frame.sort_values(columns).reset_index(drop=True)

        for name, digest in self.manifest["feature_manifests"].items():
            feature = json.loads(artifact(digest))
            plan = feature.get("plan", {})
            if (feature.get("schema_version") != 1 or plan.get("field") != name
                    or plan.get("clock") not in {"as_run", "publication_replay"}
                    or feature.get("inputs", {}).get("plan") != plan
                    or canonical_hash(feature["inputs"]) != feature.get("input_hash")):
                raise ContractError("feature manifest plan/input binding mismatch")
            replay = plan["clock"] == "publication_replay"
            llm = any(source.get("extraction_kind") == "llm"
                      for source in feature["inputs"].get("sources", []))
            if feature.get("retrospective_replay") is not replay or (replay and llm and plan.get("allow_retrospective_llm") is not True):
                raise ContractError("inconsistent retrospective feature evidence")
            _validate_feature_clocks(feature)
            original = rows(pd.read_parquet(io.BytesIO(artifact(feature["observations_hash"]))))
            declared = rows(pd.DataFrame(feature["row_provenance"], columns=[
                "session", "asset", "field", "value", "available_at", "source_row_id"]))
            actual = rows(observations[observations.field == name])
            if (len(original) != feature.get("row_count") or not original.equals(declared)
                    or not original.equals(actual) or not set(original.field) <= {name}):
                raise ContractError("snapshot feature rows differ from the bound feature export")
            result[name] = {"kind": "materialized_feature", "manifest_hash": digest,
                            "clock": plan["clock"], "retrospective_llm": replay and llm}
        return {"status": "declared_not_independently_approved", "fields": result}

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
        if self.manifest["schema_version"] == 2:
            if "open_at" not in sessions:
                raise ContractError("v2 snapshots require explicit open_at clocks")
            sessions["open_at"] = _timestamps(sessions["open_at"], "open_at")
            # Validate the whole calendar before slicing can hide a late decision.
            ResearchPanel(tuple(sessions.session), ("calendar",),
                          {"clock": np.zeros((len(sessions), 1))}, np.ones((len(sessions), 1), dtype=bool),
                          decision_at=tuple(sessions.decision_at), open_at=tuple(sessions.open_at))
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
        timed = self.manifest["schema_version"] == 2
        return ResearchPanel(dates, assets, values, eligible,
                             decision_at=tuple(sessions.decision_at) if timed else None,
                             open_at=tuple(sessions.open_at) if timed else None,
                             snapshot_hash=self.snapshot_hash,
                             provenance_json=json.dumps(self._feature_provenance(), sort_keys=True))
