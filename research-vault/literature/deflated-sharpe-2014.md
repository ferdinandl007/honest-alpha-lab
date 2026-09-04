---
paper_id: deflated-sharpe-2014
year: 2014
reviewed_on: 2026-09-04
lane: statistics
read_depth: abstract_and_statistical_method
replication_status: not_run
code_status: not_applicable
---

# The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality

Source: [primary source](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).

## Evidence reviewed

Adjusts Sharpe-ratio inference for selection across trials and non-normal returns, using track-record length, return moments and the distribution of tested Sharpe ratios.

## Application to Honest Alpha Lab — our recommendation

Log all tests and preserve dependent-trial information. Report a selection-aware Sharpe diagnostic alongside net performance and confidence intervals.

## Limits and remaining checks

DSR is not an arbitrary penalty to subtract from IC, nor a guarantee of alpha. Correlated trials and serially dependent returns require justified treatment. Our overlapping-horizon IC analysis needs its own dependence-aware uncertainty calculation.

## Connections

- [[literature/backtest-overfitting]]
- [[literature/alphaeval-2025]]
- [[literature/index]]

