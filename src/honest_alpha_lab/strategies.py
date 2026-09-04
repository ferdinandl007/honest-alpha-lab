"""Research-only strategy sleeves derived from the immutable alpha proposals."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from .contracts import ContractError


class StrategyFamily(str, Enum):
    SYMBOLIC = "symbolic"
    EVENT = "event"
    ALTERNATIVE = "alternative"


class StrategySide(str, Enum):
    LONG_ONLY = "long_only"
    SHORT_ONLY = "short_only"
    LONG_SHORT = "long_short"


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    strategy_id: str
    name: str
    family: StrategyFamily
    signal_description: str
    required_inputs: tuple[str, ...]
    horizon_days: tuple[int, ...]
    side: StrategySide
    rebalance_days: int
    requires_borrow: bool = False

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.name or not self.required_inputs:
            raise ContractError("strategy definitions need an id, name, and required inputs")
        if not self.horizon_days or any(value <= 0 for value in self.horizon_days):
            raise ContractError("strategy horizons must be positive")
        if self.rebalance_days <= 0:
            raise ContractError("strategy rebalance interval must be positive")


def research_strategy_catalog() -> Mapping[str, StrategyDefinition]:
    """The 15 mined proposals as testable sleeves, not approved strategies."""
    strategies = (
        StrategyDefinition("revision_low_crowding", "Revision Confirmation Under Low Crowding", StrategyFamily.SYMBOLIC, "Positive estimate revisions with medium-term confirmation, filtered for lower short interest.", ("earnings_revision_20d", "ret_20d", "short_interest"), (20, 60), StrategySide.LONG_SHORT, 20, True),
        StrategyDefinition("profitable_growth", "Profitable Growth Discipline", StrategyFamily.SYMBOLIC, "Profitability plus restrained asset growth and revision confirmation.", ("gross_profitability_ttm", "asset_growth_1y", "earnings_revision_20d"), (60,), StrategySide.LONG_SHORT, 20, True),
        StrategyDefinition("cash_yield_credibility", "Cash-Yield Credibility Through Revisions", StrategyFamily.SYMBOLIC, "Cash-flow yield conditioned on improving revisions and low accruals.", ("fcf_yield", "earnings_revision_20d", "accruals_ttm"), (20, 60), StrategySide.LONG_SHORT, 20, True),
        StrategyDefinition("low_beta_drift", "Low-Beta Information Drift", StrategyFamily.SYMBOLIC, "Confirmed return/revision drift with low beta.", ("ret_20d", "ret_60d", "earnings_revision_20d", "beta_252d"), (60,), StrategySide.LONG_SHORT, 20, True),
        StrategyDefinition("short_crowding_fragility", "Liquidity-Scaled Short Crowding Fragility", StrategyFamily.SYMBOLIC, "Deteriorating revisions and price trend where short interest is high relative to trading capacity.", ("short_interest", "dollar_volume_20d", "market_cap", "earnings_revision_20d", "ret_20d"), (20, 60), StrategySide.SHORT_ONLY, 10, True),
        StrategyDefinition("debt_default_shock", "Uncured Debt Default Disclosure Shock", StrategyFamily.EVENT, "Short an issuer after a contemporaneously accepted uncured Item 2.04 default disclosure.", ("edgar_acceptance_time", "8k_item_2_04_event", "borrow_availability"), (5, 20, 60), StrategySide.SHORT_ONLY, 1, True),
        StrategyDefinition("auditor_conflict", "Auditor Departure With Unresolved Reporting Conflict", StrategyFamily.EVENT, "Short conflict-qualified auditor departure events; exclude clean rotations.", ("edgar_acceptance_time", "8k_item_4_01_event", "borrow_availability"), (20, 60), StrategySide.SHORT_ONLY, 1, True),
        StrategyDefinition("going_concern", "Explicit Going-Concern Substantial-Doubt Flag", StrategyFamily.EVENT, "Short unresolved going-concern disclosures at filing acceptance.", ("edgar_acceptance_time", "going_concern_event", "borrow_availability"), (20, 60), StrategySide.SHORT_ONLY, 1, True),
        StrategyDefinition("material_weakness", "Unremediated Material-Weakness Reporting Penalty", StrategyFamily.EVENT, "Short unremediated material-weakness disclosures, net of concurrent events.", ("edgar_acceptance_time", "material_weakness_event", "borrow_availability"), (20, 60), StrategySide.SHORT_ONLY, 1, True),
        StrategyDefinition("exercise_retain", "Insider Option Exercise With Full Share Retention", StrategyFamily.EVENT, "Long Form 4 option exercises where acquired shares are substantially retained.", ("edgar_acceptance_time", "form4_exercise_retain_event"), (20, 60), StrategySide.LONG_ONLY, 1),
        StrategyDefinition("refinery_maintenance", "Refinery Maintenance Shock and Downstream Margin Pressure", StrategyFamily.ALTERNATIVE, "Map EIA refinery utilization/inventory surprises to a predeclared refinery basket.", ("eia_release_time", "eia_utilization", "eia_inventory", "issuer_basket_map"), (5, 20), StrategySide.LONG_SHORT, 5, True),
        StrategyDefinition("freight_pulse", "Freight Momentum as an Industrial Shipment Pulse", StrategyFamily.ALTERNATIVE, "Map BTS freight acceleration to a predeclared industrial/logistics basket.", ("bts_release_time", "bts_freight_tsi", "issuer_basket_map"), (20, 60), StrategySide.LONG_SHORT, 20, True),
        StrategyDefinition("tsa_airline", "Checkpoint Throughput Pulse for Airline Passenger Revenue", StrategyFamily.ALTERNATIVE, "Map PIT TSA passenger throughput surprises to airline domestic-exposure weights.", ("tsa_publication_time", "tsa_passenger_volume", "airline_exposure_map"), (5, 20), StrategySide.LONG_SHORT, 5, True),
        StrategyDefinition("retail_divergence", "Retail Category Divergence and Inventory-Demand Mismatch", StrategyFamily.ALTERNATIVE, "Map Census category sales surprises to retailer category exposure.", ("census_release_time", "census_retail_category_sales", "retailer_exposure_map"), (20, 60), StrategySide.LONG_SHORT, 20, True),
        StrategyDefinition("weather_flight_disruption", "Weather-Conditioned Flight Disruption Cost Proxy", StrategyFamily.ALTERNATIVE, "Map first-published weather and FAA operations stress to airline hub exposure.", ("noaa_observation_vintage", "faa_publication_time", "airline_hub_exposure_map"), (5, 20), StrategySide.SHORT_ONLY, 1),
    )
    catalog = {strategy.strategy_id: strategy for strategy in strategies}
    if len(catalog) != 15:
        raise ContractError("research strategy catalog must contain exactly 15 unique sleeves")
    return catalog
