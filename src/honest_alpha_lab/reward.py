"""Research-quality reward scoring for real out-of-sample alpha evaluations."""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import sqrt
from statistics import mean
from typing import Mapping, Sequence

from .contracts import AgentKind, ContractError
from .evaluation import EvaluationResult, _require_finite, stability_by_fold, trial_adjusted_pvalue


@dataclass(frozen=True, slots=True)
class RewardEvidence:
    """Numerical-service output required before an agent can receive reward credit."""

    candidate_id: str
    agent_kind: AgentKind
    evaluation: EvaluationResult
    raw_p_value: float
    trial_count: int
    complexity: float
    capacity_proxy_usd: float
    point_in_time_verified: bool
    lineage_verified: bool
    regime_rank_ics: tuple[float, ...] = ()
    incremental_rank_ic: float | None = None
    semantic_chain_verified: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.agent_kind, AgentKind):
            raise ContractError("reward evidence requires a recognized AgentKind")
        if not isinstance(self.evaluation, EvaluationResult):
            raise ContractError("reward evidence requires an EvaluationResult")
        self.evaluation.__post_init__()
        if self.candidate_id != self.evaluation.candidate_id:
            raise ContractError("reward candidate and evaluation must match")
        trial_adjusted_pvalue(self.raw_p_value, self.trial_count)
        _require_finite("complexity and capacity", (self.complexity, self.capacity_proxy_usd))
        if self.complexity < 0 or self.capacity_proxy_usd < 0:
            raise ContractError("complexity and capacity must be non-negative")
        if self.incremental_rank_ic is not None:
            _require_finite("incremental rank IC", (self.incremental_rank_ic,))
            # Paired augmented-model IC minus fixed-library model IC.
            if not -2 <= self.incremental_rank_ic <= 2:
                raise ContractError("incremental rank IC difference must be in [-2, 2]")
        if not isinstance(self.regime_rank_ics, (tuple, list)):
            raise ContractError("regime rank ICs must be a sequence")
        object.__setattr__(self, "regime_rank_ics", tuple(self.regime_rank_ics))
        _require_finite("regime rank ICs", self.regime_rank_ics)
        if any(not -1 <= value <= 1 for value in self.regime_rank_ics):
            raise ContractError("regime rank ICs must be in [-1, 1]")


@dataclass(frozen=True, slots=True)
class ResearchRewardPolicy:
    min_rank_ic: float = 0.005
    min_incremental_rank_ic: float = 0.001
    min_stability: float = 0.60
    max_adjusted_p_value: float = 0.10
    max_similarity: float = 0.80
    max_turnover: float = 1.50
    min_capacity_proxy_usd: float = 1_000_000.0
    cost_scale_usd: float = 1_000.0
    information_weight: float = 100.0
    incremental_information_weight: float = 100.0
    stability_weight: float = 0.25
    regime_weight: float = 25.0
    similarity_weight: float = 1.0
    turnover_weight: float = 0.10
    cost_weight: float = 1.0
    complexity_weight: float = 0.05

    def __post_init__(self) -> None:
        _require_finite("reward policy", tuple(getattr(self, field.name) for field in fields(self)))
        if any(getattr(self, field.name) < 0 for field in fields(self)):
            raise ContractError("reward policy thresholds and weights must be non-negative")
        if self.min_rank_ic < 0 or not 0 <= self.min_stability <= 1:
            raise ContractError("reward policy IC and stability thresholds are invalid")
        if not 0 < self.max_adjusted_p_value <= 1 or not 0 <= self.max_similarity <= 1:
            raise ContractError("reward policy p-value or similarity threshold is invalid")
        if self.max_turnover < 0 or self.min_capacity_proxy_usd < 0 or self.cost_scale_usd <= 0:
            raise ContractError("reward policy cost, turnover, or capacity threshold is invalid")


@dataclass(frozen=True, slots=True)
class RewardResult:
    candidate_id: str
    reward: float
    eligible_for_registry_validation: bool
    adjusted_p_value: float
    stability: float
    incremental_rank_ic: float | None
    regime_floor_rank_ic: float | None
    gates: Mapping[str, bool]
    components: Mapping[str, float]

    @property
    def eligible_for_registry_acceptance(self) -> bool:
        """Legacy report accessor; always false for development harness outputs.

        This report is not a sealed-test receipt or registry authorization.
        """
        return (
            self.eligible_for_registry_validation is True
            and self.gates.get("sealed_evaluation") is True
        )


