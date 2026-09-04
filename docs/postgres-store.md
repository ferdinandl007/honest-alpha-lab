# Durable PostgreSQL research metadata and queue

`honest_alpha_lab.postgres_store` is a standalone psycopg 3 adapter, tested against
PostgreSQL 14. The [persistent numerical pipeline](persistent-research-loop.md) now
uses this adapter for reservation, execution and feedback. It does not yet replace
all legacy in-memory lifecycle, portfolio or reward integrations.
Install psycopg separately; dependency and integration files are deliberately
unchanged. Run from this checkout, or explicitly distribute the SQL files when
packaging the adapter.

## Installation and credentials

Use a dedicated database. A cluster administrator runs `sql/000_roles.sql` once
against that database. It creates four **NOLOGIN**, nonadministrative roles and
two private schemas. It intentionally fails on existing role/schema names rather
than adopting or modifying unrelated objects. Role names are cluster-wide: a
second database in the same cluster needs administrator provisioning of the two
schemas using the already existing roles instead of rerunning bootstrap.

Provision separate login credentials through your normal secret-management
process. Grant each application login exactly one inherited membership:

| Role | Allowed application operations |
| --- | --- |
| `hal_proposer` | Reserve/deduplicate candidates and append graph notes; read research metadata |
| `hal_worker` | Claim, heartbeat, reclaim, fail and complete trials; read research metadata |
| `hal_validator` | Create immutable runs; append/read sealed records; read research metadata |
| `hal_store_owner` | Own and migrate the schema; never an application credential |

The migrator login receives only `hal_store_owner` membership. It must be able to
`SET ROLE hal_store_owner`. Application logins must inherit their designated role;
they must not have owner membership, mixed research-role membership, SUPERUSER,
CREATEROLE, CREATEDB, REPLICATION, BYPASSRLS, or unrelated grants that undermine
these boundaries. Logins are deliberately not created by the migration helper.

```python
import os
from honest_alpha_lab.postgres_store import migrate

# Run in a separate administrative deployment step, never inside a worker.
installed = migrate(os.environ["HAL_MIGRATION_DSN"])
# For an installed package, supply sql_directory="/path/to/deployed/sql".
```

`migrate()` takes an advisory transaction lock, sets the owner role, and installs
`001_store.sql` plus its SHA-256 checksum atomically. Repeating the identical
migration returns `False`. A changed installed migration raises an error. This
is a version-one installer, not a general migration framework; future upgrades
need new migrations and runner support. Application connections never run DDL.

