# Always-on research service

The product requirement is continuous, 24/7 signal discovery. The service now
has a persistent campaign scheduler and real researcher/numerical handlers.
Individual jobs have finite deadlines and retry allowances; idle queues and
daily dispatch limits do not terminate the service.

This is a new implementation checkpoint, not a completed production deployment
or a multi-day reliability certification. No service is installed at login or
boot by importing this module or writing its configuration.

## Start and inspect

Review `config/supervisor.example.json` first: it defines symbolic, text/event and
alternative-data discovery campaigns. Each may invoke the configured Codex account
up to six times per UTC day, including retries. Those are dispatch caps, **not
guaranteed dollar/token caps**. Adjust the configuration to your authorized budget.
The example neither buys data nor admits newly discovered sources automatically.

```sh
.venv/bin/python -m honest_alpha_lab supervise \
  --config config/supervisor.example.json \
  --state var/supervisor/state.sqlite \
  --work-root var/supervisor/work

.venv/bin/python -m honest_alpha_lab supervisor-status \
  --state var/supervisor/state.sqlite

.venv/bin/python -m honest_alpha_lab pause-campaign \
  --state var/supervisor/state.sqlite --campaign-id alternative-data-discovery-v1

.venv/bin/python -m honest_alpha_lab resume-campaign \
  --state var/supervisor/state.sqlite --campaign-id alternative-data-discovery-v1
```

The foreground service runs until SIGINT/SIGTERM. A paused campaign does not start
new work; already running work can finish. Stopping the service interrupts its
worker and preserves retry state. Restart with the **same state and work roots**.
Changing a registered specification is rejected; pause that campaign and register
a new version/ID. Removing a campaign from a configuration file does not pause
its persisted registration—use the explicit pause command.

One service process executes one job at a time. Several service processes can
share the same local state store and claim different campaigns. This is a
single-host SQLite scheduler, not a distributed/multi-host database. The numerical
trial queue remains PostgreSQL. Host boot/restart management, monitoring, backup,
disk retention and multi-day testing remain deployment work.

## What a job does

Proposal handlers use the network-enabled, writable research mode described in
[research workspaces](research-workspaces.md). Agents can write collectors, use
ordinary libraries and save arbitrary file formats. The optional bounded HTTPS
collector is not their mandatory path. Discovery-only results need no approved
lineage; emitted alpha candidates still do.

Each job freezes its input and prompt identity under `work/jobs/<job-id>/`.
Researchers receive their brief, campaign iteration, recent findings and optional
vault/development feedback. Prior findings are loaded across campaigns under the
same work root. The current policy rotates configured campaigns and supplies recent
memory; it is not yet a learned research planner or full semantic graph search.

Agent files remain in `work/research-scratch/`; task packages are in
`work/agent-tasks/`. `proposal.json` checkpoints findings before numerical
submission; `result.json` records the completed handoff. Stable candidate and
submission identities let retries reuse completed work without creating another
numerical trial. A completed CLI final response can also be recovered without
another model invocation. An interrupted CLI with **no** final response is retained
as an interrupted attempt, not silently regenerated under the same job identity.

State transitions are:

`pending → running → completed`, or `running → pending retry / failed`.

After a terminal job, the campaign schedules its next iteration after its configured
interval. Failed jobs do not end the service; repeated failures still consume the
daily attempt allowance. The service waits for a new UTC day when that allowance
is exhausted. This is not permission to renew a paid-data subscription or increase
authorized account spending.

## Connect numerical evaluation

A proposal payload may additionally contain `numerical_run_id`, `artifact_root`
and `lineage_ids`. With `HAL_PROPOSER_DSN` configured for the trusted handler, it
loads the locked run, adds measured development feedback to researcher context,
and submits executable proposals with stable identifiers.

Add a separate campaign with `kind: "numerical"` and payload fields `run_id`,
`snapshot_root`, `artifact_root`. It uses `HAL_WORKER_DSN` and processes one
numerical queue item per invocation. The supervisor does not pass these credentials
into the CLI researcher environment.

**Existing numerical run budgets and deadlines still apply.** Automatic creation
of successive independently declared numerical runs as data and campaign budgets
change is not implemented. An expired run is not silently extended, and its final
test is never reused as development feedback. The discovery-only example is not
an end-to-end validated signal campaign until real snapshots and numerical runs
are connected.

## Recovery and containment

Reservations and attempt counts commit before execution. Claims are exclusive;
expired leases can be recovered, and stale workers cannot publish. Default leases
cover the job deadline plus cleanup margin, so recovery does not intentionally
race a worker that is still within its execution allowance.

On POSIX, each worker owns a process group and a local watchdog. This watchdog
survives a supervisor crash and ends the worker at its deadline. Ordinary inherited
descendants are terminated with the group. Detached processes and arbitrary CLI
executables still require OS/container containment. Windows descendant cleanup
does not have the same guarantee. Database rows show last recorded state, not
proof that a service process is currently alive.

The local supervisor log is operational history, not an independently anchored
immutable alpha ledger. Keep it outside researcher write access. Full read
isolation, authenticated data/map admission, aggregate model spend accounting,
health alerts and complete disaster recovery are still required.

## Verification

Tests cover durable restart/refill, concurrent process claims, stale leases,
retry exhaustion, UTC-day allowance renewal, pause/resume, real worker deadlines,
supervisor-crash watchdog behavior, checkpoint reuse and cross-round findings.
CLI workspace tests exercise real subprocess collection/code execution without
paid model calls. `scripts/run_supervisor_smoke.py` separately runs one actual
Codex researcher and consumes account usage; it is not a financial benchmark.
