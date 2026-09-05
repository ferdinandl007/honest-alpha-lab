"""Real database/process integration using explicitly labeled correctness inputs."""
from dataclasses import replace
from uuid import uuid4

import pytest
from test_numerical_cli import make_numerical_snapshot
from test_postgres_store import cluster as _postgres_cluster

from honest_alpha_lab.artifacts import LocalArtifactStore
from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.dsl import Formula
from honest_alpha_lab.numerical import WalkForwardConfig
from honest_alpha_lab.pipeline import (
    NumericalJobService,
    NumericalPlan,
    development_feedback,
    freeze_numerical_run,
    submit_formula,
)

cluster = _postgres_cluster  # pytest fixture reuse, with a separately owned temporary cluster


def prepare(cluster, tmp_path, *, permit_fixture=True, **changes):
    snapshot, dates = make_numerical_snapshot(tmp_path)
    config = WalkForwardConfig(min_train_days=10, test_days=10, min_assets=4, bootstrap_samples=99)
    plan = NumericalPlan(snapshot.snapshot_hash, str(dates[-1]), ("x",), horizon=5,
                         beta_window=5, allow_correctness_fixture=permit_fixture, config=config,
                         max_trial_seconds=30)
    plan = replace(plan, **changes)
    run_id = f"pipeline-{uuid4()}"
    freeze_numerical_run(cluster.store("validator_one"), run_id, plan,
                         max_trials=3, max_attempts=1)
    return run_id, snapshot, plan


def test_canonical_formula_roundtrips_and_lists_inputs():
    for expression in ("+ x 1", "rank(+ x y)", "min(x, y, 0.00001)", "ts_mean(x, 5)"):
        parsed = Formula.parse(expression)
        assert Formula.parse(parsed.canonical_expression).formula_hash == parsed.formula_hash
    assert Formula.parse("rank(+ x y)").required_fields == frozenset({"x", "y"})
    assert Formula.parse("1").required_fields == frozenset()


def test_postgres_proposal_numerical_worker_feedback_and_duplicate(cluster, tmp_path):
    run_id, snapshot, _ = prepare(cluster, tmp_path)
    proposer = cluster.store("proposer_one")
    first = submit_formula(proposer, run_id, "+ x 1", submission_id="one")
    duplicate = submit_formula(proposer, run_id, " + 1 x ", submission_id="two")
    replay = submit_formula(proposer, run_id, "+ x 1", submission_id="one")
    assert first.created and not duplicate.created and not replay.created
    assert first.trial_id == duplicate.trial_id == replay.trial_id
    assert proposer.get_run(run_id)["reserved_trials"] == 1
    service = NumericalJobService(cluster.store("worker_one"), snapshot.directory.parent,
                                   tmp_path / "artifacts", lease_seconds=6)
    result = service.process_one(run_id)
    assert result["status"] == "evaluated", result
    assert result["financial_alpha_verified"] is False
    assert result["snapshot_purpose"] == "correctness_fixture"
    assert service.process_one(run_id) is None
    feedback = development_feedback(proposer, LocalArtifactStore(tmp_path / "artifacts"), run_id)
    assert len(feedback) == 1
    assert feedback[0]["trial_id"] == str(first.trial_id)
    assert "incremental_rank_ic" in feedback[0]
    assert feedback[0]["financial_alpha_verified"] is False
    assert {item["method"] for item in feedback[0]["regime_diagnostics"]} == {"hmm", "gaussian_mixture"}
    assert feedback[0]["signals_artifact_hash"] == result["signals_artifact_hash"]
    assert feedback[0]["strategy_id"] == result["formula_hash"]
    assert proposer.verify_chain(run_id)


def test_invalid_proposal_is_logged_without_claiming_valid_evaluation(cluster, tmp_path):
    run_id, _, _ = prepare(cluster, tmp_path)
    proposer = cluster.store("proposer_one")
    before = len(proposer.records(run_id))
    with pytest.raises(ContractError, match="outside"):
        submit_formula(proposer, run_id, "rank(future_outcome)", submission_id="invalid")
    assert len(proposer.records(run_id)) == before + 2
    assert proposer.get_run(run_id)["reserved_trials"] == 0


