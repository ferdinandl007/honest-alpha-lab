---
paper_id: backtest-overfitting
year: 2015
reviewed_on: 2026-09-04
lane: statistics
read_depth: author_hosted_abstract_search_extract_full_fetch_failed
replication_status: not_run
code_status: not_applicable
---

# The Probability of Backtest Overfitting

Source: [primary source](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).

## Evidence reviewed

Introduces a framework and combinatorially symmetric cross-validation for estimating the risk that in-sample strategy selection produces disappointing out-of-sample rankings.

## Application to Honest Alpha Lab — our recommendation

Retain the complete candidate return matrix, including losers, so a selection-overfitting diagnostic is possible.

## Limits and remaining checks

Use PBO as a diagnostic, not permission to replace chronological testing with shuffled folds. Dependence and an adaptively accumulated library need careful treatment. This review accessed the author-hosted abstract through search; the direct PDF fetch failed.

## Connections

- [[literature/deflated-sharpe-2014]]
- [[literature/alphaeval-2025]]
- [[literature/index]]

