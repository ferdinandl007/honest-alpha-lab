from datetime import date, datetime, timedelta, timezone
import tempfile
import unittest

from honest_alpha_lab.agents import (
    AlternativeDataAgent,
    AlternativeHypothesis,
    EventSignal,
    SymbolicFactorAgent,
    TextEventAgent,
    make_job,
)
from honest_alpha_lab.alternative_data import (
    AggregateDatasetObservation,
    AlternativeDatasetCreationAgent,
    CatalogSearchTool,
    DatasetCatalogEntry,
    DatasetRegistry,
    EconomicHypothesis,
    InMemoryDatasetCatalog,
    PITFeatureDefinition,
    RawDatasetObservation,
    SemanticEvidence,
    SemanticRewardPolicy,
)
from honest_alpha_lab.asset_mapping import (
    AssetExposure,
    AssetMappingProposal,
    AssetMappingRegistry,
    ExposureKind,
    ExternalFeatureObservation,
    PointInTimeAssetMapper,
)
from honest_alpha_lab.artifacts import LocalArtifactStore
from honest_alpha_lab.contracts import (
    AgentKind,
    AlphaStatus,
    ContractError,
    DataLineage,
    EvaluationPolicy,
    InputSnapshot,
    ResearchBudget,
)
from honest_alpha_lab.dsl import Formula
from honest_alpha_lab.evaluation import (
    EvaluationResult,
    PurgedWalkForwardSplitter,
    SealedTestConfig,
    SealedTestGate,
    capacity_proxy,
    conditional_performance,
    half_life,
    rank_ic,
    similarity,
    stability_by_fold,
    trial_adjusted_pvalue,
    turnover,
)
from honest_alpha_lab.ledger import AlphaRegistry, TrialLedger
from honest_alpha_lab.models import (
    EqualWeightAlphaEnsemble,
    GaussianHMMBaseline,
    MarketRegimeObservation,
    MultivariateGaussianHMMBaseline,
    RidgeRanker,
    RollingClusterBaseline,
)
from honest_alpha_lab.orchestration import JobUsage, ResearchQueue
from honest_alpha_lab.pit import PITObservation, PITStore, UniverseMembership
from honest_alpha_lab.prompts import ALTERNATIVE_DATASET_PROMPT, get_prompt
from honest_alpha_lab.subagents import (
    AgentTask,
    LocalProposalWorker,
    SubagentOrchestrator,
)
from honest_alpha_lab.tools import ToolCall, ToolName, ToolResult, ToolRouter


UTC = timezone.utc


def lineage(dataset_id="prices"):
    return DataLineage(
        dataset_id,
        "provider",
        "licensed-test",
        "https://data.test/x",
        datetime(2026, 1, 1, tzinfo=UTC),
        "content-hash",
        "v1",
    )


def snapshot():
    return InputSnapshot(("data-hash",), "universe-hash", "code-hash")


