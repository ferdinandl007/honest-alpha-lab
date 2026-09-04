"""Mandatory robustness rubric for proposal-only alpha candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .contracts import AgentKind, AlphaCandidate, EvaluationPolicy, canonical_hash


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    check_id: str
    category: str
    requirement: str
    pass_criterion: str
    required_evidence: str


@dataclass(frozen=True, slots=True)
class ValidationPlan:
    plan_id: str
    candidate_id: str
    snapshot_hash: str
    policy_hash: str
    agent_kind: AgentKind
    checks: tuple[ValidationCheck, ...]


class RobustnessRubric:
    """Builds the fixed validation requirements before any result is observed."""

    def __init__(self, policy: EvaluationPolicy | None = None) -> None:
        self.policy = policy or EvaluationPolicy()

    def plan(self, candidate: AlphaCandidate) -> ValidationPlan:
        checks = (*self._common_checks(), *self._agent_checks(candidate.agent_kind))
        policy_hash = canonical_hash(self.policy)
        identity = canonical_hash(
            {
                "candidate_id": candidate.candidate_id,
                "snapshot_hash": candidate.input_snapshot_hash,
                "policy_hash": policy_hash,
                "checks": checks,
            }
        )
        return ValidationPlan(
            identity, candidate.candidate_id, candidate.input_snapshot_hash, policy_hash,
            candidate.agent_kind, checks
        )

    def _common_checks(self) -> Sequence[ValidationCheck]:
        horizons = ", ".join(str(value) for value in self.policy.horizons)
        return (
            ValidationCheck("pit-lineage", "data integrity", "Freeze the declared data, code, historical universe membership, delisting returns, and each source's first eligible timestamp.", "Features and universe knowledge precede the decision; future labels are outcomes only and cannot enter training before their outcome interval ends.", "Signed snapshot manifest, source-vintage hashes, and PIT leakage audit."),
            ValidationCheck("purged-walk-forward", "out-of-sample", f"Use expanding purged walk-forward folds for {horizons}-trading-day residual-return horizons; purge covers the longest label and embargo is locked.", "All reported model choices and thresholds are fitted only on prior folds.", "Fold-level dates, scores, residual returns, and pre-registered policy hash."),
            ValidationCheck("temporal-persistence", "stability", "Measure rank IC, spread return, decay/half-life, and sign persistence by fold and non-overlapping calendar eras.", "Positive performance is not confined to one era or corporate/event cycle; failures are retained.", "Fold table, confidence intervals/bootstrap, and decay curve."),
            ValidationCheck("market-state-probabilities", "market conditions", "Infer latent-state probabilities from returns, volatility, spreads, breadth, correlations, liquidity, and rates with a Gaussian HMM; compare with a non-Markov clustering baseline.", "Report probability-weighted IC and effective sample size for every state. A conditional alpha must declare a usable probability rule; no hard bull/bear labels.", "Per-state metrics, state probabilities, HMM/clustering comparison, and transition/stability diagnostics."),
            ValidationCheck("cross-section-robustness", "market conditions", "Re-run across sector, size, liquidity, price, volatility, and calendar subuniverses using historically correct eligibility.", "Information is not an artifact of one sector, microcap tail, illiquid names, or a small set of issuers.", "Subuniverse IC table, concentration statistics, and exclusions log."),
            ValidationCheck("incrementality", "library overlap", "Compare score correlation and incremental OOS information against every accepted and pending library alpha.", "Similarity and marginal contribution satisfy the locked policy; a duplicate is rejected or explicitly merged before a new trial is counted.", "Similarity matrix, residualized IC, and candidate-to-library linkage."),
            ValidationCheck("multiple-testing", "statistical discipline", "Record the global trial count before inspecting results and apply the locked family-wise/deflated-performance correction.", "Adjusted significance passes the pre-registered threshold; failed variants remain in the trial ledger.", "Immutable trial ledger and raw/adjusted p-values."),
            ValidationCheck("implementation-economics", "trading realism", "Estimate turnover, delay, fees/slippage, liquidity participation, capacity proxy, borrow/short availability where relevant, and signal decay.", "Net evidence remains after conservative costs and the candidate has an actionable holding/refresh rule.", "Gross/net return, turnover, capacity, cost, and decay report."),
            ValidationCheck("sealed-test", "confirmation", "After numerical validation, run exactly once on the separately sealed final period using the locked snapshot and policy.", "The predeclared decision rule passes without code, feature, or threshold changes.", "Sealed artifact hash, one-use gate trace, and final result."),
        )

    def _agent_checks(self, kind: AgentKind) -> Sequence[ValidationCheck]:
        if kind == AgentKind.SYMBOLIC_FACTOR:
            return (ValidationCheck("formula-exposure-ablation", "symbolic factor", "Validate the constrained-DSL formula and ablate each term while neutralizing sector, beta, size, and parent factors.", "The formula contributes beyond constituent terms and survives exposure neutralization.", "DSL hash, ablation table, and factor-exposure regression."),)
        if kind == AgentKind.TEXT_EVENT:
            return (ValidationCheck("event-time-and-confounds", "text/event", "Use source publication or EDGAR acceptance time, deduplicate amendments, and isolate concurrent earnings, guidance, and market-wide news.", "The event label is reproducible at the timestamp and incremental to known concurrent disclosures.", "Event ledger, label-review sample, amendment map, and matched-control study."),)
        if kind in {AgentKind.ALTERNATIVE_DATA, AgentKind.ALTERNATIVE_DATASET_CREATOR}:
            return (
                ValidationCheck("proxy-chain-and-vintages", "alternative data", "Verify license/provenance and a predeclared economic mechanism using first-published vintages. Fundamental proxies test observation → operations → earnings surprise → residual return; liquidity, flow or risk-premium hypotheses require their own falsifiable mechanism.", "Mechanism and incremental residual-return evidence survive revisions, timing, cost and issuer/exposure-map controls; predicting an operating metric alone is not alpha.", "License record, vintage archive, mapping table, mechanism-specific evidence, incremental-return test and cost/latency log."),
                ValidationCheck("asset-applicability-map", "alternative data", "Freeze a steward-approved, dated source-key → asset exposure map and use only exposures, sector/issuer identities, and universe membership known at the decision timestamp.", "The result survives direct-issuer, industry-only, and geography/facility/route exposure ablations; mapping changes, concentration, and unavailable entities do not drive it.", "Immutable map hash, evidence URIs, exposure weights and availability times, historical membership table, and mapping-ablation report."),
            )
        return ()
