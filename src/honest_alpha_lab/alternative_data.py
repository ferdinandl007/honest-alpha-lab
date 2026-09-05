"""Semantic alternative-data discovery, independent data approval, and PIT feature building."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Iterable, Mapping, Protocol, Sequence
from uuid import uuid4

from .contracts import (
    AgentKind,
    ContractError,
    DataLineage,
    ResearchJob,
    canonical_hash,
    utc_now,
)
from .dsl import Formula
from .ledger import ImmutableLedger, LedgerEntry
from .pit import PITObservation
from .tools import ToolResult
from .asset_mapping import (
    AssetExposureMap,
    ExternalFeatureObservation,
    MappedFeatureObservation,
    PointInTimeAssetMapper,
)


class DatasetStatus(str, Enum):
    DISCOVERED = "discovered"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class EconomicHypothesis:
    """A falsifiable bridge from observable proxy to fundamental to abnormal return."""

    hypothesis_id: str
    name: str
    issuer_or_industry: str
    observation: str
    economic_mechanism: str
    fundamental_target: str
    expected_direction: int
    lead_min_days: int
    lead_max_days: int
    earnings_bridge: str
    abnormal_return_bridge: str
    discovery_terms: tuple[str, ...]

    @classmethod
    def new(
        cls,
        name: str,
        issuer_or_industry: str,
        observation: str,
        economic_mechanism: str,
        fundamental_target: str,
        expected_direction: int,
        lead_min_days: int,
        lead_max_days: int,
        earnings_bridge: str,
        abnormal_return_bridge: str,
        discovery_terms: Sequence[str],
    ) -> "EconomicHypothesis":
        return cls(
            str(uuid4()),
            name,
            issuer_or_industry,
            observation,
            economic_mechanism,
            fundamental_target,
            expected_direction,
            lead_min_days,
            lead_max_days,
            earnings_bridge,
            abnormal_return_bridge,
            tuple(discovery_terms),
        )

    def __post_init__(self) -> None:
        required = (
            self.hypothesis_id,
            self.name,
            self.issuer_or_industry,
            self.observation,
            self.economic_mechanism,
            self.fundamental_target,
            self.earnings_bridge,
            self.abnormal_return_bridge,
        )
        if not all(required):
            raise ContractError("economic hypotheses require an explicit causal chain")
        if self.expected_direction not in {-1, 1}:
            raise ContractError("expected direction must be -1 or 1")
        if self.lead_min_days < 0 or self.lead_max_days < self.lead_min_days:
            raise ContractError("economic hypothesis lead range is invalid")
        if not self.discovery_terms:
            raise ContractError(
                "economic hypotheses require data-catalog discovery terms"
            )


@dataclass(frozen=True, slots=True)
class DatasetCatalogEntry:
    dataset_id: str
    provider: str
    source_uri: str
    license_id: str
    license_uri: str
    tags: tuple[str, ...]
    point_in_time: bool
    allows_research: bool
    allows_derived_features: bool
    contains_personal_data: bool
    license_verified_at: datetime | None
    monthly_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if not all(
            (
                self.dataset_id,
                self.provider,
                self.source_uri,
                self.license_id,
                self.license_uri,
            )
        ):
            raise ContractError(
                "catalog entries require provider, source, and license provenance"
            )
        if not self.tags:
            raise ContractError("catalog entries require searchable tags")
        if self.monthly_cost_usd < 0:
            raise ContractError("dataset cost cannot be negative")
        if (
            self.license_verified_at is not None
            and self.license_verified_at.tzinfo is None
        ):
            raise ContractError("license verification time must be timezone-aware")

    @property
    def eligible_for_research(self) -> bool:
        return (
            self.point_in_time
            and self.allows_research
            and self.allows_derived_features
            and not self.contains_personal_data
            and self.license_verified_at is not None
        )


class DatasetCatalog(Protocol):
    def search(self, terms: Sequence[str]) -> tuple[DatasetCatalogEntry, ...]:
        ...


class InMemoryDatasetCatalog:
    def __init__(self, entries: Iterable[DatasetCatalogEntry]) -> None:
        self._entries = tuple(entries)

    def search(self, terms: Sequence[str]) -> tuple[DatasetCatalogEntry, ...]:
        normalized = {term.lower() for term in terms}
        return tuple(
            entry
            for entry in self._entries
            if entry.dataset_id.lower() in normalized
            or normalized.intersection(tag.lower() for tag in entry.tags)
        )


class CatalogSearchTool:
    """Typed adapter through which a discovery subagent may search a supplied catalog."""

    def __init__(self, catalog: DatasetCatalog) -> None:
        self._catalog = catalog

    def execute(self, arguments: Mapping[str, object]) -> ToolResult:
        terms = arguments.get("terms")
        if not isinstance(terms, Sequence) or isinstance(terms, (str, bytes)):
            raise ContractError("catalog search requires a sequence of search terms")
        entries = self._catalog.search(tuple(str(term) for term in terms))
        return ToolResult(
            {
                "datasets": tuple(
                    {
                        "dataset_id": entry.dataset_id,
                        "provider": entry.provider,
                        "tags": entry.tags,
                        "eligible_for_research": entry.eligible_for_research,
                        "monthly_cost_usd": entry.monthly_cost_usd,
                    }
                    for entry in entries
                )
            }
        )


class LicenseLookupTool:
    """Exposes license facts without allowing an agent to approve or accept terms."""

    def __init__(self, catalog: DatasetCatalog) -> None:
        self._catalog = catalog

    def execute(self, arguments: Mapping[str, object]) -> ToolResult:
        dataset_id = arguments.get("dataset_id")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ContractError("license lookup requires a dataset_id")
        entries = self._catalog.search((dataset_id,))
        matching = next(
            (entry for entry in entries if entry.dataset_id == dataset_id), None
        )
        if matching is None:
            raise KeyError(dataset_id)
        return ToolResult(
            {
                "dataset_id": matching.dataset_id,
                "license_id": matching.license_id,
                "license_uri": matching.license_uri,
                "eligible_for_research": matching.eligible_for_research,
                "contains_personal_data": matching.contains_personal_data,
            }
        )


@dataclass(frozen=True, slots=True)
class DatasetCandidate:
    candidate_id: str
    hypothesis_id: str
    dataset: DatasetCatalogEntry
    status: DatasetStatus = DatasetStatus.DISCOVERED
    discovered_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.hypothesis_id:
            raise ContractError(
                "dataset candidates require candidate and hypothesis ids"
            )
        if self.discovered_at.tzinfo is None:
            raise ContractError("dataset discovery timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DatasetRegistryEvent:
    candidate_id: str
    from_status: DatasetStatus | None
    to_status: DatasetStatus
    actor: str
    reason: str
    event_hash: str


class DatasetRegistry:
    """Separate, independently governed registry for discovered external datasets."""

    def __init__(self) -> None:
        self._candidates: dict[str, DatasetCandidate] = {}
        self._events: ImmutableLedger[DatasetRegistryEvent] = ImmutableLedger()

    def discover(
        self, hypothesis: EconomicHypothesis, dataset: DatasetCatalogEntry
    ) -> DatasetCandidate:
        candidate = DatasetCandidate(str(uuid4()), hypothesis.hypothesis_id, dataset)
        self._candidates[candidate.candidate_id] = candidate
        self._event(
            candidate.candidate_id,
            None,
            DatasetStatus.DISCOVERED,
            "dataset-agent",
            "catalog match",
        )
        return candidate

    def approve(self, candidate_id: str, actor: str, reason: str) -> DatasetCandidate:
        if actor != "data-steward":
            raise PermissionError(
                "only an independent data steward may approve a dataset"
            )
        candidate = self._get(candidate_id)
        if candidate.status != DatasetStatus.DISCOVERED:
            raise ContractError("only discovered datasets may be approved")
        if not candidate.dataset.eligible_for_research:
            raise ContractError(
                "dataset lacks verified lawful, non-personal PIT research rights"
            )
        approved = DatasetCandidate(
            candidate.candidate_id,
            candidate.hypothesis_id,
            candidate.dataset,
            DatasetStatus.APPROVED,
            candidate.discovered_at,
        )
        self._candidates[candidate_id] = approved
        self._event(
            candidate_id,
            DatasetStatus.DISCOVERED,
            DatasetStatus.APPROVED,
            actor,
            reason,
        )
        return approved

    def reject(self, candidate_id: str, actor: str, reason: str) -> DatasetCandidate:
        candidate = self._get(candidate_id)
        if candidate.status != DatasetStatus.DISCOVERED:
            raise ContractError("only discovered datasets may be rejected")
        rejected = DatasetCandidate(
            candidate.candidate_id,
            candidate.hypothesis_id,
            candidate.dataset,
            DatasetStatus.REJECTED,
            candidate.discovered_at,
        )
        self._candidates[candidate_id] = rejected
        self._event(
            candidate_id,
            DatasetStatus.DISCOVERED,
            DatasetStatus.REJECTED,
            actor,
            reason,
        )
        return rejected

    def lineage(
        self, candidate_id: str, content_hash: str, schema_version: str
    ) -> DataLineage:
        candidate = self._get(candidate_id)
        if candidate.status != DatasetStatus.APPROVED:
            raise PermissionError("only approved datasets can create usable lineage")
        dataset = candidate.dataset
        return DataLineage(
            dataset.dataset_id,
            dataset.provider,
            dataset.license_id,
            dataset.source_uri,
            utc_now(),
            content_hash,
            schema_version,
            point_in_time=True,
        )

    def get(self, candidate_id: str) -> DatasetCandidate:
        return self._get(candidate_id)

    def events(self) -> tuple[LedgerEntry[DatasetRegistryEvent], ...]:
        return self._events.entries()

    def verify(self) -> bool:
        return self._events.verify()

    def _get(self, candidate_id: str) -> DatasetCandidate:
        try:
            return self._candidates[candidate_id]
        except KeyError as error:
            raise KeyError(candidate_id) from error

    def _event(
        self,
        candidate_id: str,
        from_status: DatasetStatus | None,
        to_status: DatasetStatus,
        actor: str,
        reason: str,
    ) -> None:
        payload = {
            "candidate_id": candidate_id,
            "from_status": from_status,
            "to_status": to_status,
            "actor": actor,
            "reason": reason,
        }
        self._events.append(
            DatasetRegistryEvent(
                candidate_id,
                from_status,
                to_status,
                actor,
                reason,
                canonical_hash(payload),
            )
        )


@dataclass(frozen=True, slots=True)
class PITFeatureDefinition:
    name: str
    dataset_candidate_id: str
    formula: str
    availability_lag_days: int

    def __post_init__(self) -> None:
        if not self.name or not self.dataset_candidate_id:
            raise ContractError(
                "PIT features require a name and approved dataset candidate"
            )
        Formula.parse(self.formula)
        if self.availability_lag_days < 0:
            raise ContractError("feature availability lag cannot be negative")


@dataclass(frozen=True, slots=True)
class RawDatasetObservation:
    asset: str
    observed_on: date
    published_at: datetime
    values: Mapping[str, float]

    def __post_init__(self) -> None:
        if not self.asset or not self.values:
            raise ContractError("raw observations require an asset and values")
        if self.published_at.tzinfo is None:
            raise ContractError("raw publication time must be timezone-aware")
        if self.published_at.date() < self.observed_on:
            raise ContractError(
                "raw observation cannot be published before it is observed"
            )


@dataclass(frozen=True, slots=True)
class FeatureBuildResult:
    lineage: DataLineage
    observations: tuple[PITObservation, ...]
    feature_hash: str


@dataclass(frozen=True, slots=True)
class AggregateDatasetObservation:
    """A raw row keyed to an external entity rather than directly to a stock."""

    source_key: str
    observed_on: date
    published_at: datetime
    values: Mapping[str, float]

    def __post_init__(self) -> None:
        if not self.source_key or not self.values:
            raise ContractError("aggregate observations require a source key and values")
        if self.published_at.tzinfo is None:
            raise ContractError("aggregate publication time must be timezone-aware")
        if self.published_at.date() < self.observed_on:
            raise ContractError("aggregate observation cannot publish before it is observed")


class AlternativeDatasetCreationAgent:
    """Factory B: proposes real-world proxies, but cannot approve data or alpha."""

    kind = AgentKind.ALTERNATIVE_DATASET_CREATOR

    def discover(
        self,
        job: ResearchJob,
        hypotheses: Sequence[EconomicHypothesis],
        catalog: DatasetCatalog,
        registry: DatasetRegistry,
    ) -> tuple[DatasetCandidate, ...]:
        if job.agent_kind != self.kind:
            raise ContractError(
                "dataset creation agent requires an alternative-dataset job"
            )
        discovered: list[DatasetCandidate] = []
        for hypothesis in hypotheses:
            for entry in catalog.search(hypothesis.discovery_terms):
                if entry.eligible_for_research:
                    discovered.append(registry.discover(hypothesis, entry))
        return tuple(discovered)

    def build_feature(
        self,
        definition: PITFeatureDefinition,
        raw_rows: Sequence[RawDatasetObservation],
        registry: DatasetRegistry,
        schema_version: str = "v1",
    ) -> FeatureBuildResult:
        candidate = registry.get(definition.dataset_candidate_id)
        if candidate.status != DatasetStatus.APPROVED:
            raise PermissionError(
                "feature building requires independent dataset approval"
            )
        formula = Formula.parse(definition.formula)
        formula.require_scalar()
        source_hash = canonical_hash(raw_rows)
        lineage = registry.lineage(candidate.candidate_id, source_hash, schema_version)
        observations: list[PITObservation] = []
        for row in raw_rows:
            value = formula.evaluate(
                {name: [float(item)] for name, item in row.values.items()}
            )[0]
            available_at = row.published_at + timedelta(
                days=definition.availability_lag_days
            )
            observations.append(
                PITObservation(
                    row.asset,
                    definition.name,
                    value,
                    row.observed_on,
                    available_at,
                    lineage.dataset_id,
                )
            )
        return FeatureBuildResult(
            lineage,
            tuple(observations),
            canonical_hash({"definition": definition, "source_hash": source_hash}),
        )

    def build_mapped_feature(
        self,
        definition: PITFeatureDefinition,
        raw_rows: Sequence[AggregateDatasetObservation],
        registry: DatasetRegistry,
        exposure_map: AssetExposureMap,
        mapper: PointInTimeAssetMapper,
        decision_date: date,
        decision_time: datetime,
        schema_version: str = "v1",
    ) -> tuple[DataLineage, tuple[MappedFeatureObservation, ...], str]:
        """Build a source-keyed feature then expand it through an approved PIT map."""
        candidate = registry.get(definition.dataset_candidate_id)
        if candidate.status != DatasetStatus.APPROVED:
            raise PermissionError("mapped feature building requires independent dataset approval")
        if exposure_map.proposal.dataset_id != candidate.dataset.dataset_id:
            raise ContractError("exposure map and dataset feature have different datasets")
        formula = Formula.parse(definition.formula)
        formula.require_scalar()
        source_hash = canonical_hash(raw_rows)
        lineage = registry.lineage(candidate.candidate_id, source_hash, schema_version)
        source_features = tuple(
            ExternalFeatureObservation(
                candidate.dataset.dataset_id,
                definition.name,
                row.source_key,
                formula.evaluate({name: [float(value)] for name, value in row.values.items()})[0],
                row.observed_on,
                row.published_at + timedelta(days=definition.availability_lag_days),
                lineage.dataset_id,
            )
            for row in raw_rows
        )
        mapped = mapper.map(exposure_map, source_features, decision_date, decision_time)
        return lineage, mapped, canonical_hash({"definition": definition, "source": source_hash, "map": exposure_map})

    def alpha_hypothesis(
        self,
        hypothesis: EconomicHypothesis,
        definition: PITFeatureDefinition,
        registry: DatasetRegistry,
    ):
        """Handoff to the shared Alpha Registry path after independent data approval.

        This prepares an alpha proposal only; it does not evaluate or accept it.
        """
        from .agents import AlternativeHypothesis

        candidate = registry.get(definition.dataset_candidate_id)
        if candidate.status != DatasetStatus.APPROVED:
            raise PermissionError("alpha handoff requires an approved dataset")
        dataset = candidate.dataset
        return AlternativeHypothesis(
            name=hypothesis.name,
            economic_mechanism=hypothesis.economic_mechanism,
            dataset_id=dataset.dataset_id,
            provider=dataset.provider,
            license_id=dataset.license_id,
            publication_lag_days=definition.availability_lag_days,
            feature_definition=definition.formula,
        )


@dataclass(frozen=True, slots=True)
class SemanticEvidence:
    """OOS evidence for the full proxy -> earnings -> return causal chain."""

    fundamental_information: float
    earnings_surprise_information: float
    abnormal_return_information: float
    library_similarity: float
    monthly_data_cost_usd: float
    complexity: float
    instability: float

    def __post_init__(self) -> None:
        if not 0 <= self.library_similarity <= 1:
            raise ContractError("library similarity must be in [0, 1]")
        if min(self.monthly_data_cost_usd, self.complexity, self.instability) < 0:
            raise ContractError("semantic evidence penalties cannot be negative")


@dataclass(frozen=True, slots=True)
class SemanticRewardPolicy:
    cost_scale_usd: float = 1_000.0
    similarity_weight: float = 1.0
    complexity_weight: float = 0.05
    instability_weight: float = 1.0

    def score(self, evidence: SemanticEvidence) -> float:
        if self.cost_scale_usd <= 0:
            raise ContractError("cost scale must be positive")
        return (
            evidence.abnormal_return_information
            + 0.5 * evidence.earnings_surprise_information
            + 0.25 * evidence.fundamental_information
            - self.similarity_weight * evidence.library_similarity
            - evidence.monthly_data_cost_usd / self.cost_scale_usd
            - self.complexity_weight * evidence.complexity
            - self.instability_weight * evidence.instability
        )
