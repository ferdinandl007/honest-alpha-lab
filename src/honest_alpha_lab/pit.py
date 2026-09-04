"""Point-in-time data and residual target construction primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from typing import Iterable, Mapping

from .contracts import ContractError, DataLineage, canonical_hash


@dataclass(frozen=True, slots=True)
class PITObservation:
    asset: str
    feature: str
    value: float
    as_of: date
    available_at: datetime
    lineage_id: str

    def __post_init__(self) -> None:
        if not self.asset or not self.feature or not self.lineage_id:
            raise ContractError("PIT observations require asset, feature, and lineage")
        if not isfinite(self.value):
            raise ContractError("PIT observations must contain finite values")
        if self.available_at.tzinfo is None:
            raise ContractError("available_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class UniverseMembership:
    asset: str
    valid_from: date
    valid_to: date | None = None

    def contains(self, when: date) -> bool:
        return self.valid_from <= when and (
            self.valid_to is None or when <= self.valid_to
        )


@dataclass(frozen=True, slots=True)
class ReturnObservation:
    asset: str
    trade_date: date
    return_value: float
    sector: str
    sector_return: float
    market_beta: float


class PITStore:
    """Small reference store that enforces publication-time availability at query time."""

    def __init__(
        self, lineages: Iterable[DataLineage], memberships: Iterable[UniverseMembership]
    ) -> None:
        self._lineages = {lineage.dataset_id: lineage for lineage in lineages}
        self._memberships = tuple(memberships)
        self._observations: list[PITObservation] = []

    def add(self, observation: PITObservation) -> None:
        if observation.lineage_id not in self._lineages:
            raise ContractError("observation references unknown lineage")
        self._observations.append(observation)

    def query(
        self, asset: str, feature: str, decision_date: date, decision_time: datetime
    ) -> tuple[PITObservation, ...]:
        if decision_time.tzinfo is None:
            raise ContractError("decision_time must be timezone-aware")
        if (
            any(
                m.asset == asset and m.contains(decision_date)
                for m in self._memberships
            )
            is False
        ):
            return ()
        return tuple(
            o
            for o in self._observations
            if o.asset == asset
            and o.feature == feature
            and o.as_of <= decision_date
            and o.available_at <= decision_time
        )

    def snapshot_hash(self) -> str:
        return canonical_hash(
            {
                "lineages": self._lineages,
                "memberships": self._memberships,
                "observations": tuple(self._observations),
            }
        )


def residual_return(
    stock_return: float, sector_return: float, market_beta: float, market_return: float
) -> float:
    """Default target: stock minus sector minus estimated market-beta contribution."""
    return stock_return - sector_return - market_beta * market_return


def build_residual_targets(
    rows: Iterable[ReturnObservation], market_returns: Mapping[date, float]
) -> dict[tuple[str, date], float]:
    result: dict[tuple[str, date], float] = {}
    for row in rows:
        if row.trade_date not in market_returns:
            raise ContractError(f"missing market return for {row.trade_date}")
        result[(row.asset, row.trade_date)] = residual_return(
            row.return_value,
            row.sector_return,
            row.market_beta,
            market_returns[row.trade_date],
        )
    return result