class ContractTests(unittest.TestCase):
    def test_hash_chain_and_lifecycle_authorization(self):
        registry = AlphaRegistry()
        candidate = SymbolicFactorAgent().propose(
            make_job(
                AgentKind.SYMBOLIC_FACTOR, snapshot(), ResearchBudget(), "+", "test"
            ),
            {"formulas": ["+ momentum value"], "lineages": [lineage()]},
        )[0]
        registry.register(candidate)
        with self.assertRaises(PermissionError):
            registry.transition(
                candidate.candidate_id,
                AlphaStatus.VALIDATED,
                "research-agent",
                "looks good",
            )
        registry.transition(
            candidate.candidate_id,
            AlphaStatus.SCREENED,
            "screening-service",
            "schema ok",
        )
        registry.transition(
            candidate.candidate_id,
            AlphaStatus.VALIDATED,
            "numerical-validator",
            "walk-forward passed",
        )
        accepted = registry.transition(
            candidate.candidate_id,
            AlphaStatus.ACCEPTED,
            "numerical-validator",
            "incremental",
        )
        self.assertEqual(accepted.status, AlphaStatus.ACCEPTED)
        self.assertTrue(registry.verify())
        self.assertEqual(len(registry.events()), 4)

    def test_trial_ledger_is_append_only(self):
        ledger = TrialLedger()
        started = ledger.start(
            "candidate", AgentKind.SYMBOLIC_FACTOR, "snapshot", "policy"
        )
        completed = ledger.complete(started, {"rank_ic": 0.1})
        self.assertEqual(completed.status.value, "completed")
        self.assertEqual(len(ledger.entries()), 2)
        self.assertTrue(ledger.verify())
        with self.assertRaises(ContractError):
            ledger.complete(completed, {})

    def test_pit_store_rejects_lookahead(self):
        store = PITStore([lineage()], [UniverseMembership("ABC", date(2020, 1, 1))])
        store.add(
            PITObservation(
                "ABC",
                "estimate",
                3.0,
                date(2026, 1, 3),
                datetime(2026, 1, 4, 12, tzinfo=UTC),
                "prices",
            )
        )
        decision_time = datetime(2026, 1, 4, 9, tzinfo=UTC)
        self.assertEqual(
            store.query("ABC", "estimate", date(2026, 1, 4), decision_time), ()
        )
        decision_time = datetime(2026, 1, 4, 13, tzinfo=UTC)
        self.assertEqual(
            len(store.query("ABC", "estimate", date(2026, 1, 4), decision_time)), 1
        )

    def test_external_observation_maps_only_after_steward_approval_and_pit_checks(self):
        proposal = AssetMappingProposal.new(
            "airline-demand", "tsa", "checkpoint_surprise", "airport IATA code",
            ExposureKind.ROUTE_OR_HUB, "dated airline hub roster", "airport demand affects carrier revenue",
            ("historical route disclosure",),
        )
        exposure = AssetExposure(
            "AIR", "JFK", ExposureKind.ROUTE_OR_HUB, 0.4,
            date(2020, 1, 1), None, datetime(2020, 1, 2, tzinfo=UTC),
            "https://example.test/airline-route", "route-roster-v1",
        )
        maps = AssetMappingRegistry()
        exposure_map = maps.submit(proposal, (exposure,), ("route-roster-v1",))
        observation = ExternalFeatureObservation(
            "tsa", "checkpoint_surprise", "JFK", 2.0, date(2020, 1, 3),
            datetime(2020, 1, 3, 18, tzinfo=UTC), "tsa-vintage",
        )
        mapper = PointInTimeAssetMapper((UniverseMembership("AIR", date(2020, 1, 1)),))
        with self.assertRaises(PermissionError):
            mapper.map(exposure_map, (observation,), date(2020, 1, 3), datetime(2020, 1, 3, 19, tzinfo=UTC))
        approved = maps.approve(exposure_map.map_id, "data-steward", "dated source and map reviewed", "tsa")
        self.assertEqual(
            mapper.map(approved, (observation,), date(2020, 1, 3), datetime(2020, 1, 3, 17, tzinfo=UTC)),
            (),
        )
        mapped = mapper.map(approved, (observation,), date(2020, 1, 3), datetime(2020, 1, 3, 19, tzinfo=UTC))
        self.assertEqual(len(mapped), 1)
        self.assertEqual(mapped[0].observation.asset, "AIR")
        self.assertEqual(mapped[0].observation.value, 0.8)

    def test_external_mapping_rejects_future_membership_and_future_mapping(self):
        proposal = AssetMappingProposal.new(
            "retail", "census", "category_surprise", "NAICS code", ExposureKind.INDUSTRY,
            "historical NAICS classification", "category sales affect retailers", ("PIT classification",),
        )
        exposure = AssetExposure(
            "NEW", "445", ExposureKind.INDUSTRY, 1.0, date(2020, 1, 1), None,
            datetime(2020, 2, 1, tzinfo=UTC), "https://example.test/classification", "sic-v1",
        )
        registry = AssetMappingRegistry()
        submitted = registry.submit(proposal, (exposure,), ("sic-v1",))
        approved = registry.approve(submitted.map_id, "data-steward", "ok", "census")
        mapper = PointInTimeAssetMapper((UniverseMembership("NEW", date(2020, 3, 1)),))
        observation = ExternalFeatureObservation(
            "census", "category_surprise", "445", 1.0, date(2020, 1, 15),
            datetime(2020, 1, 16, tzinfo=UTC), "census-v1",
        )
        self.assertEqual(mapper.map(approved, (observation,), date(2020, 1, 16), datetime(2020, 2, 2, tzinfo=UTC)), ())


