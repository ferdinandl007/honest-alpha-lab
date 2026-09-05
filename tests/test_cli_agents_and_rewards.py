from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

from honest_alpha_lab.agents import make_job
from honest_alpha_lab.cli_agents import CliAgentSpec, CliSubagentWorker, FileTaskStore
from honest_alpha_lab.contracts import AgentKind, DataLineage, InputSnapshot, ResearchBudget
from honest_alpha_lab.evaluation import EvaluationResult
from honest_alpha_lab.prompts import get_prompt
from honest_alpha_lab.reward import ResearchRewardHarness, ResearchRewardPolicy, RewardEvidence
from honest_alpha_lab.subagents import AgentTask
from honest_alpha_lab.subagents import AgentRun
from honest_alpha_lab.contracts import AgentRunStatus, AlphaCandidate
from honest_alpha_lab.tools import ToolRouter
from honest_alpha_lab.research_vault import ResearchVault
from honest_alpha_lab.validation import RobustnessRubric
from honest_alpha_lab.portfolio import (
    AllocationPolicy,
    HistoricalDataset,
    MarketBar,
    PointInTimeSignal,
    PortfolioBacktester,
    TraditionalAllocator,
)
from honest_alpha_lab.strategies import research_strategy_catalog


UTC = timezone.utc


def _snapshot() -> InputSnapshot:
    return InputSnapshot(("dataset-hash",), "universe-hash", "code-hash")


def _lineage() -> DataLineage:
    return DataLineage(
        "prices", "test", "licensed", "https://example.test/prices",
        datetime(2026, 1, 1, tzinfo=UTC), "content-hash", "v1"
    )


class CliAgentTests(unittest.TestCase):
    def test_codex_default_isolated_from_desktop_plugins_and_rules(self):
        job = make_job(
            AgentKind.SYMBOLIC_FACTOR, _snapshot(), ResearchBudget(), "prompt", "test"
        )
        task = AgentTask.new(job, "symbolic-factor-research", {"lineages": [_lineage()]})
        spec = CliAgentSpec.codex()
        with tempfile.TemporaryDirectory() as directory:
            store = FileTaskStore(directory)
            package = store.create(task, "proposal", ())
            command = CliSubagentWorker(AgentKind.SYMBOLIC_FACTOR, spec, store)._command(package)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--ephemeral", command)

    def test_cli_worker_creates_auditable_task_package_and_parses_proposal(self):
        job = make_job(
            AgentKind.SYMBOLIC_FACTOR, _snapshot(), ResearchBudget(), "prompt", "test"
        )
        task = AgentTask.new(job, "symbolic-factor-research", {"lineages": [_lineage()]})
        fixture = Path(__file__).parent / "fixtures" / "fake_cli_agent.py"
        with tempfile.TemporaryDirectory() as directory:
            spec = CliAgentSpec(
                name="fake", executable=sys.executable,
                command_prefix=(str(fixture),), sandbox="read-only", allow_unmetered_provider=True
            )
            worker = CliSubagentWorker(AgentKind.SYMBOLIC_FACTOR, spec, FileTaskStore(directory))
            result = worker.run(task, get_prompt("symbolic-factor-research"), ToolRouter())
            package = Path(directory) / task.task_id
            self.assertEqual(result.alpha_candidates[0].name, "cli:momentum")
            self.assertEqual(result.alpha_candidates[0].input_snapshot_hash, job.input_snapshot_hash)
            self.assertTrue((package / "task.json").exists())
            self.assertTrue((package / "output.schema.json").exists())
            self.assertTrue((package / "events.jsonl").exists())
            self.assertIn("completed", (package / "trace.jsonl").read_text(encoding="utf-8"))
            self.assertTrue((package / "output.schema.json").is_absolute())


