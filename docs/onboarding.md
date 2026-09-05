# Honest Alpha Lab — onboarding

**Current-state guide · 5 September 2026**

### Money-safety update

The [money-safety review and migration guide](money-safety-review.md) records
the corrected accounting, timing, authorization and spending-control paths.
Reimport legacy numerical snapshots with explicit exchange opens and rerun
affected research; do not reuse old backtest results as corrected evidence.
CLI agents now refuse to launch until the operator explicitly acknowledges that
provider dollar/token caps are not enforced by this runner. Live trading remains
off by default and is not certified ready for real money.

## 1. What this system is

Honest Alpha Lab is a research workbench for finding stock-market signals and
checking whether they add useful information beyond signals we already have.
Agents generate ideas; conventional numerical models test them; a shared record
stores what was attempted and what happened.

It is **not yet a finished, continuously operating alpha-mining service**. Working
components include agent task files, a persistent numerical queue, historical
evaluation, event/proxy feature construction and shared research memory. Real-data
benchmarking, unrestricted research-workspace integration and several operational
and validation connections are still unfinished. A green test suite is evidence
about software behavior—not evidence of profitable trading strategies.

Live execution remains off by default, and there is no LLM fine-tuning. The owner's
execution extension now adds a strategy builder, historical session books,
persistent daily paper ledgers and a private approval console with an optional,
separately armed Webull HK production route. See [books and control room](books-and-control-room.md)
for current interfaces and limitations. No live deployment or real order test has
been performed; account-wide risk and reconciliation remain required.

### The intended division of responsibility

Researchers should have freedom to choose sources, explore papers, collect data,
write experimental code and follow unexpected leads. Collection should not require
a predefined catalog or an approval ceremony for every file. Access restrictions,
unauthorized private data and agreed resource/spending limits still matter.

The stricter contracts belong at the handoff into reproducible testing and the
trusted alpha library. An interesting downloaded dataset is a research input—not
automatically an approved dataset or accepted alpha.

**Current capability:** the supervisor uses an explicit, network-enabled research
mode with a writable scratch directory. Agents can collect data, write code and
use arbitrary file formats without the optional collector's format restrictions.
The legacy proposal-only mode remains read-only. These modes do not by themselves
provide full OS/read isolation or independently enforced account spending limits.

## 2. The researchers and services

| Component | Job | Current position |
| --- | --- | --- |
| Symbolic Factor Agent | Propose combinations of numerical fields: momentum, value, revisions and other economic relationships. | CLI proposal path and executable formula evaluation exist. |
| Text and Event Agent | Find information in filings, releases, transcripts and news; turn it into timestamped events. | Proposal contracts and event-to-stock feature builder exist. A general autonomous extraction/collection workflow is not complete. |
| Alternative-data researcher and dataset creator | Invent proxies, discover sources, collect measurements and identify affected companies. | Separate discovery/creation roles, reference catalog tools, mapping contracts and executable proxy feature builder exist. Open-ended collection integration is in progress. |
| Numerical evaluator | Test proposals against a fixed historical dataset and baseline library. | Implemented with bounded queued execution and development reports. |
| Market-state service | Estimate probabilities of different market conditions and measure conditional performance. | Fitted HMM and non-Markov Gaussian-mixture components exist; not included in every evaluation automatically. |
| Portfolio service | Allocate among strategy sleeves and simulate positions, orders and rebalancing. | Separate research backtester exists; not a complete automatic downstream stage of every mined alpha. |
| Independent validation | Decide whether evidence is sufficient for admission and final testing. | Several checks and durable sealed-test reservations exist; full service/deployment integration remains incomplete. |

The alternative-dataset creator is a distinct role within the alternative-data
path, not a missing fourth asset class. It should create useful datasets rather
than merely describe hypothetical signals.

## 3. How one research round works

```text
Research brief + available data + prior attempts
                       ↓
          Agents explore and propose ideas
                       ↓
       Executable feature/formula available?
             ├─ No → record data-needed work
             └─ Yes
                  ↓
        Deduplicate and reserve a numerical trial
                  ↓
        Test on the locked development snapshot
                  ↓
        Store report, predictions and provenance
                  ↓
        Feed measured results into the next round
```

