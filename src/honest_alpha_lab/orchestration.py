"""Queued research jobs with explicit budget accounting."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from .contracts import ContractError, ResearchJob


@dataclass(frozen=True, slots=True)
class JobUsage:
    trials: int = 0
    runtime_seconds: int = 0
    data_cost_usd: float = 0.0
    agent_tokens: int = 0


class ResearchQueue:
    """Minimal queue reference implementation; workers receive immutable jobs."""

    def __init__(self) -> None:
        self._pending: list[ResearchJob] = []
        self._usage: dict[str, JobUsage] = {}
        self._lock = RLock()

    def submit(self, job: ResearchJob) -> None:
        with self._lock:
            if job.job_id in self._usage:
                raise ContractError("job ids are immutable and cannot be reused")
            self._pending.append(job)
            self._usage[job.job_id] = JobUsage()

    def claim(self) -> ResearchJob | None:
        with self._lock:
            return self._pending.pop(0) if self._pending else None

    def record_usage(self, job: ResearchJob, usage: JobUsage) -> None:
        if (
            usage.trials < 0
            or usage.runtime_seconds < 0
            or usage.data_cost_usd < 0
            or usage.agent_tokens < 0
        ):
            raise ContractError("job usage cannot be negative")
        budget = job.budget
        if (
            usage.trials > budget.max_trials
            or usage.runtime_seconds > budget.max_runtime_seconds
            or usage.data_cost_usd > budget.max_data_cost_usd
            or usage.agent_tokens > budget.max_agent_tokens
        ):
            raise PermissionError("research job exceeded its immutable budget")
        with self._lock:
            if job.job_id not in self._usage:
                raise KeyError(job.job_id)
            self._usage[job.job_id] = usage

    def usage(self, job_id: str) -> JobUsage:
        return self._usage[job_id]
