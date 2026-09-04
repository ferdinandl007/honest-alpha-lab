"""Open-code subagent execution contracts and an in-process reference runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol
from uuid import uuid4

from .agents import ProposalAgent
from .contracts import (
    AgentKind,
    AgentRunStatus,
    AgentTrace,
    AlphaCandidate,
    ContractError,
    ResearchJob,
    canonical_hash,
)
from .ledger import AlphaRegistry, ImmutableLedger
from .orchestration import JobUsage, ResearchQueue
from .prompts import PromptTemplate, get_prompt
from .tools import ToolRouter


@dataclass(frozen=True, slots=True)
class AgentTask:
    task_id: str
    job: ResearchJob
    prompt_name: str
    context: Mapping[str, object]

    @classmethod
    def new(
        cls, job: ResearchJob, prompt_name: str, context: Mapping[str, object]
    ) -> "AgentTask":
        prompt = get_prompt(prompt_name)
        if prompt.agent_kind != job.agent_kind:
            raise ContractError("task prompt and job agent kind must match")
        return cls(str(uuid4()), job, prompt_name, dict(context))

    @property
    def context_hash(self) -> str:
        return canonical_hash(self.context)


@dataclass(frozen=True, slots=True)
class SubagentResult:
    alpha_candidates: tuple[AlphaCandidate, ...] = ()
    output: Mapping[str, object] = field(default_factory=dict)
    usage: JobUsage = field(default_factory=JobUsage)

    @property
    def output_hash(self) -> str:
        return canonical_hash(
            {
                "candidate_ids": tuple(
                    candidate.candidate_id for candidate in self.alpha_candidates
                ),
                "output": self.output,
                "usage": self.usage,
            }
        )


@dataclass(frozen=True, slots=True)
class AgentRun:
    run_id: str
    task_id: str
    status: AgentRunStatus
    result_hash: str | None = None
    error: str | None = None


class SubagentWorker(Protocol):
    kind: AgentKind

    def run(
        self, task: AgentTask, prompt: PromptTemplate, tools: ToolRouter
    ) -> SubagentResult:
        ...


class LocalProposalWorker:
    """Deterministic local worker used in tests and simple scripted research jobs."""

    def __init__(self, proposal_agent: ProposalAgent) -> None:
        self.kind = proposal_agent.kind
        self._proposal_agent = proposal_agent

    def run(
        self, task: AgentTask, prompt: PromptTemplate, tools: ToolRouter
    ) -> SubagentResult:
        del prompt, tools
        proposals = self._proposal_agent.propose(task.job, task.context)
        return SubagentResult(
            alpha_candidates=proposals,
            output={"worker": "local-proposal", "proposal_count": len(proposals)},
            usage=JobUsage(trials=len(proposals)),
        )


class DelegatingSubagentWorker:
    """Adapter seam for Codex/OpenCode or another subagent provider.

    The caller supplies the provider-specific dispatcher. The adapter receives a fully
    rendered, versioned prompt and returns typed proposals; it never receives a registry
    transition or sealed-test capability.
    """

    def __init__(self, kind: AgentKind, dispatcher) -> None:
        self.kind = kind
        self._dispatcher = dispatcher

    def run(
        self, task: AgentTask, prompt: PromptTemplate, tools: ToolRouter
    ) -> SubagentResult:
        response = self._dispatcher(
            prompt.render(task.context),
            {
                "task_id": task.task_id,
                "job_id": task.job.job_id,
                "snapshot_hash": task.job.input_snapshot_hash,
                "allowed_tools": tuple(
                    name.value for name in tools.allowed_for(self.kind)
                ),
            },
        )
        if not isinstance(response, SubagentResult):
            raise ContractError("subagent dispatcher must return SubagentResult")
        return response


class SubagentOrchestrator:
    """Queues and traces workers while publishing proposed alphas to the shared registry."""

    def __init__(
        self,
        registry: AlphaRegistry,
        queue: ResearchQueue | None = None,
        tools: ToolRouter | None = None,
    ) -> None:
        self._registry = registry
        self._queue = queue or ResearchQueue()
        self._tools = tools or ToolRouter()
        self._workers: dict[AgentKind, SubagentWorker] = {}
        self._tasks: dict[str, AgentTask] = {}
        self._runs: list[AgentRun] = []
        self._traces: ImmutableLedger[AgentTrace] = ImmutableLedger()

    def register_worker(self, worker: SubagentWorker) -> None:
        if worker.kind in self._workers:
            raise ContractError(f"worker already registered for {worker.kind.value}")
        self._workers[worker.kind] = worker

    def submit(self, task: AgentTask) -> None:
        if task.job.job_id in self._tasks:
            raise ContractError("research job is already bound to an immutable task")
        self._tasks[task.job.job_id] = task
        self._tools.register_job(task.job)
        self._queue.submit(task.job)

    def run_next(self) -> AgentRun | None:
        job = self._queue.claim()
        if job is None:
            return None
        task = self._tasks[job.job_id]
        run_id = str(uuid4())
        self._traces.append(
            AgentTrace(
                run_id=run_id,
                job_id=job.job_id,
                agent_kind=job.agent_kind,
                event_type="run_started",
                input_hash=canonical_hash(
                    {"task": task, "prompt": get_prompt(task.prompt_name)}
                ),
                output_hash=canonical_hash({"status": AgentRunStatus.RUNNING}),
            )
        )
        try:
            worker = self._workers[job.agent_kind]
            result = worker.run(task, get_prompt(task.prompt_name), self._tools)
            total_usage = JobUsage(
                trials=result.usage.trials,
                runtime_seconds=result.usage.runtime_seconds,
                data_cost_usd=result.usage.data_cost_usd
                + self._tools.data_cost(job.job_id),
                agent_tokens=result.usage.agent_tokens,
            )
            self._queue.record_usage(job, total_usage)
            for candidate in result.alpha_candidates:
                if candidate.agent_kind != job.agent_kind:
                    raise ContractError(
                        "worker returned a candidate from another agent kind"
                    )
                if candidate.input_snapshot_hash != job.input_snapshot_hash:
                    raise ContractError(
                        "worker candidate does not match the job input snapshot"
                    )
                self._registry.register(candidate)
            run = AgentRun(
                run_id, task.task_id, AgentRunStatus.COMPLETED, result.output_hash
            )
            self._traces.append(
                AgentTrace(
                    run_id=run_id,
                    job_id=job.job_id,
                    agent_kind=job.agent_kind,
                    event_type="run_completed",
                    input_hash=task.context_hash,
                    output_hash=result.output_hash,
                )
            )
        except Exception as error:
            run = AgentRun(
                run_id, task.task_id, AgentRunStatus.FAILED, error=str(error)
            )
            self._traces.append(
                AgentTrace(
                    run_id=run_id,
                    job_id=job.job_id,
                    agent_kind=job.agent_kind,
                    event_type="run_failed",
                    input_hash=task.context_hash,
                    output_hash=canonical_hash({"error": str(error)}),
                )
            )
        self._runs.append(run)
        return run

    def runs(self) -> tuple[AgentRun, ...]:
        return tuple(self._runs)

    def traces(self):
        return self._traces.entries()

    def verify(self) -> bool:
        return self._traces.verify() and self._tools.verify()
