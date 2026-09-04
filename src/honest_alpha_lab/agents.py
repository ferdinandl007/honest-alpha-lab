"""Typed seams for agent-driven proposal generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, Sequence
from uuid import uuid4

from .contracts import (
    AgentKind,
    AlphaCandidate,
    ContractError,
    DataLineage,
    InputSnapshot,
    ResearchBudget,
    ResearchJob,
    canonical_hash,
)
from .dsl import Formula


class ProposalAgent(Protocol):
    kind: AgentKind

    def propose(
        self, job: ResearchJob, context: Mapping[str, object]
    ) -> tuple[AlphaCandidate, ...]:
        ...


def make_job(
    kind: AgentKind,
    snapshot: InputSnapshot,
    budget: ResearchBudget,
    prompt: str,
    requested_by: str = "codex",
) -> ResearchJob:
    return ResearchJob(
        str(uuid4()),
        kind,
        snapshot.snapshot_hash,
        budget,
        canonical_hash(prompt),
        requested_by,
    )


class SymbolicFactorAgent:
    kind = AgentKind.SYMBOLIC_FACTOR

    def propose(
        self, job: ResearchJob, context: Mapping[str, object]
    ) -> tuple[AlphaCandidate, ...]:
        formulas = context.get("formulas", ())
        if not isinstance(formulas, Sequence) or isinstance(formulas, (str, bytes)):
            raise ContractError("symbolic context must provide a sequence of formulas")
        lineage_ids = _lineage_ids(context)
        candidates = []
        for expression in formulas:
            formula = Formula.parse(str(expression))
            candidates.append(
                AlphaCandidate.new(
                    self.kind,
                    f"symbolic:{formula.formula_hash[:12]}",
                    {"dsl": formula.expression, "formula_hash": formula.formula_hash},
                    job.input_snapshot_hash,
                    lineage_ids,
                )
            )
        return tuple(candidates)


@dataclass(frozen=True, slots=True)
class EventSignal:
    event_id: str
    asset: str
    event_type: str
    event_time: datetime
    source_time: datetime
    polarity: float
    magnitude: float
    source_lineage_id: str

    def __post_init__(self) -> None:
        if self.event_time.tzinfo is None or self.source_time.tzinfo is None:
            raise ContractError("event timestamps must be timezone-aware")
        if self.source_time < self.event_time:
            raise ContractError("source publication time cannot predate the event time")
        if not -1 <= self.polarity <= 1 or self.magnitude < 0:
            raise ContractError(
                "event polarity must be in [-1, 1] and magnitude non-negative"
            )


class TextEventAgent:
    kind = AgentKind.TEXT_EVENT

    def propose(
        self, job: ResearchJob, context: Mapping[str, object]
    ) -> tuple[AlphaCandidate, ...]:
        events = context.get("events", ())
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise ContractError(
                "text context must provide structured events, not raw executable code"
            )
        if not events:
            return ()
        lineage_ids = _lineage_ids(context)
        for event in events:
            if not isinstance(event, EventSignal):
                raise ContractError(
                    "text agent accepts only validated EventSignal objects"
                )
            if event.source_lineage_id not in lineage_ids:
                raise ContractError(
                    "event source lineage is not present in the job context"
                )
        types = sorted({event.event_type for event in events})
        return (
            AlphaCandidate.new(
                self.kind,
                "event:" + ":".join(types),
                {
                    "event_types": types,
                    "feature": "polarity_x_magnitude",
                    "event_count": len(events),
                },
                job.input_snapshot_hash,
                lineage_ids,
            ),
        )


@dataclass(frozen=True, slots=True)
class AlternativeHypothesis:
    name: str
    economic_mechanism: str
    dataset_id: str
    provider: str
    license_id: str
    publication_lag_days: int
    feature_definition: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.name,
                self.economic_mechanism,
                self.dataset_id,
                self.provider,
                self.license_id,
                self.feature_definition,
            )
        ):
            raise ContractError(
                "alternative hypotheses require mechanism and provenance fields"
            )
        if self.publication_lag_days < 0:
            raise ContractError("publication lag cannot be negative")


class AlternativeDataAgent:
    kind = AgentKind.ALTERNATIVE_DATA

    def propose(
        self, job: ResearchJob, context: Mapping[str, object]
    ) -> tuple[AlphaCandidate, ...]:
        hypotheses = context.get("hypotheses", ())
        if not isinstance(hypotheses, Sequence) or isinstance(hypotheses, (str, bytes)):
            raise ContractError(
                "alternative-data context must provide typed hypotheses"
            )
        lineage_ids = _lineage_ids(context)
        candidates = []
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, AlternativeHypothesis):
                raise ContractError(
                    "alternative-data proposals require validated licensing metadata"
                )
            if hypothesis.dataset_id not in lineage_ids:
                raise ContractError(
                    "alternative dataset must have matching licensed lineage in the job context"
                )
            candidates.append(
                AlphaCandidate.new(
                    self.kind,
                    f"alternative:{hypothesis.name}",
                    {
                        "mechanism": hypothesis.economic_mechanism,
                        "dataset_id": hypothesis.dataset_id,
                        "provider": hypothesis.provider,
                        "license_id": hypothesis.license_id,
                        "publication_lag_days": hypothesis.publication_lag_days,
                        "feature_definition": hypothesis.feature_definition,
                    },
                    job.input_snapshot_hash,
                    lineage_ids,
                )
            )
        return tuple(candidates)


def _lineage_ids(context: Mapping[str, object]) -> tuple[str, ...]:
    lineages = context.get("lineages", ())
    if not isinstance(lineages, Sequence) or isinstance(lineages, (str, bytes)):
        raise ContractError(
            "agent context must include a sequence of DataLineage objects"
        )
    ids = []
    for lineage in lineages:
        if not isinstance(lineage, DataLineage):
            raise ContractError("agent context contains invalid lineage")
        ids.append(lineage.dataset_id)
    if not ids:
        raise ContractError("agent context must include at least one lineage")
    return tuple(ids)
