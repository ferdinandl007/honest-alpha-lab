# Research-only 15-sleeve portfolio backtest

`research_strategy_catalog()` turns the 15 mined candidates into explicitly named strategy sleeves: five symbolic, five EDGAR event, and five alternative-data sleeves. They remain **research candidates**; creating a sleeve does not validate it or authorize investment.

`TraditionalAllocator` supports equal-weight, inverse-volatility, and diagonal-shrinkage long-only minimum-variance sleeve allocation. Allocation returns must come from a training window that ends before the test window. Weights are capped and frozen during the next test window; do not fit weights on the period being reported.

`PortfolioBacktester` uses only a verified `HistoricalDataset` with point-in-time signals and OHLCV bars. Its research execution model has next-bar limit orders, participation caps, expiry of unfilled limits, borrow checks for new shorts, fixed stop loss and take-profit levels, and periodic sleeve rebalancing. If a bar reaches both a stop and target, it records the stop first. It has no broker, order router, or live-data adapter.

## Required data

Do not use a survivorship-biased current-ticker download for validation. Supply a fixed snapshot containing historical membership, delisting returns, corporate-action-adjusted OHLCV, sector/asset mappings, borrow availability for short sleeves, and first-available signal timestamps. Event and alternative-data inputs must retain source release vintages.

The runnable adapter expects three CSV files:

- `bars.csv`: `day,asset,open,high,low,close,dollar_volume,borrow_available`
- `signals.csv`: `strategy_id,asset,score,available_at,snapshot_hash`
- `training_returns.csv`: `strategy_id,return` with aligned, pre-test sleeve returns.

Run only after a dataset snapshot has passed PIT checks:

```bash
PYTHONPATH=src python3.12 scripts/run_portfolio_backtest.py \
  --bars bars.csv --signals signals.csv --training-returns training_returns.csv \
  --snapshot-hash <immutable-snapshot-hash> --output portfolio-backtest.json
```

The output is an audit artifact (NAV path, simulated fills, rejected orders, and frozen sleeve allocation), not a live order ticket or an approved portfolio.
