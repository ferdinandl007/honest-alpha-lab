"""Allowlisted typed tools available to research subagents."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from threading import RLock
from typing import Mapping, Protocol

from .contracts import (
    AgentKind,
    AgentTrace,
    ContractError,
    ResearchJob,
    canonical_hash,
    require_number,
)
from .ledger import ImmutableLedger


class ToolName(str, Enum):
    PUBLIC_DOCUMENT_FETCH = "public_document_fetch"
    DATASET_CATALOG_SEARCH = "dataset_catalog_search"
    LICENSE_LOOKUP = "license_lookup"
    DATASET_SAMPLE_READ = "dataset_sample_read"
    POINT_IN_TIME_FEATURE_BUILD = "point_in_time_feature_build"
    ALPHA_LIBRARY_SIMILARITY = "alpha_library_similarity"


@dataclass(frozen=True, slots=True)
class ToolCall:
    run_id: str
    job_id: str
    agent_kind: AgentKind
    tool_name: ToolName
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.run_id or not self.job_id:
            raise ContractError("tool calls require a run and job id")


@dataclass(frozen=True, slots=True)
class ToolResult:
    output: Mapping[str, object]
    external_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        require_number(self.external_cost_usd, "tool cost")

    @property
    def output_hash(self) -> str:
        return canonical_hash(self.output)


class ResearchTool(Protocol):
    def execute(self, arguments: Mapping[str, object]) -> ToolResult:
        ...


class ToolPolicy:
    """Default deny policy; no registry or sealed-test operation is an agent tool."""

    _ALLOWED: dict[AgentKind, frozenset[ToolName]] = {
        AgentKind.SYMBOLIC_FACTOR: frozenset({ToolName.ALPHA_LIBRARY_SIMILARITY, ToolName.PUBLIC_DOCUMENT_FETCH}),
        AgentKind.TEXT_EVENT: frozenset({ToolName.DATASET_SAMPLE_READ, ToolName.POINT_IN_TIME_FEATURE_BUILD, ToolName.PUBLIC_DOCUMENT_FETCH}),
        AgentKind.ALTERNATIVE_DATA: frozenset(
            {
                ToolName.PUBLIC_DOCUMENT_FETCH,
                ToolName.DATASET_CATALOG_SEARCH,
                ToolName.LICENSE_LOOKUP,
                ToolName.DATASET_SAMPLE_READ,
                ToolName.POINT_IN_TIME_FEATURE_BUILD,
                ToolName.ALPHA_LIBRARY_SIMILARITY,
            }
        ),
        AgentKind.ALTERNATIVE_DATASET_CREATOR: frozenset(
            {
                ToolName.PUBLIC_DOCUMENT_FETCH,
                ToolName.DATASET_CATALOG_SEARCH,
                ToolName.LICENSE_LOOKUP,
                ToolName.DATASET_SAMPLE_READ,
                ToolName.POINT_IN_TIME_FEATURE_BUILD,
            }
        ),
        AgentKind.NUMERICAL_VALIDATOR: frozenset({ToolName.ALPHA_LIBRARY_SIMILARITY}),
        AgentKind.RESEARCH_ORCHESTRATOR: frozenset(),
    }

    def authorize(self, call: ToolCall) -> None:
        if call.tool_name not in self._ALLOWED[call.agent_kind]:
            raise PermissionError(
                f"{call.agent_kind.value} may not call {call.tool_name.value}"
            )

    def allowed_for(self, agent_kind: AgentKind) -> tuple[ToolName, ...]:
        return tuple(sorted(self._ALLOWED[agent_kind], key=lambda name: name.value))


class ToolRouter:
    """Routes only registered, typed tools and emits an immutable tool trace."""

    def __init__(self, policy: ToolPolicy | None = None) -> None:
        self._policy = policy or ToolPolicy()
        self._tools: dict[ToolName, ResearchTool] = {}
        self._jobs: dict[str, ResearchJob] = {}
        self._data_costs: dict[str, Decimal] = {}
        self._cost_bounds: dict[ToolName, Decimal] = {}
        self._blocked_jobs: set[str] = set()
        self._lock = RLock()
        self._traces: ImmutableLedger[AgentTrace] = ImmutableLedger()

    def register(self, name: ToolName, tool: ResearchTool, *, max_cost_usd: float) -> None:
        """Trusted deployment promises this bound for every call, including failures.

        Declare audited free adapters with max_cost_usd=0. This is an upper
        bound, not an estimate; an adapter must enforce it at its provider.
        Reservations are local to this in-memory router, not crash durable.
        """
        require_number(max_cost_usd, "tool maximum cost")
        with self._lock:
            if name in self._tools:
                raise ContractError(f"tool {name.value} is already registered")
            self._tools[name] = tool
            self._cost_bounds[name] = Decimal(str(max_cost_usd))

    def register_job(self, job: ResearchJob) -> None:
        job.budget.__post_init__()
        with self._lock:
            if job.job_id in self._jobs:
                raise ContractError("tool budget is already registered for this job")
            self._jobs[job.job_id] = job
            self._data_costs[job.job_id] = Decimal(0)

    def execute(self, call: ToolCall) -> ToolResult:
        self._policy.authorize(call)
        try:
            job = self._jobs[call.job_id]
        except KeyError as error:
            raise ContractError(
                "tool call references an unregistered job budget"
            ) from error
        if job.agent_kind != call.agent_kind:
            raise PermissionError(
                "tool call agent kind does not match its registered job"
            )
        try:
            tool = self._tools[call.tool_name]
        except KeyError as error:
            raise ContractError(
                f"tool {call.tool_name.value} is unavailable"
            ) from error
        arguments = deepcopy(dict(call.arguments))
        input_hash = canonical_hash(arguments)
        bound = self._cost_bounds[call.tool_name]
        with self._lock:
            if call.job_id in self._blocked_jobs:
                raise PermissionError("tool cost contract failed; job is blocked")
            next_cost = self._data_costs[call.job_id] + bound
            if next_cost > Decimal(str(job.budget.max_data_cost_usd)):
                raise PermissionError("tool call would exceed the immutable data-cost budget")
            self._data_costs[call.job_id] = next_cost

        def trace(event, output_hash):
            self._traces.append(AgentTrace(
                run_id=call.run_id, job_id=call.job_id, agent_kind=call.agent_kind,
                event_type=event, tool_name=call.tool_name.value,
                input_hash=input_hash, output_hash=output_hash))

        try:
            trace("tool_reserved", canonical_hash({"max_cost_usd": str(bound)}))
            result = tool.execute(arguments)
            if not isinstance(result, ToolResult):
                raise ContractError("tool must return ToolResult")
            result.__post_init__()
            actual = Decimal(str(result.external_cost_usd))
            if actual > bound:
                with self._lock:
                    self._data_costs[call.job_id] += actual - bound
                    self._blocked_jobs.add(call.job_id)
                raise ContractError("tool exceeded its trusted maximum cost; job is blocked")
            trace("tool_call", result.output_hash)
        except BaseException:
            # Unknown charges, malformed results and audit failures never refund.
            try:
                trace("tool_failed", canonical_hash({"reservation_retained": True}))
            except Exception:  # noqa: BLE001 -- retain original error and disable unauditable work
                with self._lock:
                    self._blocked_jobs.add(call.job_id)
            raise
        with self._lock:
            self._data_costs[call.job_id] -= bound - actual
        return result

    def traces(self):
        return self._traces.entries()

    def allowed_for(self, agent_kind: AgentKind) -> tuple[ToolName, ...]:
        return self._policy.allowed_for(agent_kind)

    def data_cost(self, job_id: str) -> float:
        with self._lock:
            return float(self._data_costs[job_id])

    def verify(self) -> bool:
        return self._traces.verify()
