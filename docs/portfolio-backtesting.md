# Research-only 15-sleeve portfolio backtest

`research_strategy_catalog()` turns the 15 mined candidates into explicitly named strategy sleeves: five symbolic, five EDGAR event, and five alternative-data sleeves. They remain **research candidates**; creating a sleeve does not validate it or authorize investment.

`TraditionalAllocator` supports equal-weight, inverse-volatility, and diagonal-shrinkage long-only minimum-variance sleeve allocation. Allocation returns must come from a training window that ends before the test window. Weights are capped and frozen during the next test window; do not fit weights on the period being reported.

`PortfolioBacktester` defaults to requiring a verified `HistoricalDataset`. The development workflow explicitly permits unverified research inputs and labels results accordingly; timestamp checks do not independently approve data. Its research execution model has next-bar limit orders, participation caps, expiry of unfilled limits, borrow checks for new shorts, fixed stop loss and take-profit levels, and periodic sleeve rebalancing. If a bar reaches both a stop and target, it records the stop first. It has no broker, order router, or live-data adapter.

## Required data

Do not use a survivorship-biased current-ticker download for validation. Supply a fixed snapshot containing historical membership, delisting returns, corporate-action-adjusted OHLCV, sector/asset mappings, borrow availability for short sleeves, and first-available signal timestamps. Event and alternative-data inputs must retain source release vintages.

The runnable adapter expects three CSV files:

- `bars.csv`: `day,asset,open,high,low,close,dollar_volume,borrow_available`
- `signals.csv`: `strategy_id,asset,score,available_at,snapshot_hash`
- `training_returns.csv`: `day,strategy_id,return,available_at` with identically aligned pre-test dates and return availability before the test starts.

Use the [integrated workflow](state-and-portfolio-workflow.md) and [request template](../config/portfolio.example.json). Independent PIT checks are still required before treating results as investment evidence:

```bash
python -m honest_alpha_lab backtest-portfolio \
  --request config/my-portfolio-request.json \
  --output-directory var/portfolio-artifacts
```

The output is an audit artifact (NAV path, simulated fills, rejected orders, and frozen sleeve allocation), not a live order ticket or an approved portfolio.

The legacy script now delegates to this dated workflow and accepts `--request`
and `--output-directory`. Undated training-return arguments are no longer accepted.
Numerical prediction artifacts can supply custom sleeves, beyond the original 15
candidate definitions. Supervisor portfolio campaigns support the same workflow.