class ResearchRewardHarness:
    """DEVELOPMENT feedback only; sealed outcomes belong to the final validator.

    Evidence flags are attestations, not authenticated service identities. Diagnostic
    components remain visible on failed gates, but no scalar credit is awarded.
    """

    def __init__(self, policy: ResearchRewardPolicy | None = None) -> None:
        self.policy = policy or ResearchRewardPolicy()

    def score(self, evidence: RewardEvidence) -> RewardResult:
        evidence.__post_init__()
        evaluation = evidence.evaluation
        if evaluation.sealed:
            raise ContractError("scheduling rewards require DEVELOPMENT evidence; sealed outcomes are forbidden")
        if not evaluation.fold_metrics:
            raise ContractError("reward scoring requires walk-forward fold metrics")
        adjusted_p = trial_adjusted_pvalue(evidence.raw_p_value, evidence.trial_count)
        stability = stability_by_fold(evaluation.fold_metrics)
        incremental_ic = evidence.incremental_rank_ic
        regime_floor = min(evidence.regime_rank_ics) if evidence.regime_rank_ics else None
        gates = {
            "point_in_time_verified": evidence.point_in_time_verified is True,
            "lineage_verified": evidence.lineage_verified is True,
            "sealed_evaluation": evaluation.sealed,
            "persistent_folds": evaluation.persistent,
            "rank_ic": evaluation.out_of_sample_rank_ic >= self.policy.min_rank_ic,
            "incremental_evidence": incremental_ic is not None,
            "incremental_information": (
                incremental_ic is not None and incremental_ic >= self.policy.min_incremental_rank_ic
            ),
            # Conditional signals may fail in some states, but missing state
            # evidence cannot be treated as demonstrated robustness.
            "market_state_evidence": len(evidence.regime_rank_ics) >= 2,
            "stability": stability >= self.policy.min_stability,
            "multiple_testing": adjusted_p <= self.policy.max_adjusted_p_value,
            "similarity": evaluation.similarity <= self.policy.max_similarity,
            "turnover": evaluation.turnover <= self.policy.max_turnover,
            "capacity": evidence.capacity_proxy_usd >= self.policy.min_capacity_proxy_usd,
            "semantic_chain": (
                evidence.semantic_chain_verified is True
                if evidence.agent_kind in {AgentKind.ALTERNATIVE_DATA, AgentKind.ALTERNATIVE_DATASET_CREATOR}
                else True
            ),
        }
        regime_bonus = 0.0
        if evidence.regime_rank_ics:
            regime_bonus = max(0.0, mean(evidence.regime_rank_ics)) * self.policy.regime_weight
        components = {
            "oos_information": evaluation.out_of_sample_rank_ic * self.policy.information_weight,
            "incremental_information": (
                incremental_ic * self.policy.incremental_information_weight
                if incremental_ic is not None else 0.0
            ),
            "stability": stability * self.policy.stability_weight,
            "regime": regime_bonus,
            "similarity_penalty": -evaluation.similarity * self.policy.similarity_weight,
            "turnover_penalty": -evaluation.turnover * self.policy.turnover_weight,
            "data_cost_penalty": -evaluation.data_cost_usd / self.policy.cost_scale_usd * self.policy.cost_weight,
            "complexity_penalty": -evidence.complexity * self.policy.complexity_weight,
            "trial_count_diagnostic": -sqrt(evidence.trial_count) / 100.0,
        }
        reward = sum(components.values())
        _require_finite("reward components and total", (*components.values(), reward))
        validation_gates = {name: passed for name, passed in gates.items() if name != "sealed_evaluation"}
        eligible = all(validation_gates.values())
        return RewardResult(
            candidate_id=evidence.candidate_id,
            reward=reward if eligible else 0.0,
            eligible_for_registry_validation=eligible,
            adjusted_p_value=adjusted_p,
            stability=stability,
            incremental_rank_ic=incremental_ic,
            regime_floor_rank_ic=regime_floor,
            gates=gates,
            components=components,
        )

    def leaderboard(self, evidence: Sequence[RewardEvidence]) -> tuple[RewardResult, ...]:
        """Rank immutable evidence records, with eligible candidates shown first."""
        results = [self.score(record) for record in evidence]
        return tuple(
            sorted(
                results,
                key=lambda result: (result.eligible_for_registry_validation, result.reward),
                reverse=True,
            )
        )
