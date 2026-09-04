"""Honest Alpha Lab: guardrails-first alpha research primitives."""

from .contracts import (
    AgentKind,
    AlphaCandidate,
    AlphaStatus,
    EvaluationPolicy,
    InputSnapshot,
    ResearchBudget,
    TrialRecord,
)
from .ledger import AlphaRegistry, ImmutableLedger
from .alternative_data import (
    AlternativeDatasetCreationAgent,
    DatasetRegistry,
    EconomicHypothesis,
)
from .subagents import AgentTask, DelegatingSubagentWorker, SubagentOrchestrator
from .cli_agents import CliAgentSpec, CliSubagentWorker, FileTaskStore
from .reward import ResearchRewardHarness, ResearchRewardPolicy, RewardEvidence, RewardResult
from .research_loop import DiversityResearchLoop, ResearchRequest, ResearchRoundResult
from .research_vault import ResearchVault
from .validation import RobustnessRubric, ValidationCheck, ValidationPlan
from .portfolio import AllocationPolicy, HistoricalDataset, PortfolioBacktester, TraditionalAllocator
from .strategies import StrategyDefinition, StrategyFamily, StrategySide, research_strategy_catalog
from .asset_mapping import (
    AssetMappingProposal,
    AssetExposure,
    AssetExposureMap,
    AssetMappingRegistry,
    ExposureKind,
    ExternalFeatureObservation,
    MappedFeatureObservation,
    PointInTimeAssetMapper,
)

__all__ = [
    "AgentKind",
    "AlphaCandidate",
    "AlphaRegistry",
    "AgentTask",
    "AlternativeDatasetCreationAgent",
    "AlphaStatus",
    "EvaluationPolicy",
    "ImmutableLedger",
    "InputSnapshot",
    "DatasetRegistry",
    "DelegatingSubagentWorker",
    "CliAgentSpec",
    "CliSubagentWorker",
    "FileTaskStore",
    "ResearchRewardHarness",
    "ResearchRewardPolicy",
    "RewardEvidence",
    "RewardResult",
    "DiversityResearchLoop",
    "ResearchRequest",
    "ResearchRoundResult",
    "ResearchVault",
    "RobustnessRubric",
    "ValidationCheck",
    "ValidationPlan",
    "AllocationPolicy",
    "HistoricalDataset",
    "PortfolioBacktester",
    "TraditionalAllocator",
    "StrategyDefinition",
    "StrategyFamily",
    "StrategySide",
    "research_strategy_catalog",
    "AssetMappingProposal",
    "AssetExposure",
    "AssetExposureMap",
    "AssetMappingRegistry",
    "ExposureKind",
    "ExternalFeatureObservation",
    "MappedFeatureObservation",
    "PointInTimeAssetMapper",
    "EconomicHypothesis",
    "ResearchBudget",
    "SubagentOrchestrator",
    "TrialRecord",
]