1. A coordinator supplies the research scope, a fixed data-snapshot identity,
   budgets, available fields and previous results.
2. Agents propose candidates. The shared context includes prior attempts and
   measured development feedback, helping them avoid repeating work.
3. A candidate that cannot yet be expressed using available features is recorded
   as `data_needed`. The system does not invent a backtest for it.
4. Executable formulas are canonicalized. Equivalent formatting and certain
   commutative variants share one numerical trial within the locked run.
5. A worker claims a job, verifies the data and run configuration, performs the
   evaluation and writes content-addressed artifacts. Leases and timeouts prevent
   stale workers from publishing as if they still owned a job.
6. The next adaptive round receives those measured results. Pending numerical
   trials must finish or be recovered before the next round starts.

`QueuedResearchLoop` implements the bounded numerical flow. The newer `supervise`
command runs recurring campaigns for all researcher roles, shares recent findings,
and can connect to an existing numerical run. It does not yet provision real data
or automatically renew successive numerical runs as their budgets expire.

## 4. Exactly what are the inputs?

There are two levels: **exploration inputs** and **evaluation inputs**. A researcher
can begin with an idea and sources; a credible historical test needs substantially
more than a prompt or a list of today's stock tickers.

### A. Research brief and agent configuration

The brief describes the economic question, target companies/sectors or universe,
desired novelty, time horizon and permitted spending. For example:

> Investigate whether changes in publicly available freight activity contain
> information about US transport companies beyond ordinary price momentum.
> Explore different sources and explain failures as well as promising results.

Each current agent job carries:

| Input | Meaning |
| --- | --- |
| `job_id`, `agent_kind`, `requested_by` | Identity, researcher role and initiator. |
| `input_snapshot_hash` | The fixed data identity for the experiment. A hash alone does not supply the data. |
| `prompt_hash` and prompt/context | Versioned instructions, available fields, source context and shared memory. |
| `max_trials` | Declared trial allowance. |
| `max_runtime_seconds` | Declared elapsed-time allowance. |
| `max_data_cost_usd` | Declared data-spending allowance. |
| `max_agent_tokens` | Declared model-token allowance. |

The job defaults are 100 trials, 3,600 seconds, $0 data spending and 100,000 agent
tokens. **These are not all globally enforced today.** Numerical reservations,
attempt limits and runtime checks have stronger enforcement than overall agent
spend/token accounting. Do not treat a declared token limit as a guaranteed bill cap.

Discovery-only CLI tasks can return findings without approved lineage identifiers.
Emitted alpha candidates still require lineage drawn from the task context.

### B. Historical numerical dataset: three Parquet files

| File | Exact required columns | What it represents |
| --- | --- | --- |
| Observations | `session`, `asset`, `field`, `value`, `available_at`, `source_row_id` | A numerical field for a security/session, its availability time and source identity. |
| Historical universe | `asset`, `valid_from`, `valid_to`, `known_at` | Which securities belonged to the research universe, with dated knowledge of membership. `valid_to` is nullable and inclusive. |
| Exchange calendar | `session`, `decision_at` | The actual trading sessions and decision timestamps. |

Dates identify sessions; timestamps must include timezones. Values must be finite.
Missing observations are absent rows, not fabricated zeros. Use stable security
identifiers and historically correct membership, including delisted securities
where coverage permits—not today's constituents projected backward.

For the current residual-return evaluator, supply these six fields:

```text
total_return_open          total_return_close
sector_total_return_open   sector_total_return_close
market_total_return_open   market_total_return_close
```

Also supply every numerical feature referenced by the proposed formula and its
baselines. Sector/market series must be aligned to the security/session panel.
Adjustment conventions, historical sector assignments and delisting treatment
need provider evidence; the system does not create them automatically.

The snapshot reader selects the latest vintage available at each decision. It
does **not** automatically carry a quarterly observation forward across daily
sessions. Periodic, event and proxy inputs need explicit feature materialization.

