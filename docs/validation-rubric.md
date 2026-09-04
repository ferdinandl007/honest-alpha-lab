# Candidate validation rubric

Every proposal receives an immutable `ValidationPlan` before its first numerical trial. A plan is not a backtest result and cannot validate or accept an alpha.

The common gates are: point-in-time lineage and historical membership; purged expanding walk-forward across all configured horizons; temporal persistence and decay; probability-weighted HMM market-state analysis compared with a non-Markov clustering baseline; sector/size/liquidity/volatility/calendar robustness; incremental contribution and similarity to the shared library; global trial-count correction; conservative turnover/cost/capacity analysis; and one single-use sealed test after numerical validation.

Symbolic formulas additionally undergo constrained-DSL, term-ablation, and exposure-neutralization checks. Text/event signals require timestamp, amendment, labeling, and concurrent-news controls. Alternative data requires licensing, first-published vintage preservation, basket mapping, cost/latency accounting, and evidence for observation → economic metric → earnings surprise → residual return.

Regime dependence is allowed only when measured and declared. The system stores market states as probabilities—not retrospective bull/bear labels—and reports effective sample size and performance for every state. A candidate that works only conditionally cannot be presented as an all-weather alpha.
