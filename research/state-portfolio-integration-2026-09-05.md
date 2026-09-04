# State and portfolio integration checkpoint — 5 September 2026

The full local suite passed **536 tests**, with zero failures, errors or skips.
The 362 warnings are repeated upstream hmmlearn/NumPy shape-assignment
deprecations. They are not suppressed. Local machine-readable evidence lives at
`var/verification/state-portfolio-integration-2026-09-05.xml` and is not published.

What is now connected:

- Gaussian HMM and Gaussian-mixture diagnostics in numerical reports, including
  fold-local probabilities, probability-weighted conditional IC, missing-input
  statuses and convergence details. Development feedback carries those diagnostics.
- Numerical predictions archived as Parquet and dated CSV signal handoffs using
  the immutable calendar's decision clocks.
- A portfolio command comparing equal-weight, inverse-volatility and shrinkage
  minimum-variance allocation on identically aligned pre-test sleeve returns.
- Explicit costs, daily execution, rebalancing, NAV, drawdown, turnover, fills and
  input/report artifacts. Allocation weights stay frozen during each internal test.
- Recurring portfolio campaigns with frozen input hashes, bounded child execution
  and completed-report recovery.

Regression checks cover future-data changes not rewriting earlier probabilities,
training-only fits, future label missingness not removing current predictions,
unaligned/late training returns, changed inputs and the actual command/supervisor
boundaries. Inverse-volatility normalization was corrected. CSV parsing no longer
marks its own inputs independently verified.

The numerical-command test feeds its exported signals directly into the portfolio
workflow. These are explicitly labeled **software/accounting fixtures**, not
synthetic financial benchmarks. No new historical-market performance or profitable
alpha is claimed. Suitable equity execution data, independent PIT verification
and the separate final-validation process remain necessary.

See [the workflow guide](../docs/state-and-portfolio-workflow.md) for exact inputs,
commands, artifact interpretation and limitations.
