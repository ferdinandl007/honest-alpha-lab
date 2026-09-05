"""Queued research jobs with explicit budget accounting."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from .contracts import ContractError, ResearchJob, require_count, require_number


@dataclass(frozen=True, slots=True)
class JobUsage:
    """Cumulative usage; worker data cost excludes separately metered router calls."""

    trials: int = 0
    runtime_seconds: float = 0
    data_cost_usd: float = 0.0
    agent_tokens: int = 0

    def __post_init__(self) -> None:
        require_count(self.trials, "trials")
        require_count(self.agent_tokens, "agent_tokens")
        require_number(self.runtime_seconds, "runtime_seconds")
        require_number(self.data_cost_usd, "data_cost_usd")


def validate_usage(job: ResearchJob, usage: JobUsage) -> None:
    if not isinstance(usage, JobUsage):
        raise ContractError("job usage must be JobUsage")
    usage.__post_init__()
    job.budget.__post_init__()
    budget = job.budget
    if (usage.trials > budget.max_trials
            or usage.runtime_seconds > budget.max_runtime_seconds
            or usage.data_cost_usd > budget.max_data_cost_usd
            or usage.agent_tokens > budget.max_agent_tokens):
        raise ContractError("research job exceeded its immutable budget")


class ResearchQueue:
    """Minimal queue reference implementation; workers receive immutable jobs."""

    def __init__(self) -> None:
        self._pending: list[ResearchJob] = []
        self._usage: dict[str, JobUsage] = {}
        self._jobs: dict[str, ResearchJob] = {}
        self._lock = RLock()

    def submit(self, job: ResearchJob) -> None:
        with self._lock:
            if job.job_id in self._usage:
                raise ContractError("job ids are immutable and cannot be reused")
            self._pending.append(job)
            self._usage[job.job_id] = JobUsage()
            self._jobs[job.job_id] = job

    def claim(self) -> ResearchJob | None:
        with self._lock:
            return self._pending.pop(0) if self._pending else None

    def record_usage(self, job: ResearchJob, usage: JobUsage) -> None:
        with self._lock:
            if job.job_id not in self._usage:
                raise KeyError(job.job_id)
            if job != self._jobs[job.job_id]:
                raise ContractError("usage references a different immutable job")
            try:
                validate_usage(self._jobs[job.job_id], usage)
            except ContractError as error:
                raise PermissionError(str(error)) from error
            previous = self._usage[job.job_id]
            if any(getattr(usage, name) < getattr(previous, name) for name in
                   ("trials", "runtime_seconds", "data_cost_usd", "agent_tokens")):
                raise ContractError("cumulative job usage cannot decrease")
            self._usage[job.job_id] = usage

    def usage(self, job_id: str) -> JobUsage:
        with self._lock:
            return self._usage[job_id]