### C. Dataset declaration

The JSON declaration contains these exact fields:

```text
provider                  source_uri
license_id                rights_evidence_uri
availability_evidence_uri universe_evidence_uri
adjustment_convention     delisting_coverage
security_identifier       retrieved_at
purpose
```

`purpose` is `research` or `correctness_fixture`. Retrieval time includes a timezone.
Importing preserves the file bytes and creates a content-hashed snapshot. A
declaration records provenance claims; it does not independently prove them or
grant a license.

### D. Additional inputs for text events and alternative-data features

The implemented feature-building request has exactly four top-level objects:
`plan`, `sources`, `exposures`, `sessions`.

| Object | Exact fields |
| --- | --- |
| `plan` | `dataset_id`, `field`, `mode`, `max_age_days`, `clock`, `lag_sessions`, `half_life_days`, `allow_retrospective_llm` |
| Each source vintage | `dataset_id`, `source_key`, `observation_id`, `observed_on`, `value`, `published_at`, `retrieved_at`, `extracted_at`, `source_hash`, `publication_evidence_hash`, `spans`, `extractor_id`, `extraction_kind` |
| Each exposure vintage | `asset`, `source_key`, `valid_from`, `valid_to`, `available_at`, `weight`, `evidence_hash` |
| Each session | `session`, `decision_at` |

Each evidence span has `start`, `end`, `quote`: character positions and the matching
quotation in the archived UTF-8 source. Source and evidence hashes point to stored
artifacts. Extraction kind is `deterministic`, `llm` or `human`.

The `source_key` can identify a facility, region, issuer or other economic entity.
Exposure records connect that entity to securities with dated weights and evidence.
For example, a port observation does not automatically become a shipping-company
signal; the relationship must be supplied and tested.

Two feature modes are implemented:

- `event_sum`: sum distinct active event scores, optionally decayed, using exposure
  effective on each event's observed date and known at the decision.
- `latest_proxy`: use the latest observed period per source and decision-date
  exposures; missing required source components suppress the aggregate.

The default clock, `as_run`, includes collection and extraction time. Explicit
`publication_replay` is a retrospective experiment. Using today's LLM on old
documents is **not** proof that its predictions were historically available.

These fields define the testing handoff. They are not meant to dictate how an
exploration agent thinks, searches or writes its initial collector.

### E. Locked evaluation plan and baseline library

The numerical plan specifies:

| Field | Meaning/default |
| --- | --- |
| `snapshot_hash` | Actual imported snapshot identity. |
| `development_end` | Last permitted development date, selected before inspecting results. |
| `fields` | Allowed numerical feature names. |
| `baseline` | Named formulas representing the fixed comparison library; default empty. |
| `horizon` | One of 5, 10, 20 or 60 trading sessions; default 20. |
| `beta_window` | Trailing estimation window; default 252 sessions. |
| `max_trial_seconds` | Numerical trial timeout; default 300 seconds. |
| `allow_correctness_fixture` | Default false. Fixtures are not financial benchmarks. |
| `config` | Model and walk-forward configuration below. |

The current `config` fields/defaults are `min_train_days=252`, `test_days=63`,
`embargo_days=5`, `min_assets=20`, `min_train_rows=100`, `min_folds=2`, `model="ridge"`,
`regularization=1.0`, `bootstrap_samples=1000`, `seed=0`.

Use `ridge`, `elastic_net` or `lightgbm`. Baselines must use the declared fields.
To test four horizons, explicitly run four declared evaluations; the default run
does not automatically cover all horizons, all regimes and every robustness check.

The run also locks its maximum unique numerical trials, attempts per trial and
wall-clock duration. Changing the numerical source/runtime invalidates further
execution under that frozen run.

### F. Infrastructure inputs

- Python 3.12 and the project's analytics/model dependencies.
- Local artifact/snapshot directories and a shared research-vault location.
- A configured CLI agent and authentication if actual LLM research is requested.
- A provisioned PostgreSQL database for persistent queued evaluation.
- Separate service credentials: `HAL_VALIDATOR_DSN`, `HAL_PROPOSER_DSN`,
  `HAL_WORKER_DSN`; deployment setup also uses a migration role.

