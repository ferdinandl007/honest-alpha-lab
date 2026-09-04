# Executable numerical research workflow

This workflow computes development evidence. It does not approve a dataset,
promote a candidate, claim portfolio profitability, or access a sealed test.

## Environment

Use Python 3.12 and install the project with its analytics, models, storage and dev
extras. The workspace `.venv` contains these dependencies. The dated requirements
file records tested versions; it is not a cross-platform lock. Use:

```sh
.venv/bin/python -m pip install -e '.[analytics,models,storage,dev]'
.venv/bin/python -m pytest -q
```

Until editable installation, prefix CLI commands with `PYTHONPATH=src`.

## Snapshot input contract

Prepare three Parquet exports from a lawful historical source:

| File | Required columns |
| --- | --- |
| Observations | `session`, `asset`, `field`, finite `value`, timezone-aware `available_at`, `source_row_id` |
| Historical universe | `asset`, `valid_from`, nullable inclusive `valid_to`, timezone-aware `known_at` |
| Exchange calendar | unique ordered `session`, timezone-aware `decision_at` |

Identifiers must be stable security identifiers, not today's ticker lookup. Store
all relevant vintages. The reader picks only the latest value available by that
session's decision and resolves known membership changes. It does not carry values
across sessions. Periodic financial fields must therefore be materialized by a
separate point-in-time feature builder, preserving their original evidence.

The declaration JSON requires `provider`, `source_uri`, `license_id`,
`rights_evidence_uri`, `availability_evidence_uri`, `universe_evidence_uri`,
`adjustment_convention`, `delisting_coverage`, `security_identifier`, timezone-aware
`retrieved_at`, and `purpose` (`research` or `correctness_fixture`).

```sh
PYTHONPATH=src .venv/bin/python -m honest_alpha_lab import-snapshot \
  --observations /absolute/path/observations.parquet \
  --universe /absolute/path/universe.parquet \
  --sessions /absolute/path/sessions.parquet \
  --declaration /absolute/path/declaration.json
```

The command preserves exact bytes under a content-addressed directory and prints
its location. Reading verifies the manifest and every file hash. Declared rights
and publication times still require independent review; strings are not proof of
licensing or point-in-time correctness. The CLI never automatically marks them
approved.

## Features and labels

`ResearchPanel` uses explicit date-by-security arrays. `rank` and `zscore` operate
cross-sectionally; lag, delta and means operate through each security's history.
Ties receive average ranks. Warm-up, absent inputs, ineligible names and division
by zero remain missing. Rolling means need complete windows. Parser size, nesting,
function arity and literal windows are bounded. Mining must use `evaluate_panel`;
the legacy one-dimensional API is only a diagnostic compatibility interface.

The executable label convention is decision-close information followed by entry
at the next open and exit `horizon` sessions later at the open. Supply consistent
`total_return_open`, `sector_total_return_open`, `market_total_return_open`, and
their corresponding `close` fields. Total-return adjustment and delisting proceeds
must be provider-verified. Missing terminal outcomes stay missing, not zero; an
explicit zero terminal index records a total loss. No prices are invented.

Beta is trailing OLS exposure of stock-minus-sector daily returns to the market.
Subtracting this *incremental* market contribution avoids subtracting the sector's
market exposure twice. Beta uses only observations through the decision session.
The labels are prediction targets, not a complete fee/borrow/order simulation.

## Evaluate a candidate against a fixed library

```sh
PYTHONPATH=src .venv/bin/python -m honest_alpha_lab evaluate-formula \
  --snapshot /absolute/path/content-addressed-snapshot \
  --formula 'rank(ts_delta(close, 20))' --field close \
  --baseline 'rank(ts_delta(close, 5))' \
  --development-end 2025-12-31 --horizon 20 --model ridge
```

Dates and fields here illustrate command syntax; they do not imply this dataset
exists locally. Select the development cutoff before inspecting outcomes. Store
the final test elsewhere under independent service permissions.

Ridge, elastic-net and LightGBM use identical complete-case samples for baseline
and augmented models. Scaling and fitting use training data only; LightGBM ranking
groups are trading dates. Outcome end indices, not only row dates, control purging.
Each fold reports raw candidate IC and paired baseline/augmented IC. Incremental IC
means their difference, not raw IC relabeled as incremental evidence.

Outputs contain content hashes, model versions, fold boundaries, coverage,
daily IC, sign-invariant library similarity, and moving-block development
uncertainty. Gapped/insufficient IC histories fail explicitly rather than creating
misleading uncertainty estimates. Final-test outcomes are refused by the scheduling
reward harness and excluded from research-memory reads.

Results and predictions are content-addressed artifacts. `financial_alpha_verified`
remains false: portfolio/cost evidence, independent provenance approval and final
validation are separate obligations. Correctness fixtures require the explicit
`--allow-correctness-fixture` flag and remain labeled as such in every result.

## Market-state comparison

`FittedRegimeModel` fits either Gaussian HMM emissions/transitions or a non-Markov
Gaussian mixture with train-only scaling. Inference dates must follow training.
HMM output is forward-filtered probabilities, never future-smoothed labels.
`conditional_ic` reports probability-weighted daily IC and effective days; these
effective days do not correct serial dependence. State analysis is currently a
tested service API, not yet integrated into every CLI report.

## Still required

The numerical CLI and queue are building blocks. Automatic proposal-to-evaluation
feedback, real-data provider ingestion, dataset-steward approvals, three-agent
feature compilation, benchmark scheduling, graph retrieval, cost/portfolio
integration and separately permissioned final evaluation remain tracked in the
implementation audit. Passing software fixtures does not satisfy those items.
