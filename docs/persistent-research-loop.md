# Persistent numerical feedback loop

The implemented path is now:

`proposal → canonical reservation → leased numerical process → immutable development evidence → next proposal context`

It is not yet the full research platform. Source acquisition, independent dataset
approval, text/proxy feature compilation, portfolio-cost integration, global agent
spend control and matched-budget real-data benchmarks remain required.

## Separation of services

Use the PostgreSQL roles and private schemas described in [the storage guide](postgres-store.md).
The validator freezes a `NumericalPlan`; a proposer can reserve formulas but cannot
change that plan; a numerical worker consumes the queue. Agent task contexts
contain field names, fixed baselines and development summaries, not database
sessions or snapshot filesystem locations.

The plan commits to the actual imported snapshot hash, development cutoff, permitted
feature fields, baseline formulas, label horizon, trailing beta window, conventional
model configuration, and per-trial runtime. The run separately limits wall-clock
time, unique numerical trials and attempts. Source files and numerical dependency
versions are fingerprinted; changed runtime/source stops new work on that run.

These are research-service boundaries. Processes still need separate OS identities,
protected credentials and data mounts in deployment. Never expose the worker's
environment or sealed storage to a proposal CLI. This code does not itself establish
complete OS isolation or provider licensing.

## Command-line service entry points

Provision credentials separately; do not store them in prompts, plans or shell
arguments. Each service command reads only its named connection variable:

| Command | Connection variable | Purpose |
| --- | --- | --- |
| `freeze-run` | `HAL_VALIDATOR_DSN` | Commit the plan and budgets |
| `submit-formula` | `HAL_PROPOSER_DSN` | Log a proposal and reserve canonical computation |
| `work-one` | `HAL_WORKER_DSN` | Claim and execute one numerical job |
| `research-feedback` | `HAL_PROPOSER_DSN` | Read verified development summaries |

Prepare a plan using `NumericalPlan(...).payload()` and JSON serialization. It must
name a real imported snapshot, the actual fields and a predeclared development end.
There is no default fabricated snapshot or automatic dataset approval.

```sh
.venv/bin/python -m honest_alpha_lab freeze-run \
  --run-id research-001 --plan /absolute/path/plan.json \
  --max-trials 30 --max-attempts 1 --max-runtime-seconds 3600

.venv/bin/python -m honest_alpha_lab submit-formula \
  --run-id research-001 --submission-id proposal-001 \
  --formula 'rank(ts_delta(close, 20))'

.venv/bin/python -m honest_alpha_lab work-one \
  --run-id research-001 --snapshot-root /absolute/path/snapshots \
  --artifact-root /absolute/path/development-artifacts

.venv/bin/python -m honest_alpha_lab research-feedback \
  --run-id research-001 --artifact-root /absolute/path/development-artifacts
```

Commands assume their own service environment is already configured. A database
driver failure produces its error class and SQLSTATE, not a connection string.
`work-one` reporting no claim does **not** prove that all workers are finished.

## Identity and audit behavior

`submit_formula` records each submitted attempt with a stable submission ID before
parsing and reservation. Formatting and commutative-order variants serialize to
one canonical executable expression, so they share a numerical trial within the
locked run. Replaying the same submission is idempotent; changing its contents is
not allowed. Invalid field/formula attempts are retained as rejected proposal notes.
These notes are not alpha approvals or completed numerical evaluations.

The fixed numerical child verifies the plan, formula hash, allowed fields and
content-addressed snapshot. It builds the observed panel and residual labels,
runs purged walk-forward evaluation, and saves report/prediction artifacts. A
parent process renews the database lease, enforces the smaller of the per-trial
timeout and remaining run budget, and terminates an over-budget child. Late or
foreign lease tokens cannot publish results. Process termination has a bounded
cleanup grace period; this is not a memory-limit sandbox.

Numerical outcomes distinguish:

- `evaluated`: actual numerical diagnostics were computed, not financial acceptance.
- `not_evaluated`: inputs or usable evidence were unavailable; no invented metrics.
- execution failure: the attempt is spent, recorded and retried only within policy.

`completed` in the SQL queue means the **job** reached a terminal result. It does
not mean the candidate passed validation. Results retain `financial_alpha_verified:
false`; explicitly permitted correctness fixtures remain labeled as fixtures.

## Adaptive proposal rounds

`QueuedResearchLoop` wraps the existing `DiversityResearchLoop`, which can use a
file-backed CLI worker. Before a round it inserts measured development feedback
and the locked field/horizon/baseline context. After proposals are recorded, it
reserves their executable DSLs and drains bounded numerical work. The next round
cannot start while those numerical trials are pending or running.

An idea without compiled DSL is recorded as `data_needed`, including text/event
and alternative-data hypotheses that still need a feature builder. It does not
receive surrogate backtest results. `drain()` can recover already-reserved work
without invoking proposal agents again.

This version requires **one round coordinator**. Numerical workers use database
leases, but distributed proposal-round leasing and crash recovery between proposal
creation and trial reservation are not yet complete. A round interrupted during
submission may require replaying its saved candidates with their stable submission
IDs. Do not regenerate proposals to conceal an interrupted attempt.

Feedback retrieval verifies content hashes and binds reports to the locked formula,
snapshot, model policy and library. It includes the evaluated expression and its
candidate linkage. It reads only public development records, not the validator's
sealed schema. Stored diagnostics remain readable after the run deadline and after
source updates; executing additional trials does not.

## Verification and remaining budget work

`tests/test_pipeline.py` uses a real private PostgreSQL cluster and real numerical
child processes over explicit correctness fixtures. It verifies canonical duplicate
reservation, persisted evaluation, next-round feedback, data-needed outcomes and
actual timeout termination. This is software integration evidence, not a synthetic
financial benchmark or a claim of agent-discovered alpha.

Global model-token/dollar accounting, bounded invalid-proposal generation, compute
resource limits beyond elapsed time, and conventional cost-sensitive scheduling
rewards remain unfinished. The research reward harness continues to reject missing
required evidence; the raw feedback diagnostics do not bypass it.
