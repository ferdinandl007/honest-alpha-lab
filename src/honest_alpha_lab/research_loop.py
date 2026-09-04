"""Diversity-aware multi-run orchestration built on the immutable research vault."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from uuid import uuid4

from .contracts import AlphaCandidate, ContractError, ResearchJob
from .ledger import AlphaRegistry
from .research_vault import ResearchVault, VaultRound
from .subagents import AgentRun, AgentTask, SubagentOrchestrator


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    job: ResearchJob
    prompt_name: str
    context: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ResearchRoundResult:
    vault_round: VaultRound
    runs: tuple[AgentRun, ...]
    candidates: tuple[AlphaCandidate, ...]


class DiversityResearchLoop:
    """Creates diverse proposal runs and records each attempt before the next round."""

    def __init__(
        self,
        orchestrator: SubagentOrchestrator,
        registry: AlphaRegistry,
        vault: ResearchVault,
    ) -> None:
        self._orchestrator = orchestrator
        self._registry = registry
        self._vault = vault

    def run_round(self, requests: Sequence[ResearchRequest], round_id: str | None = None) -> ResearchRoundResult:
        if not requests:
            raise ContractError("a research round needs at least one request")
        tasks = tuple(
            AgentTask.new(
                request.job,
                request.prompt_name,
                {**request.context, "shared_research_memory": self._vault.shared_context()},
            )
            for request in requests
        )
        before = {candidate.candidate_id for candidate in self._registry.all()}
        for task in tasks:
            self._orchestrator.submit(task)
        runs: list[AgentRun] = []
        while (run := self._orchestrator.run_next()) is not None:
            runs.append(run)
        candidates = tuple(
            candidate
            for candidate in self._registry.all()
            if candidate.candidate_id not in before
        )
        vault_round = self._vault.record_round(round_id or str(uuid4()), tasks, runs, candidates)
        return ResearchRoundResult(vault_round, tuple(runs), candidates)


@dataclass(frozen=True, slots=True)
class EvaluatedResearchRound:
    proposals: ResearchRoundResult
    submissions: tuple[Mapping[str, object], ...]
    numerical_jobs: tuple[Mapping[str, object], ...]
    measured_feedback: tuple[Mapping[str, object], ...]
    outstanding_trials: tuple[str, ...]


class QueuedResearchLoop:
    """Single-coordinator proposal rounds with persistent numerical feedback.

    The underlying proposal runner may use a CLI worker. Database sessions never
    enter its task context. Numerical workers can run concurrently, but one
    coordinator must own round ordering; a distributed coordinator lease remains
    a separate deployment requirement. Call drain() to resume evaluations without
    regenerating a completed proposal round after an interruption.
    """

    def __init__(self, proposals: DiversityResearchLoop, proposer_store, numerical_service,
                 artifacts, run_id: str):
        self.proposals = proposals
        self.proposer_store = proposer_store
        self.numerical_service = numerical_service
        self.artifacts = artifacts
        self.run_id = run_id

    def _outstanding(self) -> tuple[str, ...]:
        from uuid import UUID

        pending = []
        after = 0
        while records := self.proposer_store.records(self.run_id, after_sequence=after):
            after = records[-1]["sequence"]
            for record in records:
                envelope = record["envelope"]
                if envelope["kind"] == "trial_reserved":
                    trial_id = envelope["payload"]["trial_id"]
                    trial = self.proposer_store.get_trial(UUID(trial_id))
                    if trial["state"] not in {"completed", "failed"}:
                        pending.append(trial_id)
        return tuple(pending)

    def drain(self):
        """Bounded recovery of already reserved work; never rerun proposal agents."""
        run = self.proposer_store.get_run(self.run_id)
        if not run:
            raise ContractError("unknown research run")
        outcomes = []
        for _ in range(run["max_trials"] * run["max_attempts"]):
            try:
                result = self.numerical_service.process_one(self.run_id)
            except (TimeoutError, RuntimeError) as exc:
                outcomes.append({"status": "execution_failed", "kind": type(exc).__name__})
                continue
            if result is None:
                break
            outcomes.append(result)
        return tuple(outcomes)

    def run_round(self, requests: Sequence[ResearchRequest], *, round_id: str) -> EvaluatedResearchRound:
        import psycopg

        from .pipeline import _plan_from_run, development_feedback, submit_formula

        if not isinstance(round_id, str) or not round_id or len(round_id) > 100:
            raise ContractError("round id must contain 1 to 100 characters")
        if self._outstanding():
            raise ContractError("finish or recover pending numerical trials before a new adaptive round")
        plan, _ = _plan_from_run(self.proposer_store.get_run(self.run_id))
        if any(request.job.input_snapshot_hash != plan.snapshot_hash for request in requests):
            raise ContractError("proposal jobs must use the locked numerical snapshot")
        feedback = development_feedback(self.proposer_store, self.artifacts, self.run_id)
        informed = tuple(replace(request, context={
            **request.context,
            "measured_development_feedback": feedback,
            "locked_numerical_inputs": {"fields": plan.fields, "horizon": plan.horizon,
                                         "development_end": plan.development_end,
                                         "baseline": plan.baseline},
        }) for request in requests)
        result = self.proposals.run_round(informed, round_id)
        submissions = []
        for candidate in result.candidates:
            key = f"{round_id}:{candidate.candidate_id}"
            metadata = {"candidate_id": candidate.candidate_id, "name": candidate.name,
                        "agent_kind": candidate.agent_kind.value, "lineage_ids": candidate.lineage_ids}
            expression = candidate.specification.get("dsl")
            if not isinstance(expression, str):
                note = {**metadata, "status": "data_needed",
                        "reason": "proposal needs an approved executable feature/DSL before numerical evaluation"}
                self.proposer_store.append_graph_event(self.run_id, f"data-needed:{key}", note)
                submissions.append(note)
                continue
            if candidate.input_snapshot_hash != plan.snapshot_hash:
                raise ContractError("proposal worker changed the committed snapshot")
            try:
                reservation = submit_formula(self.proposer_store, self.run_id, expression,
                                              submission_id=key, metadata=metadata)
                submissions.append({**metadata, "status": "reserved" if reservation.created else "duplicate",
                                    "trial_id": str(reservation.trial_id)})
            except ContractError as exc:
                submissions.append({**metadata, "status": "not_submitted", "reason": str(exc)})
            except psycopg.errors.RaiseException as exc:
                if "trial budget exhausted" not in str(exc):
                    raise
                submissions.append({**metadata, "status": "budget_exhausted"})
        jobs = self.drain()
        return EvaluatedResearchRound(result, tuple(submissions), jobs,
            development_feedback(self.proposer_store, self.artifacts, self.run_id), self._outstanding())