class RewardHarnessTests(unittest.TestCase):
    def test_reward_gates_real_evaluation_evidence_without_registry_authority(self):
        evaluation = EvaluationResult(
            candidate_id="candidate-1", snapshot_hash="snapshot", policy_hash="policy",
            out_of_sample_rank_ic=0.02, turnover=0.3, similarity=0.2,
            data_cost_usd=25.0, fold_metrics=(0.01, 0.02, -0.01, 0.03), sealed=False,
        )
        result = ResearchRewardHarness(ResearchRewardPolicy()).score(
            RewardEvidence(
                candidate_id="candidate-1", agent_kind=AgentKind.SYMBOLIC_FACTOR,
                evaluation=evaluation, raw_p_value=0.001, trial_count=10, complexity=1.0,
                capacity_proxy_usd=2_000_000.0, point_in_time_verified=True,
                lineage_verified=True, regime_rank_ics=(0.01, 0.02),
                incremental_rank_ic=0.008,
            )
        )
        self.assertTrue(result.eligible_for_registry_validation)
        self.assertFalse(result.eligible_for_registry_acceptance)
        self.assertLess(result.adjusted_p_value, 0.1)
        self.assertIn("similarity_penalty", result.components)

    def test_reward_refuses_unsealed_or_unverified_evidence(self):
        evaluation = EvaluationResult(
            candidate_id="candidate-2", snapshot_hash="snapshot", policy_hash="policy",
            out_of_sample_rank_ic=0.02, turnover=0.3, similarity=0.2,
            data_cost_usd=0.0, fold_metrics=(0.01, 0.02), sealed=False,
        )
        result = ResearchRewardHarness().score(
            RewardEvidence(
                candidate_id="candidate-2", agent_kind=AgentKind.ALTERNATIVE_DATA,
                evaluation=evaluation, raw_p_value=0.001, trial_count=1, complexity=0.0,
                capacity_proxy_usd=2_000_000.0, point_in_time_verified=False,
                lineage_verified=True, semantic_chain_verified=False,
            )
        )
        self.assertFalse(result.eligible_for_registry_validation)
        self.assertFalse(result.gates["sealed_evaluation"])
        self.assertFalse(result.gates["semantic_chain"])


class ResearchVaultTests(unittest.TestCase):
    def test_vault_records_linked_candidate_memory_for_later_rounds(self):
        job = make_job(
            AgentKind.SYMBOLIC_FACTOR, _snapshot(), ResearchBudget(), "prompt", "test"
        )
        task = AgentTask.new(job, "symbolic-factor-research", {"lineages": [_lineage()]})
        candidate = AlphaCandidate.new(
            AgentKind.SYMBOLIC_FACTOR,
            "symbolic:revision-volume",
            {"dsl": "rank(earnings_revision_20d)"},
            job.input_snapshot_hash,
            ("prices",),
        )
        with tempfile.TemporaryDirectory() as directory:
            vault = ResearchVault(directory)
            vault.record_round(
                "round-1",
                (task,),
                (AgentRun("run-1", task.task_id, AgentRunStatus.COMPLETED),),
                (candidate,),
            )
            memory = vault.shared_context()
            self.assertEqual(memory["already_tried"][0]["name"], candidate.name)
            note = Path(directory) / "candidates" / f"{candidate.candidate_id}.md"
            self.assertIn("[[datasets/prices]]", note.read_text(encoding="utf-8"))
            plan_path = vault.record_validation_plan(RobustnessRubric().plan(candidate))
            plan = plan_path.with_suffix(".md").read_text(encoding="utf-8")
            self.assertIn("market-state-probabilities", plan)
            self.assertIn("formula-exposure-ablation", plan)


class PortfolioSimulationTests(unittest.TestCase):
    def test_catalog_has_fifteen_unique_research_sleeves(self):
        self.assertEqual(len(research_strategy_catalog()), 15)

    def test_minimum_variance_allocation_is_capped_and_fully_invested(self):
        allocation = TraditionalAllocator(AllocationPolicy(max_sleeve_weight=0.60)).allocate(
            {"a": (0.01, 0.00, 0.02, -0.01), "b": (0.00, 0.01, -0.01, 0.00)}
        )
        self.assertAlmostEqual(sum(allocation.values()), 1.0, places=6)
        self.assertTrue(all(weight <= 0.60 + 1e-6 for weight in allocation.values()))

    def test_stop_has_precedence_when_stop_and_target_share_a_bar(self):
        catalog = research_strategy_catalog()
        snapshot_hash = "verified-test-snapshot"
        bars = (
            MarketBar(date(2020, 1, 1), "ABC", 100, 101, 99, 100, 10_000_000),
            MarketBar(date(2020, 1, 2), "ABC", 100, 101, 99, 100, 10_000_000),
            MarketBar(date(2020, 1, 3), "ABC", 100, 101, 99, 100, 10_000_000),
            MarketBar(date(2020, 1, 4), "ABC", 100, 120, 90, 100, 10_000_000),
        )
        signals = (PointInTimeSignal("exercise_retain", "ABC", 1.0, datetime(2020, 1, 1, 16, tzinfo=UTC), snapshot_hash),)
        result = PortfolioBacktester(catalog).run(
            HistoricalDataset(snapshot_hash, True, bars, signals), {"exercise_retain": 0.10}
        )
        self.assertIn("stop_loss", [fill.reason for fill in result.fills])