Local artifacts and metadata adapters exist. A managed object-storage deployment,
MLflow service or full Qlib installation is not required by the current numerical
command. Qlib compatibility remains a design/interface goal, not a prerequisite
that magically supplies market data.

## 5. What the evaluation actually measures

The prediction target is a forward stock return, minus its sector return and the
estimated **incremental** market-beta contribution. The executable convention is
entry at the next open and exit at the open after the selected horizon.

The evaluator fits on earlier periods and tests on later periods. Training labels
that overlap the test boundary are purged, with an additional embargo. Scaling
and conventional model fitting use training data only.

Reports include:

- Rank IC: whether higher-ranked predictions tend to have higher-ranked outcomes.
- Incremental IC: improvement from adding the candidate to the fixed baseline
  model—not the candidate's standalone IC relabeled as incremental value.
- Fold-by-fold results, coverage, library similarity and development uncertainty.
- Data, formula, policy, runtime and prediction identities for reproducibility.

Regime analysis, broader robustness rules, reward gates and portfolio simulation
exist as additional components. They are **not all automatically completed by
`evaluate-formula`**. An evaluated candidate is not an accepted strategy.

Sealed-test outcomes are excluded from search feedback. One-use reservation and
private storage mechanisms exist, but complete final-test deployment separation
and end-to-end approval remain unfinished. A never-ending search cannot repeatedly
use the same final holdout and still call it sealed.

## 6. What comes out, and where is the shared brain?

| Output/location | Contents |
| --- | --- |
| Agent task directories, normally `var/agent-tasks/` | Prompt, context, output schema, result, event logs and task state. |
| `research-vault/` | Linked Markdown notes and JSON records for candidates, datasets, rounds, evaluations and validation plans. Can be opened as an Obsidian vault. |
| PostgreSQL | Persistent numerical reservations, queue state, development records and separate private sealed records. |
| Configured artifact directories | Content-addressed source evidence, feature tables, evaluation reports and predictions. |
| `research/` and vault literature notes | Literature review and research context. |

The “shared brain” is external memory, not one perpetually trained LLM. Each new
round receives selected prior context. Current vault retrieval is bounded—normally
the last 40 candidate/evaluation records—not an exhaustive semantic graph search.
Saved literature does not automatically become full-text retrieval in every run.

There are two useful duplicate checks: canonical formula identity within a run,
and numerical similarity to the chosen library. They do not prove that every
economically equivalent idea across all historical runs has been detected.

Recognize these result labels:

- `data_needed`: an idea exists, but usable executable inputs are missing.
- `evaluated`: numerical development evidence exists.
- `not_evaluated`: the numerical request lacked usable inputs/evidence.
- Failed execution: an attempt failed or timed out; it is not a negative alpha finding.
- Accepted alpha: a separate admission decision; queue completion does not imply it.

## 7. Can it run forever?

**24/7 operation is a core product requirement.** The implemented `supervise`
service now continuously schedules finite jobs, recovers expired leases, and
stays alive while idle or waiting for the next UTC day's dispatch allowance.
Restart it against the same state and work directories to retain its work history.

This is not yet a certified, deployed unattended system. Automatic host startup,
alerts, full spend controls, successive numerical-run setup, real-data benchmark
campaigns and multi-day operational testing remain incomplete. See the
[always-on service guide](always-on-service.md).

| Capability | Today |
| --- | --- |
| Continuously schedule researcher jobs with prior findings | Yes, through the persistent supervisor and configured CLI workers. |
| Run another proposal/evaluation round with feedback | Yes, within configured numerical runs; their finite deadlines still apply. |
| Enforce numerical trial counts, attempts and elapsed runtime | Yes, within the persistent numerical run. |
| Recover work after interruption | Numerical leases and supervisor checkpoints exist. An interrupted CLI with no final response is retained as interrupted, not silently rerun. |
| Automatically renew an expired run or change its frozen dataset | No. A new declared run is needed. |
| Guarantee a global model-token/dollar cap across every researcher | Not yet. |
| Operate multiple local supervisor workers | Supported through local durable claims; distributed multi-host coordination is not implemented. |
| Discover, ingest and approve all new datasets unattended | Not yet; approval must remain independent. |
| Turn every accepted candidate into an automatically rebalanced live portfolio | No; live execution is outside phase 1. |

