# Alpha mining expansion review — 4 September 2026

The highest-value improvement is a complete, measured research loop over real historical data. More proposal agents alone will increase the idea queue without establishing whether the ideas predict returns.

Scope: read-only review of the current implementation, existing artifacts, diagnostic execution, and official provider documentation. No provider subscriptions, feed connections, or implementation changes were made in this review. Proposed signal families below are hypotheses, not measured findings.

## Current evidence and priority findings

- The research vault contains 15 candidate JSON records and 15 validation plans, with no evaluation JSON records. No CSV, Parquet, or DuckDB research data files were found outside the synced reference sources in the workspace search.
- All 25 existing tests pass. These establish covered software behavior, not historical alpha performance.
- `research_loop.py:42` creates proposal tasks, executes them sequentially, and records candidates. It does not dispatch feature computation and evaluation before generating the next batch. Shared memory asks for diversity but does not enforce economic or statistical distinctiveness.
- `dsl.py:152`: evaluating `rank(x)` on `[7, 7, 7]` returned `[0.0, 0.5, 1.0]`. Ties manufacture differences based on row order. The evaluator also exposes a flat sequence rather than explicit date-by-security axes; cross-sectional and time-series operations need separate semantics before panel-scale mining.
- `reward.py:97`: missing incremental IC falls back to raw out-of-sample IC. A diagnostic with no incremental evidence credited 0.02 incremental IC. Missing evidence must remain missing. Also, the semantic-chain gate applies to `ALTERNATIVE_DATA` but not `ALTERNATIVE_DATASET_CREATOR`; the diagnostic passed that gate despite false semantic evidence. These checks must cover both paths.
- `evaluation.py:103`: the sealed gate's consumed state exists only on the instance. Durable, atomic test consumption and separately permissioned test data are needed across restarts and concurrent workers. Agent memory must exclude sealed outcomes from subsequent optimization.
- `asset_mapping.py:171`: map approval checks caller-supplied actor and dataset strings, rather than resolving an authenticated service identity and an approved dataset record. This is a reference interface, not deployed separation of authority. Historical identity, exposure version resolution, and multiple-source aggregation also need explicit rules.
- `portfolio.py:205`: held assets absent from today's bars are omitted from NAV. `portfolio.py:218` can include multiple dated scores for the same security in a selection. Fix missing-price/delisting handling, select one eligible score per security, and verify exit/rebalance/cost behavior before interpreting portfolio backtests.

## Feed shortlist

### TradingView: visualization only under the standard arrangement

TradingView's [official support](https://www.tradingview.com/support/solutions/43000474413-i-need-access-to-your-api-in-order-to-get-data-or-indicator-values/) says it does not provide a user API for market data or indicator values; its REST API serves broker integrations. Its [standard terms, section 3](https://www.tradingview.com/policies/) restrict automated non-display processing, including use of alerts and webhooks. An unofficial downloader is therefore not the recommended research integration.