def test_fixture_refusal_is_durable_terminal_non_evidence(cluster, tmp_path):
    run_id, snapshot, _ = prepare(cluster, tmp_path, permit_fixture=False)
    proposer = cluster.store("proposer_one")
    reservation = submit_formula(proposer, run_id, "x", submission_id="one")
    service = NumericalJobService(cluster.store("worker_one"), snapshot.directory.parent, tmp_path / "artifacts")
    result = service.process_one(run_id)
    assert result["status"] == "not_evaluated"
    assert "report_artifact_hash" not in result
    assert proposer.get_trial(reservation.trial_id)["state"] == "completed"  # job, not alpha approval
    feedback = development_feedback(proposer, LocalArtifactStore(tmp_path / "artifacts"), run_id)
    assert feedback[0]["status"] == "not_evaluated"
    assert "rank_ic" not in feedback[0]


def test_timeout_kills_actual_numerical_process_and_spends_attempt(cluster, tmp_path):
    expensive = WalkForwardConfig(min_train_days=10, test_days=10, min_assets=4,
                                  bootstrap_samples=1_000_000)
    run_id, snapshot, _ = prepare(cluster, tmp_path, config=expensive, max_trial_seconds=1)
    proposer = cluster.store("proposer_one")
    reservation = submit_formula(proposer, run_id, "x", submission_id="one")
    service = NumericalJobService(cluster.store("worker_one"), snapshot.directory.parent, tmp_path / "artifacts")
    with pytest.raises(TimeoutError):
        service.process_one(run_id)
    state = proposer.get_trial(reservation.trial_id)
    assert state["state"] == "failed" and state["attempts"] == 1
    assert proposer.get_run(run_id)["reserved_trials"] == 1
    assert proposer.verify_chain(run_id)


def test_measured_feedback_reaches_next_proposal_round_without_credentials(cluster, tmp_path):
    from honest_alpha_lab.contracts import AgentKind, AlphaCandidate, ResearchBudget, ResearchJob
    from honest_alpha_lab.ledger import AlphaRegistry
    from honest_alpha_lab.orchestration import JobUsage
    from honest_alpha_lab.prompts import get_prompt
    from honest_alpha_lab.research_loop import (
        DiversityResearchLoop,
        QueuedResearchLoop,
        ResearchRequest,
    )
    from honest_alpha_lab.research_vault import ResearchVault
    from honest_alpha_lab.subagents import SubagentOrchestrator, SubagentResult

    run_id, snapshot, _ = prepare(cluster, tmp_path)
    registry = AlphaRegistry()
    orchestrator = SubagentOrchestrator(registry)

    class FixtureProposer:
        """Contract test worker, not an LLM performance benchmark."""
        kind = AgentKind.SYMBOLIC_FACTOR

        def __init__(self):
            self.contexts = []

        def run(self, task, prompt, tools):
            self.contexts.append(task.context)
            # A second round deliberately proposes an uncompiled idea; it must
            # remain data-needed rather than obtaining invented numerical results.
            spec = {"dsl": "rank(x)"} if len(self.contexts) == 1 else {"hypothesis": "needs new data"}
            candidate = AlphaCandidate.new(self.kind, f"fixture-{len(self.contexts)}", spec,
                                             task.job.input_snapshot_hash, ("test-only",))
            return SubagentResult((candidate,), usage=JobUsage(trials=1))

    generator = FixtureProposer()
    orchestrator.register_worker(generator)
    proposal_loop = DiversityResearchLoop(orchestrator, registry, ResearchVault(tmp_path / "vault"))
    loop = QueuedResearchLoop(proposal_loop, cluster.store("proposer_one"),
        NumericalJobService(cluster.store("worker_one"), snapshot.directory.parent, tmp_path / "artifacts"),
        LocalArtifactStore(tmp_path / "artifacts"), run_id)

    def request():
        prompt = get_prompt("symbolic-factor-research")
        job = ResearchJob(str(uuid4()), generator.kind, snapshot.snapshot_hash,
                           ResearchBudget(max_trials=1), prompt.prompt_hash, "test")
        return ResearchRequest(job, prompt.name, {"lineage_ids": ("test-only",)})

    first = loop.run_round((request(),), round_id="first")
    assert first.numerical_jobs[0]["status"] == "evaluated"
    assert not first.outstanding_trials
    second = loop.run_round((request(),), round_id="second")
    assert generator.contexts[0]["measured_development_feedback"] == ()
    assert len(generator.contexts[1]["measured_development_feedback"]) == 1
    assert generator.contexts[1]["measured_development_feedback"][0]["financial_alpha_verified"] is False
    assert "dsn" not in str(generator.contexts).lower()
    assert second.submissions[0]["status"] == "data_needed"
    assert second.numerical_jobs == ()
    assert len(second.measured_feedback) == 1