class EvaluationTests(unittest.TestCase):
    def test_formula_is_constrained_and_deterministic(self):
        formula = Formula.parse("zscore(+ momentum value)")
        self.assertEqual(
            formula.evaluate({"momentum": [1, 2, 3], "value": [3, 2, 1]}),
            [0.0, 0.0, 0.0],
        )
        with self.assertRaises(ContractError):
            Formula.parse("__import__(os)")
        with self.assertRaises(ContractError):
            Formula.parse("sqrt momentum")

    def test_walk_forward_purges_and_embargoes(self):
        policy = EvaluationPolicy(
            purge_days=60, embargo_days=5, min_train_days=10, min_test_days=5
        )
        dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(100)]
        windows = PurgedWalkForwardSplitter(policy).split(dates)
        self.assertGreater(len(windows), 0)
        first = windows[0]
        train_dates = {d for d in dates if first.train_start <= d <= first.train_end}
        test_dates = {d for d in dates if first.test_start <= d <= first.test_end}
        self.assertTrue(train_dates.isdisjoint(test_dates))
        self.assertGreater(
            (first.test_start - first.train_end).days, policy.embargo_days
        )

    def test_sealed_test_is_locked_and_single_use(self):
        policy = EvaluationPolicy()
        locked_snapshot = snapshot()
        locked = SealedTestConfig.create(
            locked_snapshot, policy, date(2026, 1, 1), date(2026, 3, 31)
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        gate = SealedTestGate(locked, state_path=directory.name + "/sealed.sqlite3")
        registry = AlphaRegistry()
        candidate = registry.register(
            SymbolicFactorAgent().propose(
                make_job(
                    AgentKind.SYMBOLIC_FACTOR, locked_snapshot, ResearchBudget(), "+", "test"
                ),
                {"formulas": ["+ momentum value"], "lineages": [lineage()]},
            )[0]
        )
        registry.transition(
            candidate.candidate_id, AlphaStatus.SCREENED, "screening-service", "ok"
        )
        candidate = registry.transition(
            candidate.candidate_id, AlphaStatus.VALIDATED, "numerical-validator", "ok"
        )

        def scorer(current, config):
            self.assertEqual(config.config_hash, locked.config_hash)
            return EvaluationResult(
                current.candidate_id,
                config.snapshot_hash,
                config.policy_hash,
                0.1,
                0.2,
                0.0,
                0.0,
                (0.1, 0.2),
                sealed=True,
            )

        result = gate.run(candidate, locked.snapshot_hash, locked.policy_hash, scorer)
        self.assertTrue(result.sealed)
        with self.assertRaises(ContractError):
            gate.run(candidate, locked.snapshot_hash, locked.policy_hash, scorer)

    def test_metrics_and_numerical_models(self):
        self.assertAlmostEqual(rank_ic([1, 2, 3], [10, 30, 20]), 0.5)
        self.assertAlmostEqual(turnover([0.5, 0.5], [0.25, 0.75]), 0.25)
        self.assertEqual(half_life([1.0, 0.7, 0.4]), 2.0)
        self.assertAlmostEqual(similarity([1, 2, 3], [1, 3, 2]), 0.5)
        self.assertAlmostEqual(stability_by_fold([0.1, -0.1, 0.2]), 2 / 3)
        self.assertEqual(trial_adjusted_pvalue(0.01, 200), 1.0)
        self.assertEqual(
            conditional_performance([1, 2], [0.1, 0.3], [[0.8, 0.2], [0.2, 0.8]]),
            (0.2, 0.5),
        )
        self.assertEqual(capacity_proxy([1000, 500], [0.1, 0.2]), 250.0)
        ensemble = EqualWeightAlphaEnsemble().transform({"a": [1, 2], "b": [3, 4]})
        self.assertEqual(ensemble, (2.0, 3.0))
        weights = RidgeRanker(regularization=0.1, steps=100).fit(
            [[1], [2], [3]], [1, 2, 3]
        )
        self.assertGreater(weights[0], 0)
        self.assertEqual(
            len(
                GaussianHMMBaseline()
                .fit([-0.1, -0.05, 0.01, 0.02, 0.1, 0.2])
                .filter([0.0])
            ),
            1,
        )
        regime_observations = [
            MarketRegimeObservation(value, 0.2, 1.0, 0.5, 0.3, 1.0, 0.04)
            for value in (-0.1, -0.05, 0.01, 0.02, 0.1, 0.2, 0.15, -0.02)
        ]
        posterior = (
            MultivariateGaussianHMMBaseline()
            .fit(regime_observations)
            .filter(regime_observations[:1])[0]
        )
        self.assertAlmostEqual(sum(posterior), 1.0)
        self.assertEqual(
            len(
                RollingClusterBaseline()
                .fit([-0.1, -0.05, 0.01, 0.02])
                .predict_proba([0.0])
            ),
            1,
        )


class AgentAndOpsTests(unittest.TestCase):
    def test_text_and_alternative_agents_require_typed_provenance(self):
        job = make_job(
            AgentKind.TEXT_EVENT, snapshot(), ResearchBudget(), "event", "test"
        )
        event = EventSignal(
            "e1",
            "ABC",
            "guidance",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, 1, tzinfo=UTC),
            0.5,
            1.0,
            "news",
        )
        candidate = TextEventAgent().propose(
            job, {"events": [event], "lineages": [lineage("news")]}
        )[0]
        self.assertEqual(candidate.agent_kind, AgentKind.TEXT_EVENT)
        alt_job = make_job(
            AgentKind.ALTERNATIVE_DATA, snapshot(), ResearchBudget(), "alt", "test"
        )
        hypothesis = AlternativeHypothesis(
            "footfall",
            "consumer activity",
            "footfall-v1",
            "provider",
            "licensed-test",
            2,
            "weekly_change",
        )
        alt = AlternativeDataAgent().propose(
            alt_job,
            {
                "hypotheses": [hypothesis],
                "lineages": [lineage("footfall-v1")],
            },
        )[0]
        self.assertEqual(alt.agent_kind, AgentKind.ALTERNATIVE_DATA)
        with self.assertRaises(ContractError):
            AlternativeDataAgent().propose(
                alt_job,
                {
                    "hypotheses": [
                        AlternativeHypothesis(
                            "unlicensed",
                            "mechanism",
                            "unlicensed",
                            "provider",
                            "license",
                            1,
                            "feature",
                        )
                    ],
                    "lineages": [lineage()],
                },
            )

    def test_queue_budget_and_content_addressed_artifacts(self):
        job = make_job(
            AgentKind.SYMBOLIC_FACTOR,
            snapshot(),
            ResearchBudget(max_trials=2),
            "+",
            "test",
        )
        queue = ResearchQueue()
        queue.submit(job)
        self.assertEqual(queue.claim(), job)
        queue.record_usage(job, JobUsage(trials=2))
        with self.assertRaises(PermissionError):
            queue.record_usage(job, JobUsage(trials=3))
        with tempfile.TemporaryDirectory() as directory:
            store = LocalArtifactStore(directory)
            digest = store.put(b"report")
            self.assertEqual(store.get(digest), b"report")
            with self.assertRaises(ContractError):
                store.put(b"report", "0" * 64)

    def test_local_subagent_orchestrator_publishes_only_proposals(self):
        job = make_job(
            AgentKind.SYMBOLIC_FACTOR, snapshot(), ResearchBudget(), "symbolic", "test"
        )
        task = AgentTask.new(
            job,
            "symbolic-factor-research",
            {"formulas": ["+ momentum value"], "lineages": [lineage()]},
        )
        registry = AlphaRegistry()
        orchestrator = SubagentOrchestrator(registry)
        orchestrator.register_worker(LocalProposalWorker(SymbolicFactorAgent()))
        orchestrator.submit(task)
        run = orchestrator.run_next()
        self.assertEqual(run.status.value, "completed")
        self.assertEqual(len(registry.all()), 1)
        self.assertEqual(registry.all()[0].status, AlphaStatus.PROPOSED)
        self.assertTrue(orchestrator.verify())
        with self.assertRaises(ContractError):
            AgentTask.new(job, "text-event-research", {})