If charting is desirable, its [charting-library datafeed interface](https://www.tradingview.com/charting-library-docs/latest/connecting_data/datafeed-api/) accepts our own external feed. The chart library itself includes no market data. Chart licensing remains separate from data licensing.

### Daily equity foundation

- [Norgate](https://norgatedata.com/subscribe/): relevant subscription tiers include delisted securities and historical index constituents, which fit the original universe requirement. Its [updater requires Windows](https://norgatedata.com/ndu-installation.php), so deployment on this Mac needs a supported Windows ingestion host or another provider. Current fundamentals are not a substitute for historical filing vintages.
- [Massive](https://massive.com/docs/flat-files/stocks/overview): programmatic historical stock flat files include daily/minute bars, trades, and quotes. A practical API-first ingestion candidate. Verify historical coverage, adjustments, delisting outcomes, identity changes, and universe membership separately; price coverage alone does not settle these requirements.
- [Databento](https://databento.com/security-master): market data plus security-master/corporate-action products offer an alternative for richer market structure research. Evaluate exact datasets and reference-data history. Tick or order-book work should be a later scope expansion; the first experiment remains daily equity features.

### Fundamentals and events

- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces): submissions and XBRL are available programmatically without API keys. Preserve accession identifiers, original documents, and acceptance timestamps. Do not treat a latest-filing frame or present-day issuer list as a historical snapshot.
- [Sharadar SF1 documentation](https://data.nasdaq.com/databases/SF1/documentation?anchor=exception-handling): normalized fundamentals with as-reported dimensions are a paid alternative. Verify filing/date-key semantics and actual vintage behavior. These actuals do not supply historical analyst estimates, which several current proposals require.

### Distinct economic observations

- [ALFRED/FRED](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html): retrieve data known during a specified historical period, rather than today's revised history. Use release timestamps or conservative eligibility when only dates are available. A macro series common to every stock needs interaction with dated issuer exposures or lagged sensitivities to become a cross-sectional signal.
- [EIA](https://www.eia.gov/opendata/documentation.php): electricity and petroleum inputs for operating and cost hypotheses. Archive first-available releases; access to historical series is not proof that revisions are reconstructible.
- [USAspending](https://api.usaspending.gov/docs/endpoints): award/recipient data for public-contract exposure. Resolve subsidiary-to-issuer mappings, reporting delays, amendments and first availability. Award ceilings and obligations must not be equated with recognized revenue.

Provider capabilities above were checked in official documentation; access under this account, contractual research rights, and end-to-end feed operation were not tested.

## Proposed initial mining lanes

| Lane | Example hypothesis | First falsification check |
| --- | --- | --- |
| Accounting changes | Receivables or inventory growth diverges from sales; cash conversion weakens | Sector/accounting differences and original-filing availability |
| Disclosure changes | New risk language or segment deterioration relative to the issuer's prior filing | Boilerplate changes and concurrent earnings explain the result |
| Price response to information | Accounting deterioration receives little immediate price response | Incrementality over each component and market/sector effects |
| Macro exposure | Release innovations interact with prior rate/commodity sensitivity | Vintage leakage, unstable betas, or broad sector effects |
| Energy operations | Weather-adjusted demand or operating shocks affect exposed issuers | Seasonality, plant ownership dates, hedging and regulated pass-through |
| Government contracting | Changes in obligated awards precede operating improvements | Reporting delays, award revisions and revenue timing |

Every lane must use the same target, historical eligibility rules, 5/10/20/60-trading-day horizons, costs, and trial ledger. Selecting a sector, lag, event definition, or exposure map after observing performance is another research trial.

## Concrete implementation sequence

1. Correct the rank, reward, panel-axis, timestamp, and portfolio defects; enforce durable evaluator authority and final-test isolation. Keep independent numerical evaluation separate from proposing agents.
2. Ingest one equity provider plus SEC and one complementary public source. Store raw responses with content hashes, observation periods, publication times, retrieval times, versions and stable security identities. Validate against known corporate actions and original filings.
3. Make each candidate machine-executable: approved formula/event rules, required fields, universe, availability, exposure-map version and parameters. A candidate with unavailable inputs should enter a data-needed queue.
4. Complete the job chain: proposal → deduplication → feature build → walk-forward evaluation → library residualization → development reward → next proposal. Use conventional models; retain failed and null trials. Sealed evaluation is a separate final operation.
5. Add bounded parallel workers and a durable queue with job leases, retries, idempotent outputs, pre-call cost limits and shared trial reservations. Give lanes distinct economic mechanisms and retrieve related successful and failed experiments from the vault.
6. Compare a fixed-factor baseline, random valid DSL search, agent search without shared memory, and agent search with shared memory, on identical real datasets and equal budgets. Keep the final test out of this adaptive comparison.

Benchmark success by evaluated independent hypotheses per unit cost, valid-data completion rate, duplicate rejection, incremental development IC and confidence intervals, net turnover/capacity evidence, and stability across folds and probabilistic states. Candidate count and text novelty alone are insufficient.

Recommended first milestone: one reproducible historical snapshot and a bounded 30-hypothesis batch across three distinct lanes, each with a complete evaluation or an explicit failure reason. This establishes whether additional mining improves the library before scaling agent volume or buying more feeds.
