"""Point-in-time applicability mapping from external observations to securities.

An alternative-data observation is often about a place, route, facility, product
category, industry, or issuer identifier rather than a tradable security.  This
module keeps that distinction explicit.  A research agent may propose a mapping,
but an independent data steward must approve a dated exposure map before it can
expand an observation into a security-level feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from math import isfinite
from typing import Iterable, Mapping, Sequence
from uuid import uuid4

from .contracts import ContractError, canonical_hash, utc_now
from .pit import PITObservation, UniverseMembership


class ExposureKind(str, Enum):
    """Economic relationship between a source entity and a listed security."""

    DIRECT_ISSUER = "direct_issuer"
    SECTOR = "sector"
    INDUSTRY = "industry"
    BUSINESS_SEGMENT = "business_segment"
    GEOGRAPHY = "geography"
    FACILITY = "facility"
    ROUTE_OR_HUB = "route_or_hub"
    SUPPLY_CHAIN = "supply_chain"
    COMPETITOR = "competitor"


class MappingStatus(str, Enum):
    DISCOVERED = "discovered"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AssetMappingProposal:
    """A falsifiable proposal for how a non-security observation reaches assets."""

    proposal_id: str
    hypothesis_id: str
    dataset_id: str
    feature: str
    source_key_description: str
    exposure_kind: ExposureKind
    mapping_method: str
    economic_mechanism: str
    required_evidence: tuple[str, ...]
    created_at: datetime = field(default_factory=utc_now)

    @classmethod
    def new(
        cls,
        hypothesis_id: str,
        dataset_id: str,
        feature: str,
        source_key_description: str,
        exposure_kind: ExposureKind,
        mapping_method: str,
        economic_mechanism: str,
        required_evidence: Sequence[str],
    ) -> "AssetMappingProposal":
        return cls(
            str(uuid4()), hypothesis_id, dataset_id, feature, source_key_description,
            exposure_kind, mapping_method, economic_mechanism, tuple(required_evidence)
        )

    def __post_init__(self) -> None:
        if not all((
            self.proposal_id, self.hypothesis_id, self.dataset_id, self.feature,
            self.source_key_description, self.mapping_method, self.economic_mechanism,
        )):
            raise ContractError("asset mapping proposals require a complete causal mapping")
        if not self.required_evidence or any(not item for item in self.required_evidence):
            raise ContractError("asset mapping proposals require reviewable evidence")
        if self.created_at.tzinfo is None:
            raise ContractError("asset mapping proposal time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AssetExposure:
    """A dated, evidenced exposure of a listed security to one source key.

    ``source_key`` can be a CIK (accounting), NAICS/GICS identifier, county,
    facility ID, airport hub, route, retailer category, or a provider-specific
    stable location ID.  The map records its own availability: a later segment
    disclosure cannot be used to rewrite an earlier exposure.
    """

    asset: str
    source_key: str
    exposure_kind: ExposureKind
    weight: float
    valid_from: date
    valid_to: date | None
    available_at: datetime
    evidence_uri: str
    mapping_lineage_id: str

    def __post_init__(self) -> None:
        if not all((self.asset, self.source_key, self.evidence_uri, self.mapping_lineage_id)):
            raise ContractError("asset exposures require asset, source key, evidence, and lineage")
        if not isfinite(self.weight) or self.weight == 0 or abs(self.weight) > 1:
            raise ContractError("asset exposure weight must be finite, non-zero, and in [-1, 1]")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ContractError("asset exposure validity range is invalid")
        if self.available_at.tzinfo is None:
            raise ContractError("asset exposure availability must be timezone-aware")

    def contains(self, when: date) -> bool:
        return self.valid_from <= when and (self.valid_to is None or when <= self.valid_to)


@dataclass(frozen=True, slots=True)
class AssetExposureMap:
    """An immutable candidate map that must be steward-approved before use."""

    map_id: str
    proposal: AssetMappingProposal
    exposures: tuple[AssetExposure, ...]
    mapping_lineage_ids: tuple[str, ...]
    status: MappingStatus = MappingStatus.DISCOVERED
    submitted_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.map_id or not self.exposures or not self.mapping_lineage_ids:
            raise ContractError("asset exposure maps require records and mapping lineage")
        if any(exposure.mapping_lineage_id not in self.mapping_lineage_ids for exposure in self.exposures):
            raise ContractError("each exposure must reference declared mapping lineage")
        if self.submitted_at.tzinfo is None:
            raise ContractError("asset map submission time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AssetMapEvent:
    map_id: str
    from_status: MappingStatus | None
    to_status: MappingStatus
    actor: str
    reason: str
    event_hash: str


class AssetMappingRegistry:
    """An append-only approval boundary for source-to-security maps."""

    def __init__(self) -> None:
        self._maps: dict[str, AssetExposureMap] = {}
        self._events: list[AssetMapEvent] = []

    def submit(
        self,
        proposal: AssetMappingProposal,
        exposures: Iterable[AssetExposure],
        mapping_lineage_ids: Sequence[str],
    ) -> AssetExposureMap:
        record = AssetExposureMap(
            str(uuid4()), proposal, tuple(exposures), tuple(mapping_lineage_ids)
        )
        self._maps[record.map_id] = record
        self._event(record.map_id, None, MappingStatus.DISCOVERED, "mapping-agent", "proposed mapping")
        return record

    def approve(
        self,
        map_id: str,
        actor: str,
        reason: str,
        approved_dataset_id: str,
    ) -> AssetExposureMap:
        if actor != "data-steward":
            raise PermissionError("only an independent data steward may approve an asset map")
        current = self.get(map_id)
        if current.status != MappingStatus.DISCOVERED:
            raise ContractError("only discovered asset maps may be approved")
        if current.proposal.dataset_id != approved_dataset_id:
            raise ContractError("asset map does not match the independently approved dataset")
        approved = AssetExposureMap(
            current.map_id, current.proposal, current.exposures, current.mapping_lineage_ids,
            MappingStatus.APPROVED, current.submitted_at,
        )
        self._maps[map_id] = approved
        self._event(map_id, MappingStatus.DISCOVERED, MappingStatus.APPROVED, actor, reason)
        return approved

    def reject(self, map_id: str, actor: str, reason: str) -> AssetExposureMap:
        current = self.get(map_id)
        if current.status != MappingStatus.DISCOVERED:
            raise ContractError("only discovered asset maps may be rejected")
        rejected = AssetExposureMap(
            current.map_id, current.proposal, current.exposures, current.mapping_lineage_ids,
            MappingStatus.REJECTED, current.submitted_at,
        )
        self._maps[map_id] = rejected
        self._event(map_id, MappingStatus.DISCOVERED, MappingStatus.REJECTED, actor, reason)
        return rejected

    def get(self, map_id: str) -> AssetExposureMap:
        try:
            return self._maps[map_id]
        except KeyError as error:
            raise KeyError(map_id) from error

    def events(self) -> tuple[AssetMapEvent, ...]:
        return tuple(self._events)

    def _event(
        self,
        map_id: str,
        from_status: MappingStatus | None,
        to_status: MappingStatus,
        actor: str,
        reason: str,
    ) -> None:
        payload = {"map_id": map_id, "from": from_status, "to": to_status, "actor": actor, "reason": reason}
        self._events.append(AssetMapEvent(map_id, from_status, to_status, actor, reason, canonical_hash(payload)))


@dataclass(frozen=True, slots=True)
class ExternalFeatureObservation:
    """One first-published external feature, still keyed to its source entity."""

    dataset_id: str
    feature: str
    source_key: str
    value: float
    observed_on: date
    published_at: datetime
    lineage_id: str

    def __post_init__(self) -> None:
        if not all((self.dataset_id, self.feature, self.source_key, self.lineage_id)):
            raise ContractError("external observations require dataset, feature, source key, and lineage")
        if not isfinite(self.value):
            raise ContractError("external observation values must be finite")
        if self.published_at.tzinfo is None:
            raise ContractError("external publication time must be timezone-aware")
        if self.published_at.date() < self.observed_on:
            raise ContractError("external observation cannot publish before it is observed")


@dataclass(frozen=True, slots=True)
class MappedFeatureObservation:
    """A security feature plus both source-data and mapping provenance."""

    observation: PITObservation
    map_id: str
    exposure_kind: ExposureKind
    source_key: str
    source_lineage_id: str
    mapping_lineage_id: str


class PointInTimeAssetMapper:
    """Expands approved external observations only where they were knowably applicable."""

    def __init__(self, memberships: Iterable[UniverseMembership]) -> None:
        self._memberships = tuple(memberships)

    def map(
        self,
        exposure_map: AssetExposureMap,
        observations: Iterable[ExternalFeatureObservation],
        decision_date: date,
        decision_time: datetime,
    ) -> tuple[MappedFeatureObservation, ...]:
        if decision_time.tzinfo is None:
            raise ContractError("mapping decision time must be timezone-aware")
        if exposure_map.status != MappingStatus.APPROVED:
            raise PermissionError("external observations require an independently approved asset map")
        output: list[MappedFeatureObservation] = []
        relevant = [
            exposure for exposure in exposure_map.exposures
            if exposure.contains(decision_date) and exposure.available_at <= decision_time
            and self._is_member(exposure.asset, decision_date)
        ]
        for source in observations:
            if source.dataset_id != exposure_map.proposal.dataset_id or source.feature != exposure_map.proposal.feature:
                continue
            if source.observed_on > decision_date or source.published_at > decision_time:
                continue
            for exposure in relevant:
                if exposure.source_key != source.source_key:
                    continue
                available_at = max(source.published_at, exposure.available_at)
                output.append(
                    MappedFeatureObservation(
                        PITObservation(
                            exposure.asset, source.feature, source.value * exposure.weight,
                            source.observed_on, available_at, source.lineage_id,
                        ),
                        exposure_map.map_id, exposure.exposure_kind, source.source_key,
                        source.lineage_id, exposure.mapping_lineage_id,
                    )
                )
        return tuple(output)

    def _is_member(self, asset: str, when: date) -> bool:
        return any(member.asset == asset and member.contains(when) for member in self._memberships)
