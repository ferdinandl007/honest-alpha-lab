# From signal evaluation to market states and portfolio research

This workflow connects the existing numerical evaluator to state diagnostics and
conventional portfolio backtesting. It is development research, not live trading,
independent data approval, or a sealed final test.

## Market states are part of numerical reports

Both `evaluate-formula` and queued `work-one` jobs now attach `regime_reports` to
their development report. By default each usable walk-forward fold compares a
Gaussian HMM with a non-Markov Gaussian mixture. The models use daily market
returns, trailing mean and trailing volatility from available total-return fields.

Scaling, emissions and transitions are fitted on training data only. The HMM
filters forward through the embargo interval and evaluation dates; it does not
use hindsight smoothing or Viterbi labels. Each fold reports state probabilities,
probability-weighted raw, baseline and augmented IC, effective sample size and
convergence diagnostics. State numbers are local to a model and fold; they are
not universal bull/bear labels and are not pooled across refits.

Missing data, short training histories and failed fits produce explicit diagnostic
statuses. They do not become fabricated state estimates or automatic alpha
rejections. Effective days measure weight concentration, not independent samples;
conditional means are descriptive, not significance certificates.

Frozen numerical plans include `config.regime_configs` and
`config.regime_feature_window`. An empty regime configuration explicitly disables
state fitting. Existing frozen runs must be recreated after an engine change;
the worker refuses to silently change the numerical implementation.

Implementation references: [hmmlearn API](https://hmmlearn.readthedocs.io/en/stable/api.html)
and [Gaussian mixture documentation](https://scikit-learn.org/stable/modules/mixture.html).

## Predictions become dated signal inputs

Numerical runs now archive both a prediction Parquet file and a signal CSV.
The command prints `signals_artifact` and `strategy_id`; queued results contain
`signals_artifact_hash`. The strategy identifier is the candidate formula hash.

The CSV contains `strategy_id,asset,score,available_at,snapshot_hash,session`.
Availability comes from the immutable snapshot's actual `decision_at` calendar.
Finite out-of-sample predictions are exported even when their future labels have
not matured. Future label availability must not determine whether a current
prediction exists.

These are **retrospective development predictions**. They are not proof that a
model or LLM was available at the historical decision time. Formula selection and
all prior searches still count toward the experiment's trial history.

## Run the portfolio comparison

```sh
python -m honest_alpha_lab backtest-portfolio \
  --request config/my-portfolio-request.json \
  --output-directory var/portfolio-artifacts
```

Use [the example request](../config/portfolio.example.json) as a template. Supply:

- Actual execution bars: `day,asset,open,high,low,close,dollar_volume,borrow_available`.
- Dated signal CSVs, including the numerical handoff above. For several sleeves,
  concatenate compatible CSV rows while keeping each strategy and snapshot ID.
- Realized **pre-test** sleeve returns: `day,strategy_id,return,available_at`.
  `day` is the return interval's end, not its start. Each sleeve must have exactly
  the same training dates, and every return must have been available before the
  internal test starts. These returns can be derived from earlier sleeve runs;
  never use the evaluation interval to fit allocation weights.
- Explicit development and internal-test boundaries, strategy definitions,
  allocation constraints and execution costs. Zero costs must be requested
  explicitly; defaults do not imply realistic costs.

Use `strategies` to define a numerical candidate's sleeve under its formula-hash
identifier. Each definition includes `name`, `family`, `signal_description`,
`required_inputs`, `horizon_days`, `side` and `rebalance_days`.

The workflow compares equal weight, inverse volatility and shrinkage minimum
variance on the **same** inputs. Weights are frozen before the internal test;
holdings rebalance according to each sleeve's schedule. All three results are
reported; the workflow does not pick a winner using test performance.

Outputs include allocations, daily NAV and returns, drawdown, gross turnover,
fills, rejected orders, commission and borrow costs, complete policies and hashes
of the exact consumed inputs. Slippage is embedded in fill prices rather than
separately attributed. Initial holdings are cash; final positions are marked, not
automatically liquidated. Reports and input bytes are archived together.

Do not substitute total-return indices for tradeable OHLC prices. Corporate
actions, changing security identities, historical universe membership, delistings
and borrow availability require suitable upstream data. Daily bars provide only
an execution approximation; intraday ordering and actual queue priority are unknown.

## Run it under the supervisor

A campaign can use `kind: "portfolio"`, with `payload.request` holding the same
request object and `payload.artifact_root` naming an artifact directory. For these
recurring jobs, `request.input_hashes` must include SHA256 hashes for `bars`,
`signals` and `training_returns`. Changed input bytes fail the job rather than
silently changing a frozen experiment. Register a new campaign when changing the
inputs or policy.

The existing supervisor supplies deadlines, retries, durable job identity and
pause/resume. Completed portfolio reports are reused on recovery. Scheduling this
job does not refresh a dataset or approve a strategy automatically.

## What completion means here

The software path is numerical evaluation → state report → dated prediction
handoff → portfolio comparison → immutable report, with optional recurring jobs.
Software tests use explicitly labeled fixtures to check this path. Genuine equity
data and independent validation are still required to make financial claims.