class SemanticFactoryTests(unittest.TestCase):
    def _entry(self, dataset_id="faa-aircraft-movements", eligible=True):
        return DatasetCatalogEntry(
            dataset_id=dataset_id,
            provider="Federal Aviation Administration",
            source_uri="https://example.test/faa",
            license_id="public-domain",
            license_uri="https://example.test/faa-license",
            tags=("faa", "aircraft", "cargo", "fedex"),
            point_in_time=True,
            allows_research=True,
            allows_derived_features=True,
            contains_personal_data=not eligible,
            license_verified_at=datetime(2026, 1, 1, tzinfo=UTC) if eligible else None,
        )

    def _hypothesis(self):
        return EconomicHypothesis.new(
            name="FedEx hub aircraft movements",
            issuer_or_industry="FedEx",
            observation="daily cargo-hub aircraft movements",
            economic_mechanism="package volume leads shipping revenue",
            fundamental_target="quarterly revenue",
            expected_direction=1,
            lead_min_days=7,
            lead_max_days=56,
            earnings_bridge="revenue forecast error predicts earnings surprise",
            abnormal_return_bridge="surprise forecast predicts post-announcement residual return",
            discovery_terms=("faa", "cargo", "fedex"),
        )

    def test_discovery_requires_independent_approval_before_feature_or_alpha_handoff(
        self
    ):
        catalog = InMemoryDatasetCatalog(
            [self._entry(), self._entry("private-mobility", False)]
        )
        registry = DatasetRegistry()
        job = make_job(
            AgentKind.ALTERNATIVE_DATASET_CREATOR,
            snapshot(),
            ResearchBudget(),
            "discover legally accessible leading indicators",
            "test",
        )
        agent = AlternativeDatasetCreationAgent()
        candidates = agent.discover(job, [self._hypothesis()], catalog, registry)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        definition = PITFeatureDefinition(
            "hub_movements", candidate.candidate_id, "movements", 1
        )
        raw = [
            RawDatasetObservation(
                "FDX",
                date(2026, 1, 2),
                datetime(2026, 1, 3, 8, tzinfo=UTC),
                {"movements": 100.0},
            )
        ]
        with self.assertRaises(PermissionError):
            agent.build_feature(definition, raw, registry)
        with self.assertRaises(PermissionError):
            registry.approve(candidate.candidate_id, "dataset-agent", "self-approved")
        registry.approve(
            candidate.candidate_id, "data-steward", "license and PIT semantics reviewed"
        )
        build = agent.build_feature(definition, raw, registry)
        self.assertEqual(build.observations[0].value, 100.0)
        self.assertEqual(
            build.observations[0].available_at, datetime(2026, 1, 4, 8, tzinfo=UTC)
        )
        handoff = agent.alpha_hypothesis(self._hypothesis(), definition, registry)
        feature_snapshot = InputSnapshot(
            ("data-hash", build.lineage.content_hash), "universe-hash", "code-hash"
        )
        alpha_job = make_job(
            AgentKind.ALTERNATIVE_DATA,
            feature_snapshot,
            ResearchBudget(),
            "test",
            "test",
        )
        proposal = AlternativeDataAgent().propose(
            alpha_job, {"hypotheses": [handoff], "lineages": [build.lineage]}
        )[0]
        self.assertEqual(proposal.status, AlphaStatus.PROPOSED)
        self.assertTrue(registry.verify())

    def test_aggregate_feature_requires_a_licensed_source_and_approved_asset_map(self):
        catalog = InMemoryDatasetCatalog([self._entry("tsa")])
        dataset_registry = DatasetRegistry()
        discovery_job = make_job(
            AgentKind.ALTERNATIVE_DATASET_CREATOR, snapshot(), ResearchBudget(), "discover", "test"
        )
        agent = AlternativeDatasetCreationAgent()
        dataset_candidate = agent.discover(discovery_job, [self._hypothesis()], catalog, dataset_registry)[0]
        dataset_registry.approve(dataset_candidate.candidate_id, "data-steward", "reviewed")
        definition = PITFeatureDefinition("checkpoint_surprise", dataset_candidate.candidate_id, "volume", 0)
        proposal = AssetMappingProposal.new(
            self._hypothesis().hypothesis_id, "tsa", "checkpoint_surprise", "airport IATA code",
            ExposureKind.ROUTE_OR_HUB, "historical carrier hub table", "passenger traffic leads revenue",
            ("carrier route filing",),
        )
        exposure = AssetExposure(
            "AIR", "JFK", ExposureKind.ROUTE_OR_HUB, 0.5, date(2025, 1, 1), None,
            datetime(2025, 1, 2, tzinfo=UTC), "https://example.test/hubs", "hubs-v1",
        )
        mapping_registry = AssetMappingRegistry()
        submitted = mapping_registry.submit(proposal, (exposure,), ("hubs-v1",))
        exposure_map = mapping_registry.approve(submitted.map_id, "data-steward", "reviewed", "tsa")
        lineage, mapped, feature_hash = agent.build_mapped_feature(
            definition,
            (AggregateDatasetObservation("JFK", date(2026, 1, 3), datetime(2026, 1, 3, 12, tzinfo=UTC), {"volume": 10.0}),),
            dataset_registry,
            exposure_map,
            PointInTimeAssetMapper((UniverseMembership("AIR", date(2025, 1, 1)),)),
            date(2026, 1, 3),
            datetime(2026, 1, 3, 13, tzinfo=UTC),
        )
        self.assertEqual(lineage.dataset_id, "tsa")
        self.assertEqual(mapped[0].observation.asset, "AIR")
        self.assertEqual(mapped[0].observation.value, 5.0)
        self.assertTrue(feature_hash)

    def test_catalog_tool_is_typed_and_restricted(self):
        catalog = InMemoryDatasetCatalog([self._entry()])
        router = ToolRouter()
        router.register(ToolName.DATASET_CATALOG_SEARCH, CatalogSearchTool(catalog), max_cost_usd=0)
        job = make_job(
            AgentKind.ALTERNATIVE_DATASET_CREATOR,
            snapshot(),
            ResearchBudget(),
            "catalog discovery",
            "test",
        )
        router.register_job(job)
        result = router.execute(
            ToolCall(
                "run",
                job.job_id,
                AgentKind.ALTERNATIVE_DATASET_CREATOR,
                ToolName.DATASET_CATALOG_SEARCH,
                {"terms": ["faa"]},
            )
        )
        self.assertEqual(
            result.output["datasets"][0]["dataset_id"], "faa-aircraft-movements"
        )
        with self.assertRaises(PermissionError):
            router.execute(
                ToolCall(
                    "run",
                    job.job_id,
                    AgentKind.SYMBOLIC_FACTOR,
                    ToolName.DATASET_CATALOG_SEARCH,
                    {"terms": ["faa"]},
                )
            )
        self.assertTrue(router.verify())

    def test_tool_costs_are_enforced_by_the_registered_job_budget(self):
        class CostlyCatalogTool:
            def execute(self, arguments):
                del arguments
                return ToolResult({"ok": True}, external_cost_usd=2.0)

        router = ToolRouter()
        router.register(ToolName.DATASET_CATALOG_SEARCH, CostlyCatalogTool(), max_cost_usd=2)
        job = make_job(
            AgentKind.ALTERNATIVE_DATASET_CREATOR,
            snapshot(),
            ResearchBudget(max_data_cost_usd=1.0),
            "catalog discovery",
            "test",
        )
        router.register_job(job)
        with self.assertRaises(PermissionError):
            router.execute(
                ToolCall(
                    "run",
                    job.job_id,
                    AgentKind.ALTERNATIVE_DATASET_CREATOR,
                    ToolName.DATASET_CATALOG_SEARCH,
                    {"terms": ["faa"]},
                )
            )

    def test_semantic_reward_prefers_incremental_stable_information(self):
        policy = SemanticRewardPolicy()
        differentiated = SemanticEvidence(0.04, 0.06, 0.05, 0.1, 50, 0.5, 0.05)
        crowded = SemanticEvidence(0.04, 0.06, 0.05, 0.9, 2_000, 4.0, 0.5)
        self.assertGreater(policy.score(differentiated), policy.score(crowded))
        self.assertEqual(
            get_prompt(ALTERNATIVE_DATASET_PROMPT.name).prompt_hash,
            ALTERNATIVE_DATASET_PROMPT.prompt_hash,
        )


if __name__ == "__main__":
    unittest.main()