The operating model is a persistent **service running bounded jobs**, not one
unlimited experiment. Each campaign needs data/version commitments,
spending limits, stop conditions and recorded results. The service can continue
over time while each individual experiment remains finite and auditable.

## 8. First-use checklist

1. **Choose a research question and starting data.** You do not need fifteen
   strategies to begin. A small genuine dataset with known limitations is more
   useful than pretending a large fixture proves market performance.
2. **Confirm the data provider and historical coverage.** The project does not
   currently supply a verified historical US-equity benchmark dataset. A
   TradingView account or API key alone does not establish point-in-time coverage.
3. **Prepare/import the three Parquet files and declaration.** See the
   [numerical workflow](numerical-workflow.md) for executable commands.
4. **For event/proxy research, build the daily fields.** See the
   [event/proxy handoff](event-proxy-features.md). Keep exploratory collection and
   independent admission distinct.
5. **Run one numerical evaluation before scaling up.** Check field coverage,
   timestamp conventions and model/fold feasibility—not just the headline IC.
6. **Provision the persistent services for repeated rounds.** Follow the
   [PostgreSQL setup](postgres-store.md) and [persistent-loop guide](persistent-research-loop.md).
7. **Connect actual researcher workers and review their evidence.** Use the
   three-role supervisor configuration and explicitly review its daily dispatch
   allowances before starting a service that consumes model usage.
8. **Only then scale trials and compare methods.** Account for every attempted
   variant, the fixed library, costs and data limitations. Reserve final testing
   for an independent, predeclared decision.

Available main commands today are `run-symbolic`, `import-snapshot`,
`build-features`, `collect-document`, `evaluate-formula`, `freeze-run`,
`submit-formula`, `work-one`, `research-feedback`, `supervise`, `supervisor-status`,
`pause-campaign`, and `resume-campaign`. `work-one` returning no job
does not prove that another worker has finished or that the whole campaign passed.

### Optional portfolio experiments

The separate portfolio script takes OHLC bars (`day`, `asset`, `open`, `high`,
`low`, `close`, `dollar_volume`, optional `borrow_available`), timestamped signals
(`strategy_id`, `asset`, `score`, `available_at`, `snapshot_hash`) and pre-test
sleeve training returns (`strategy_id`, `return`). The training-return adapter
does not itself verify chronological separation; the operator must supply it.

Allocation methods include minimum variance, inverse volatility and equal weight.
The simulator models daily limit orders, protective exits, rebalancing,
participation and configurable costs. Daily bars cannot determine every intraday
order sequence; corporate-action/delisting/margin handling and integration remain
incomplete. The 15 existing strategy definitions are proposals, not 15 verified
profitable strategies. See [portfolio backtesting](portfolio-backtesting.md).

## 9. What has actually been verified?

The latest full-suite checkpoint is **496 passing software tests** in
`var/verification/supervisor-2026-09-05.xml`. It covers real PostgreSQL, concurrent
claims, restart/deadline behavior, researcher workspace execution and numerical
correctness fixtures. A separate real Codex researcher also collected a NOAA
dataset and proposed an unvalidated economic proxy. See the
[live smoke report](../research/supervisor-live-smoke-2026-09-05.md), including the
provider-usage accounting gap it exposed.

No matched-budget real-data alpha-mining benchmark or validated profitable
portfolio is established by those counts. The full open work is tracked in the
[implementation audit](implementation-status.md). Some older component guides
describe earlier checkpoints; this onboarding guide distinguishes those from the
current code and from planned capabilities.

**Bottom line:** today this is a functioning, partially integrated research
platform. Supply genuine historical data and a declared experiment to obtain
numerical evidence; supply researcher workers to generate new ideas. It is not
yet a finished “switch it on and leave it forever” product.
