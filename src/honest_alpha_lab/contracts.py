"""Frozen contracts shared by agents, numerical services, and storage adapters."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Mapping, Sequence
from uuid import uuid4


class AgentKind(str, Enum):
    RESEARCH_ORCHESTRATOR = "research_orchestrator"
    SYMBOLIC_FACTOR = "symbolic_factor"
    TEXT_EVENT = "text_event"
    ALTERNATIVE_DATA = "alternative_data"
    ALTERNATIVE_DATASET_CREATOR = "alternative_dataset_creator"
    NUMERICAL_VALIDATOR = "numerical_validator"
    STRATEGY_BUILDER = "strategy_builder"


class AlphaStatus(str, Enum):
    PROPOSED = "proposed"
    SCREENED = "screened"
    VALIDATED = "validated"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RETIRED = "retired"


class TrialStatus(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ContractError(ValueError):
    """Raised when a trust-boundary contract is invalid."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_hash(value: Any) -> str:
    """Hash JSON-compatible data deterministically for lineage and tamper evidence."""

    def normalize(item: Any) -> Any:
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        if hasattr(item, "__dataclass_fields__"):
            return normalize(asdict(item))
        if isinstance(item, Mapping):
            return {
                str(k): normalize(v)
                for k, v in sorted(item.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(item, (list, tuple)):
            return [normalize(v) for v in item]
        return item

    encoded = json.dumps(
        normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DataLineage:
    dataset_id: str
    provider: str
    license_id: str
    source_uri: str
    retrieval_time: datetime
    content_hash: str
    schema_version: str
    point_in_time: bool = True

    def __post_init__(self) -> None:
        if not self.dataset_id or not self.provider or not self.license_id:
            raise ContractError(
                "dataset, provider, and license identifiers are required"
            )
        if not self.source_uri or not self.content_hash or not self.schema_version:
            raise ContractError(
                "lineage requires source, content hash, and schema version"
            )
        if self.retrieval_time.tzinfo is None:
            raise ContractError("retrieval_time must be timezone-aware")
        if not self.point_in_time:
            raise ContractError(
                "only point-in-time datasets may enter the research pipeline"
            )


@dataclass(frozen=True, slots=True)
class InputSnapshot:
    dataset_hashes: tuple[str, ...]
    universe_hash: str
    code_hash: str
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.dataset_hashes or any(not item for item in self.dataset_hashes):
            raise ContractError("an input snapshot must name at least one dataset hash")
        if not self.universe_hash or not self.code_hash:
            raise ContractError("input snapshots require universe and code hashes")
        if self.created_at.tzinfo is None:
            raise ContractError("created_at must be timezone-aware")

    @property
    def snapshot_hash(self) -> str:
        return canonical_hash(self)


@dataclass(frozen=True, slots=True)
class ResearchBudget:
    max_trials: int = 100
    max_runtime_seconds: int = 3600
    max_data_cost_usd: float = 0.0
    max_agent_tokens: int = 100_000

    def __post_init__(self) -> None:
        if (
            self.max_trials <= 0
            or self.max_runtime_seconds <= 0
            or self.max_agent_tokens <= 0
        ):
            raise ContractError("research budgets must be positive")
        if self.max_data_cost_usd < 0:
            raise ContractError("data cost cannot be negative")


@dataclass(frozen=True, slots=True)
class EvaluationPolicy:
    horizons: tuple[int, ...] = (5, 10, 20, 60)
    purge_days: int = 60
    embargo_days: int = 5
    min_train_days: int = 252
    min_test_days: int = 63
    universe_id: str = "liquid_us_large_mid_cap"
    residual_target: str = "stock_return_minus_sector_minus_market_beta"

    def __post_init__(self) -> None:
        if not self.horizons or any(h <= 0 for h in self.horizons):
            raise ContractError("horizons must be positive")
        if self.purge_days < max(self.horizons) or self.embargo_days < 0:
            raise ContractError("purge_days must cover the longest forward label")
        if self.min_train_days <= 0 or self.min_test_days <= 0:
            raise ContractError("evaluation windows must be positive")


@dataclass(frozen=True, slots=True)
class AlphaCandidate:
    candidate_id: str
    agent_kind: AgentKind
    name: str
    specification: Mapping[str, Any]
    input_snapshot_hash: str
    lineage_ids: tuple[str, ...]
    created_at: datetime = field(default_factory=utc_now)
    status: AlphaStatus = AlphaStatus.PROPOSED

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.name or not self.input_snapshot_hash:
            raise ContractError("alpha candidates require id, name, and input snapshot")
        if not self.lineage_ids:
            raise ContractError("alpha candidates require data lineage")
        if self.created_at.tzinfo is None:
            raise ContractError("created_at must be timezone-aware")

    @staticmethod
    def new(
        agent_kind: AgentKind,
        name: str,
        specification: Mapping[str, Any],
        input_snapshot_hash: str,
        lineage_ids: Sequence[str],
    ) -> "AlphaCandidate":
        return AlphaCandidate(
            candidate_id=str(uuid4()),
            agent_kind=agent_kind,
            name=name,
            specification=dict(specification),
            input_snapshot_hash=input_snapshot_hash,
            lineage_ids=tuple(lineage_ids),
        )


@dataclass(frozen=True, slots=True)
class TrialRecord:
    trial_id: str
    candidate_id: str
    agent_kind: AgentKind
    snapshot_hash: str
    policy_hash: str
    status: TrialStatus
    metrics: Mapping[str, float] = field(default_factory=dict)
    started_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.trial_id
            or not self.candidate_id
            or not self.snapshot_hash
            or not self.policy_hash
        ):
            raise ContractError(
                "trial records require ids and immutable input/policy hashes"
            )
        if self.started_at.tzinfo is None or (
            self.completed_at and self.completed_at.tzinfo is None
        ):
            raise ContractError("trial timestamps must be timezone-aware")
        if self.status == TrialStatus.COMPLETED and self.completed_at is None:
            raise ContractError("completed trials require completed_at")
        if self.status == TrialStatus.FAILED and not self.error:
            raise ContractError("failed trials require an error")


@dataclass(frozen=True, slots=True)
class RegistryEvent:
    candidate_id: str
    from_status: AlphaStatus | None
    to_status: AlphaStatus
    actor: str
    reason: str
    event_hash: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ResearchJob:
    job_id: str
    agent_kind: AgentKind
    input_snapshot_hash: str
    budget: ResearchBudget
    prompt_hash: str
    requested_by: str
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.job_id or not self.input_snapshot_hash or not self.prompt_hash:
            raise ContractError(
                "research jobs require immutable prompt and snapshot hashes"
            )
        if not self.requested_by:
            raise ContractError("research jobs require an initiating actor")


@dataclass(frozen=True, slots=True)
class AgentTrace:
    """Immutable audit record for an agent invocation or approved tool call."""

    run_id: str
    job_id: str
    agent_kind: AgentKind
    event_type: str
    input_hash: str
    output_hash: str
    tool_name: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not all(
            (
                self.run_id,
                self.job_id,
                self.event_type,
                self.input_hash,
                self.output_hash,
            )
        ):
            raise ContractError(
                "agent traces require run, job, event, input, and output hashes"
            )
        if self.created_at.tzinfo is None:
            raise ContractError("agent trace timestamps must be timezone-aware")