Every public mutation is a SECURITY DEFINER function owned by the NOLOGIN owner.
It uses a fixed `pg_catalog, honest_alpha, pg_temp` search path, qualifies tables,
and checks membership using `session_user`. Callers cannot acquire validator
authority by passing an actor string or changing `current_user` with `SET ROLE`.
PUBLIC has no execute privileges; internal helpers are not executable by any
application role. These measures follow PostgreSQL's
[SECURITY DEFINER guidance](https://www.postgresql.org/docs/14/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY)
and [session identity semantics](https://www.postgresql.org/docs/14/functions-info.html).

All research tables deny direct application writes. Statement triggers reject
UPDATE, DELETE, and TRUNCATE of immutable records, including accidental owner
mutations. Queue state and the budget/head projection are mutable only through
authorized functions. The queue-status view omits lease tokens and result bodies.

## Application example

```python
import os
from honest_alpha_lab.postgres_store import PostgresStore

# These objects belong in separate processes with separate credentials in deployment.
validator = PostgresStore(os.environ["HAL_VALIDATOR_DSN"])
proposer = PostgresStore(os.environ["HAL_PROPOSER_DSN"])
worker = PostgresStore(os.environ["HAL_WORKER_DSN"])

validator.create_run(
    "run-001",
    snapshot={"dataset_hashes": ["dataset-sha256"], "code_hash": "code-sha256"},
    policy={"split": "purged", "embargo_days": 5},
    budget={"max_trials": 100, "max_attempts": 3},
)
reservation = proposer.reserve_trial(
    "run-001",
    {"agent_kind": "symbolic_factor", "formula": "rank(momentum_20d)",
     "lineage_ids": ["prices-v1"], "parameters": {"window": 20}},
    metadata={"name": "Momentum proposal"},
)
# Reservation commits before any evaluation is dispatched.
# Duplicate submissions return the existing trial, with created=False.

worker.reclaim(run_id="run-001")
lease = worker.claim(run_id="run-001", lease_seconds=300)
if lease is not None:
    # Evaluate lease.specification against the run's immutable snapshot/policy.
    worker.heartbeat(lease, lease_seconds=300)
    worker.complete(lease, {"training_score": 0.42})
    # A validator can independently evaluate and persist a private result later:
    validator.seal_result(lease.trial_id, "holdout-v1", {"holdout_score": 0.31})

assert proposer.verify_chain("run-001")
```

Each API call opens its own connection and transaction, commits before returning,
and closes the connection. Store instances can be shared across threads, but
there is no connection pool. The application facade rejects privileged or mixed
role credentials on every connection; database functions enforce authorization
independently for callers using raw SQL. Do not run this behind a pool that
authenticates all callers as an owner login and merely switches roles: the
authorization boundary is the actual database session login.

## Identity, budget, and immutable records

Runs freeze snapshot, policy, the complete budget JSON, and extracted positive
`max_trials` / `max_attempts` limits before proposal work. `max_attempts` defaults
to 3 and must be between 1 and 100. Reusing a run ID with different inputs fails.
Snapshot and policy hashes are computed by PostgreSQL; their contents remain
available alongside the hashes. These are metadata commitments, not checks that
the referenced datasets or code exist or that a numerical worker actually used
them. Do not put sealed data into run inputs or other research-visible objects.

Candidate identity is SHA-256 of recursively normalized JSONB specification text.
Object key order and numeric scale (`1`, `1.0`) do not change identity; array order,
strings, explicit nulls, and different fields do. The server computes the identity
even for raw SQL clients. The unique key is `(run_id, candidate_hash)`, binding
deduplication to that run's snapshot and policy. Metadata is descriptive and
excluded from identity; on duplicate submissions the first stored metadata wins.
Include agent kind, formula, data lineage, and **every execution-relevant option**
in the specification. This is structural JSON deduplication, not semantic formula
equivalence. It is a separate encoding/version from `contracts.canonical_hash`.

Reservation locks the run's mutable state, checks deduplication **before** budget
exhaustion, then increments the counter, inserts the immutable candidate and
trial, creates the queue item, and appends both audit records in one transaction.
A duplicate returns the original trial even when budget is exhausted. Rollback
leaves none of these effects. A consumed reservation is never refunded, including
failed trials, and retries do not consume additional trial slots. Only
`max_trials` and `max_attempts` are enforced; runtime, tokens, dollars, and other
budget fields are stored as immutable metadata for future enforcement.

Each run has a SHA-256 hash chain of run creation, candidate registration, trial
reservation/claim/failure/reclamation/completion, and graph-event records.
The hashed frame is the UTF-8 PostgreSQL text representation of
`jsonb_build_array(run_id, sequence, previous_hash, envelope)`; sequence starts
at 1 and the predecessor of the first record is `GENESIS`. The envelope includes
kind, payload, authenticated session actor, and a UTC timestamp. Hashes, inserts,
and the head update are one transaction. Graph events are idempotent on
`(run_id, event_key)` for the same payload and login; conflicting reuse fails.
They are descriptive proposal/lineage notes, never authoritative acceptance,
promotion, or evaluation decisions. No graph referential-integrity or lifecycle
state machine is supplied by this adapter.

`records()` pages by sequence. `verify_chain()` uses a repeatable-read snapshot
and a server cursor, verifies every frame/link, and compares the final tail with
the stored head. It verifies the audit chain, not arbitrary external artifacts
or a complete reconciliation of mutable projections. The run-state row serializes
append operations per run; this deliberately favors correctness and simplicity
over high write throughput within a single run.

## Queue and retry semantics

Claims select a ready row with `FOR UPDATE SKIP LOCKED`, atomically increment its
attempt, and assign a fresh UUID token, authenticated lease owner, and server-time
deadline. Pending and running subsets have partial indexes. Claiming does not
implicitly reclaim: call `reclaim()` periodically, then claim again. The default
reclaim limit is 100, bounded at 1,000; leases are 1–86,400 seconds. A `None` claim
means no currently available, unlocked work was found, not that all work is done.

Heartbeats require the owning login, current token, and an unexpired lease. They
extend without shortening the deadline and do not append audit entries. The
original `Lease` object remains usable because the token stays fixed throughout
an attempt. Expired tokens cannot heartbeat, fail, or complete, even before a
reclaimer runs. Reclaimers use SKIP LOCKED, invalidate old tokens, and requeue
expired attempts until the attempt budget is exhausted, then mark them failed.
`fail(lease, error, retry_seconds=...)` similarly records a failure and schedules
a retry with a delay of 0–86,400 seconds, or terminally fails the final attempt.

Completion commits the public result and its audit record together. An identical
repeat from the same login and token returns the stored result without another
record, including after the original deadline. A changed result, another login,
or an obsolete token is rejected. **Work execution is at least once**: lease
expiry can overlap a replacement worker with a stalled process. Completion is
idempotent in PostgreSQL, but external computations and side effects need their
own trial-ID/attempt fencing. Keep transactions short when calling SQL functions
directly. Lease updates and hash-chain appends can wait on the run head even
though claims skip locked queue rows; there is no strict FIFO fairness guarantee.

No automatic retries hide ambiguous commits. Retry reserve, run creation,
graph append, sealed append, or completion with identical inputs. An uncertain
claim should be resolved by lease expiry/reclamation. Failure is not an idempotent
endpoint: after an ambiguous failure, inspect `get_trial()` before proceeding.
Retry a transaction after PostgreSQL serialization/deadlock errors as appropriate
for its operation. SQLSTATEs are preserved as psycopg exceptions:

| SQLSTATE | Meaning |
| --- | --- |
| `42501` | Role/session permission failure |
| `23505` | Conflicting immutable/idempotency key reuse |
| `55000` | Stale/foreign lease, immutable mutation, or invalid sealed-result state |
| `P0001` | Trial budget exhausted |
| `P0002` | Referenced run/trial missing in a function expecting an existing record |
| `22023` | Invalid input arguments |

## Sealed boundary and limits

`honest_alpha_sealed.records` is accessible only to the owner and validators.
Proposers and workers have neither schema usage nor table access nor execute
permission for `seal_result`. Sealed records can only be appended for completed
trials and are immutable/idempotent by trial and record key. Their payload hash
and content never enter the public audit chain. They are individually hashed,
not part of the public run hash chain. Validator results should go exclusively to
this endpoint; `complete()` writes research-visible results.

This supplies database permissions, **not full sealed OS/process isolation**.
Superusers, schema owners able to disable triggers, backups, server logs,
credentials, and processes sharing an OS account remain privileged surfaces.
Hash chains do not prevent a privileged administrator from rewriting history and
recomputing hashes. Use separately retained/exported tail anchors for stronger
tamper evidence. Research roles share all research-visible runs; there is no
per-user/per-tenant row isolation. Operational access, authentication, transport
security, backup/restore, monitoring, external artifact storage, and real numerical
validation remain deployment/integration responsibilities.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider tests/test_postgres_store.py -q
```

The suite creates its own `/tmp/hal-pg.XXXXXX` directory with `mktemp`, a 0700
Unix-socket directory, a private data directory, and a selected port. TCP listening
is disabled and host authentication rejected. Local trust authentication is only
for this disposable cluster beneath its private parent. It never reads an external
database DSN or drops an existing database/user. Its fixture stops its own server
and removes its own temporary directory in `finally`, including on assertion
failures. An uncatchable process kill may require manual cleanup of that exact
owned directory/cluster. Set `HAL_TEST_PG_BIN` to override `/opt/homebrew/bin`;
tests skip if binaries/psycopg are missing or the OS user is root.

Coverage includes concurrent canonical deduplication, competing budget
reservations, rollback, queue-row skipping, concurrent claims, duplicate
completion, heartbeat/expiry, token and login fencing, retry exhaustion/delay,
graph idempotency, immutable triggers, database role grants, search-path shadowing,
sealed read/write denial, hash corruption detection, and PostgreSQL restart
persistence. No existing numerical or orchestration modules are modified.

Verified on 2026-09-04 with PostgreSQL 14.19 and psycopg 3.3.4: **54 tests
passed in 13.20 seconds**, using the command above. Ruff checks of the new adapter
and test file also passed. The temporary cluster was stopped and removed by the
fixture. This is validation of this standalone backend, not a claim that the
overall project or its future integration is complete.
