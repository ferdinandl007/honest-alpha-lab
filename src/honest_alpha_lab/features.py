"""Deterministic event/proxy materialization, not dataset or alpha approval.

Agent output is structured data with verifiable source spans, never executable
code. The default clock includes collection and extraction latency. Retrospective
publication replay is a separate, explicitly labeled research experiment.
"""
from __future__ import annotations

import bisect
import io
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from typing import Literal

from .artifacts import LocalArtifactStore
from .contracts import ContractError, canonical_hash


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be nonempty text")


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError("evidence must reference a content-addressed artifact")


def _time(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ContractError("feature timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _date(value):
    if type(value) is not date:
        raise ContractError("feature dates must be dates, not timestamps")


@dataclass(frozen=True)
class EvidenceSpan:
    """Character offsets in the exact archived UTF-8 text, not byte offsets."""

    start: int
    end: int
    quote: str

    def __post_init__(self):
        if type(self.start) is not int or type(self.end) is not int or not 0 <= self.start < self.end:
            raise ContractError("evidence span offsets are invalid")
        _text(self.quote, "evidence quote")


@dataclass(frozen=True)
class SourceVintage:
    dataset_id: str
    source_key: str
    observation_id: str
    observed_on: date
    value: float | None  # None is an explicit withdrawal, not a missing row.
    published_at: datetime
    retrieved_at: datetime
    extracted_at: datetime
    source_hash: str
    publication_evidence_hash: str
    spans: tuple[EvidenceSpan, ...]
    extractor_id: str  # Version/config identity, including model/prompt for LLMs.
    extraction_kind: Literal["deterministic", "llm", "human"]

    def __post_init__(self):
        for name in ("dataset_id", "source_key", "observation_id", "extractor_id"):
            _text(getattr(self, name), name)
        _date(self.observed_on)
        for name in ("published_at", "retrieved_at", "extracted_at"):
            object.__setattr__(self, name, _time(getattr(self, name)))
        if not self.published_at <= self.retrieved_at <= self.extracted_at:
            raise ContractError("publication, retrieval and extraction clocks must be ordered")
        if self.observed_on > self.published_at.date():
            raise ContractError("observed date cannot follow publication")
        if self.value is not None:
            if type(self.value) not in (int, float) or not math.isfinite(self.value):
                raise ContractError("feature values must be finite numbers or explicit withdrawals")
            object.__setattr__(self, "value", float(self.value))
        for value in (self.source_hash, self.publication_evidence_hash):
            _digest(value)
        object.__setattr__(self, "spans", tuple(self.spans))
        if not self.spans or any(not isinstance(span, EvidenceSpan) for span in self.spans):
            raise ContractError("every extraction needs typed source evidence spans")
        if self.extraction_kind not in {"deterministic", "llm", "human"}:
            raise ContractError("unknown extraction kind")


@dataclass(frozen=True)
class ExposureVintage:
    """A version of an asset/source relationship; zero weight removes exposure.

    A later known termination replaces an earlier open-ended version. Relationship
    evidence must document both economic applicability and historical availability.
    These records do not authenticate a data steward or confer approval.
    """

    asset: str
    source_key: str
    valid_from: date
    valid_to: date | None
    available_at: datetime
    weight: float
    evidence_hash: str

    def __post_init__(self):
        _text(self.asset, "asset")
        _text(self.source_key, "source_key")
        _date(self.valid_from)
        if self.valid_to is not None:
            _date(self.valid_to)
            if self.valid_to < self.valid_from:
                raise ContractError("exposure validity interval is inverted")
        object.__setattr__(self, "available_at", _time(self.available_at))
        if type(self.weight) not in (int, float) or not math.isfinite(self.weight) or abs(self.weight) > 1:
            raise ContractError("exposure weights must be finite and in [-1, 1]")
        object.__setattr__(self, "weight", float(self.weight))
        _digest(self.evidence_hash)


@dataclass(frozen=True)
class FeatureSession:
    session: date
    decision_at: datetime

    def __post_init__(self):
        _date(self.session)
        object.__setattr__(self, "decision_at", _time(self.decision_at))


@dataclass(frozen=True)
class FeaturePlan:
    dataset_id: str
    field: str
    mode: Literal["event_sum", "latest_proxy"]
    max_age_days: int
    clock: Literal["as_run", "publication_replay"] = "as_run"
    lag_sessions: int = 0
    half_life_days: float | None = None
    allow_retrospective_llm: bool = False

    def __post_init__(self):
        _text(self.dataset_id, "dataset_id")
        if not isinstance(self.field, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", self.field):
            raise ContractError("feature field must be a DSL identifier")
        if self.mode not in {"event_sum", "latest_proxy"} or self.clock not in {"as_run", "publication_replay"}:
            raise ContractError("unknown feature mode or clock")
        for name in ("max_age_days", "lag_sessions"):
            if type(getattr(self, name)) is not int or not 0 <= getattr(self, name) <= 3650:
                raise ContractError("feature age/lag must be bounded nonnegative integers")
        if type(self.allow_retrospective_llm) is not bool:
            raise ContractError("retrospective LLM opt-in must be an explicit boolean")
        if self.half_life_days is not None and (
                type(self.half_life_days) not in (int, float)
                or not math.isfinite(self.half_life_days) or self.half_life_days <= 0
                or self.mode != "event_sum"):
            raise ContractError("positive finite half-life is only supported for events")


@dataclass(frozen=True)
class FeatureContribution:
    source_vintage_hash: str
    exposure_vintage_hash: str
    value: float


@dataclass(frozen=True)
class FeatureRow:
    session: date
    asset: str
    field: str
    value: float
    available_at: datetime
    source_row_id: str
    contributions: tuple[FeatureContribution, ...]


@dataclass(frozen=True)
class FeatureBuild:
    plan: FeaturePlan
    input_hash: str
    rows: tuple[FeatureRow, ...]
    input_manifest_json: str

    def observations_frame(self):
        """Exact-session long rows accepted by ParquetSnapshot, including empty builds."""
        import pandas as pd

        columns = ("session", "asset", "field", "value", "available_at", "source_row_id")
        frame = pd.DataFrame([{name: getattr(row, name) for name in columns} for row in self.rows],
                             columns=columns)
        frame["available_at"] = pd.to_datetime(frame["available_at"], utc=True)
        frame["value"] = frame["value"].astype(float)
        return frame

    def publish(self, store: LocalArtifactStore) -> dict:
        buffer = io.BytesIO()
        self.observations_frame().to_parquet(buffer, index=False)
        observations_hash = store.put(buffer.getvalue())
        manifest = {
            "schema_version": 1, "plan": asdict(self.plan), "input_hash": self.input_hash,
            "inputs": json.loads(self.input_manifest_json),
            "observations_hash": observations_hash, "row_count": len(self.rows),
            "row_provenance": [asdict(row) for row in self.rows],
            "independently_approved": False, "financial_alpha_verified": False,
            "retrospective_replay": self.plan.clock == "publication_replay",
        }
        content = json.dumps(manifest, default=_json_date, sort_keys=True, allow_nan=False).encode()
        return {"manifest_hash": store.put(content), "observations_hash": observations_hash,
                "row_count": len(self.rows), "independently_approved": False,
                "financial_alpha_verified": False}


def _json_date(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported feature JSON type: {type(value).__name__}")


def _exposures_at(exposures, effective_day, decision):
    latest = {}
    for exposure in exposures:
        if exposure.valid_from <= effective_day and exposure.available_at <= decision:
            key = exposure.asset, exposure.source_key
            prior = latest.get(key)
            if prior is None or (exposure.valid_from, exposure.available_at) > (prior.valid_from, prior.available_at):
                latest[key] = exposure
    # Filter AFTER version selection; terminated/zero relationships never revive.
    return tuple(item for _, item in sorted(latest.items()) if item.weight != 0
                 and (item.valid_to is None or effective_day <= item.valid_to))


def materialize_features(plan: FeaturePlan, sources: tuple[SourceVintage, ...],
                         exposures: tuple[ExposureVintage, ...],
                         sessions: tuple[FeatureSession, ...],
                         artifacts: LocalArtifactStore) -> FeatureBuild:
    """Build auditable features without approving inputs, mappings or candidates.

    Events sum distinct active events, using exposures effective on the observed
    date. Proxy levels use the latest observed period per source and exposures
    effective on the decision date. All active mapped sources are required for a
    proxy aggregate: missing/stale/withdrawn inputs cannot silently become zero.
    Neither mode normalizes exposure weights. Absence of events remains missing.
    """
    if not isinstance(plan, FeaturePlan):
        raise ContractError("a typed feature plan is required")
    sources, exposures, sessions = tuple(sources), tuple(exposures), tuple(sessions)
    for values, kind in ((sources, SourceVintage), (exposures, ExposureVintage), (sessions, FeatureSession)):
        if any(not isinstance(value, kind) for value in values):
            raise ContractError("feature compiler inputs must be typed records")
    if not sessions or not exposures:
        raise ContractError("feature builds need explicit sessions and exposure records")
    if any(a.session >= b.session or a.decision_at >= b.decision_at for a, b in pairwise(sessions)):
        raise ContractError("feature sessions and decisions must be strictly chronological")
    sources = tuple(sorted(sources, key=lambda item: (item.source_key, item.observation_id, item.published_at)))
    exposures = tuple(sorted(exposures, key=lambda item: (item.asset, item.source_key, item.valid_from, item.available_at)))
    identities, periods, vintage_keys, exposure_keys = {}, {}, set(), set()
    for source in sources:
        if source.dataset_id != plan.dataset_id:
            raise ContractError("source dataset does not match the feature plan")
        if source.extraction_kind == "llm" and plan.clock == "publication_replay" and not plan.allow_retrospective_llm:
            raise ContractError("retrospective LLM extraction requires explicit opt-in; it is not historical execution")
        key = source.source_key, source.observation_id
        if key in identities and identities[key] != source.observed_on:
            raise ContractError("a revised observation cannot change its observed date")
        identities[key] = source.observed_on
        period = source.source_key, source.observed_on
        if plan.mode == "latest_proxy" and periods.get(period, key) != key:
            raise ContractError("proxy series has ambiguous observations for one period")
        periods[period] = key
        vintage_key = (*key, source.published_at)
        if vintage_key in vintage_keys:
            raise ContractError("duplicate or ambiguous source vintage")
        vintage_keys.add(vintage_key)
        try:
            content = artifacts.get(source.source_hash).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractError("source span evidence must reference archived UTF-8 text") from exc
        artifacts.get(source.publication_evidence_hash)
        for span in source.spans:
            if span.end > len(content) or content[span.start:span.end] != span.quote:
                raise ContractError("source evidence span does not match archived text")
    for exposure in exposures:
        key = exposure.asset, exposure.source_key, exposure.valid_from, exposure.available_at
        if key in exposure_keys:
            raise ContractError("ambiguous exposure vintage")
        exposure_keys.add(key)
        artifacts.get(exposure.evidence_hash)
    source_hashes = {id(item): canonical_hash(item) for item in sources}
    exposure_hashes = {id(item): canonical_hash(item) for item in exposures}
    decisions = [item.decision_at for item in sessions]
    eligible_from = {}
    for source in sources:
        clock = source.extracted_at if plan.clock == "as_run" else source.published_at
        # A truncated calendar cannot determine how many lag sessions have passed.
        if plan.lag_sessions and clock < decisions[0]:
            raise ContractError("lagged builds require calendar warm-up before each source clock")
        eligible_from[id(source)] = bisect.bisect_left(decisions, clock) + plan.lag_sessions
    rows = []
    for index, session in enumerate(sessions):
        latest = {}
        for source in sources:
            if eligible_from[id(source)] <= index:
                key = source.source_key, source.observation_id
                prior = latest.get(key)
                if prior is None or source.published_at > prior.published_at:
                    latest[key] = source
        if plan.mode == "latest_proxy":
            by_key = {}
            for source in latest.values():
                prior = by_key.get(source.source_key)
                if prior is not None and prior.observed_on == source.observed_on:
                    raise ContractError("proxy series has ambiguous observations for one period")
                if prior is None or source.observed_on > prior.observed_on:
                    by_key[source.source_key] = source
            latest = by_key
        active = [source for source in latest.values() if source.value is not None
                  and 0 <= (session.session - source.observed_on).days <= plan.max_age_days]
        contributions, actual_keys = {}, {}
        current_exposures = _exposures_at(exposures, session.session, session.decision_at)
        for source in active:
            relevant = current_exposures if plan.mode == "latest_proxy" else _exposures_at(
                exposures, source.observed_on, session.decision_at)
            age = (session.session - source.observed_on).days
            decay = 1.0 if plan.half_life_days is None else 2.0 ** (-age / plan.half_life_days)
            for exposure in relevant:
                if exposure.source_key == source.source_key:
                    value = source.value * exposure.weight * decay
                    contributions.setdefault(exposure.asset, []).append(FeatureContribution(
                        source_hashes[id(source)], exposure_hashes[id(exposure)], value))
                    actual_keys.setdefault(exposure.asset, set()).add(source.source_key)
        expected_keys = {}
        for exposure in current_exposures:
            expected_keys.setdefault(exposure.asset, set()).add(exposure.source_key)
        for asset, items in sorted(contributions.items()):
            if plan.mode == "latest_proxy" and actual_keys[asset] != expected_keys[asset]:
                continue
            items = tuple(sorted(items, key=lambda item: (item.source_vintage_hash, item.exposure_vintage_hash)))
            try:
                value = math.fsum(item.value for item in items)
            except OverflowError as exc:
                raise ContractError("aggregated feature overflow") from exc
            if not math.isfinite(value):
                raise ContractError("aggregated feature must be finite")
            identity = {"plan": plan, "session": session, "asset": asset, "contributions": items}
            rows.append(FeatureRow(session.session, asset, plan.field, value,
                                   session.decision_at, canonical_hash(identity), items))
    manifest = {"schema_version": 1, "sources": [asdict(item) for item in sources],
                "exposures": [asdict(item) for item in exposures],
                "sessions": [asdict(item) for item in sessions], "plan": asdict(plan)}
    return FeatureBuild(plan, canonical_hash(manifest), tuple(rows),
                        json.dumps(manifest, default=_json_date, sort_keys=True, allow_nan=False))


def build_from_payload(payload: dict, artifacts: LocalArtifactStore) -> FeatureBuild:
    """Strict file-tool boundary: JSON data only, with no generated code or commands."""
    if not isinstance(payload, dict) or set(payload) != {"plan", "sources", "exposures", "sessions"}:
        raise ContractError("feature request must contain plan, sources, exposures and sessions")
    try:
        sources = []
        for raw in payload["sources"]:
            item = dict(raw)
            item["observed_on"] = date.fromisoformat(item["observed_on"])
            for name in ("published_at", "retrieved_at", "extracted_at"):
                item[name] = datetime.fromisoformat(item[name])
            item["spans"] = tuple(EvidenceSpan(**span) for span in item["spans"])
            sources.append(SourceVintage(**item))
        exposures = []
        for raw in payload["exposures"]:
            item = dict(raw)
            item["valid_from"] = date.fromisoformat(item["valid_from"])
            item["valid_to"] = date.fromisoformat(item["valid_to"]) if item["valid_to"] is not None else None
            item["available_at"] = datetime.fromisoformat(item["available_at"])
            exposures.append(ExposureVintage(**item))
        sessions = tuple(FeatureSession(date.fromisoformat(item["session"]),
                                        datetime.fromisoformat(item["decision_at"]))
                         for item in payload["sessions"] if _session_keys(item))
        return materialize_features(FeaturePlan(**payload["plan"]), tuple(sources),
                                    tuple(exposures), sessions, artifacts)
    except (TypeError, KeyError, ValueError) as exc:
        raise ContractError(f"invalid feature request: {exc}") from exc


def _session_keys(item):
    if not isinstance(item, dict) or set(item) != {"session", "decision_at"}:
        raise ContractError("session fields must be exactly session and decision_at")
    return True


class EvidenceFeatureBuildTool:
    """Agent-callable compiler with service-configured roots, not agent paths.

    Register as POINT_IN_TIME_FEATURE_BUILD. Output is an unapproved research
    artifact. This tool never modifies a snapshot, a plan, or registry status.
    """

    def __init__(self, source_artifacts: LocalArtifactStore, output_artifacts: LocalArtifactStore):
        self.source_artifacts = source_artifacts
        self.output_artifacts = output_artifacts

    def execute(self, arguments):
        from .tools import ToolResult

        return ToolResult(build_from_payload(arguments, self.source_artifacts).publish(self.output_artifacts))
