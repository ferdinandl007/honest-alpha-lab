"""Standalone PostgreSQL research store; no implicit migrations or global connection.

Install psycopg 3 separately. Application logins inherit exactly one of
hal_proposer, hal_worker or hal_validator. Migration credentials stay outside this
class. Every method commits before returning and closes its connection, making
instances safe to share across threads. See docs/postgres-store.md for semantics.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def _json(value: Mapping[str, Any]) -> Jsonb:
    """Reject non-JSON values and NaN/Infinity before sending any work to PostgreSQL."""
    if not isinstance(value, Mapping):
        raise TypeError("expected a JSON object")
    return Jsonb(dict(value), dumps=lambda item: json.dumps(item, allow_nan=False))


@dataclass(frozen=True, slots=True)
class Reservation:
    trial_id: UUID
    run_id: str
    candidate_hash: str
    reservation_index: int
    created: bool


@dataclass(frozen=True, slots=True)
class Lease:
    trial_id: UUID
    run_id: str
    token: UUID
    owner: str
    attempt: int
    expires_at: datetime
    specification: dict[str, Any]


def migrate(owner_dsn: str, *, sql_directory: str | Path | None = None) -> bool:
    """Install migration 001 once, using a dedicated login allowed to SET ROLE owner.

    Bootstrap sql/000_roles.sql separately as a cluster administrator first.
    Returns True on installation, False for an already installed matching file.
    An altered installed migration is rejected. All DDL and the checksum commit
    together. This helper never creates database users or grants memberships.
    """
    directory = Path(sql_directory) if sql_directory is not None else Path(__file__).resolve().parents[2] / "sql"
    migration = (directory / "001_store.sql").read_bytes()
    checksum = hashlib.sha256(migration).hexdigest()
    with psycopg.connect(owner_dsn) as conn:
        conn.execute("SET LOCAL ROLE hal_store_owner")
        conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(714036915024117)")
        present = conn.execute("SELECT pg_catalog.to_regclass('honest_alpha.schema_version')").fetchone()[0]
        if present:
            row = conn.execute("SELECT checksum FROM honest_alpha.schema_version WHERE version = 1").fetchone()
            if row is None or row[0] != checksum:
                raise ValueError("installed migration checksum differs; use a new migration")
            return False
        conn.execute(migration.decode("utf-8"), prepare=False)
        conn.execute("INSERT INTO honest_alpha.schema_version(version, checksum) VALUES (1, %s)", (checksum,))
    return True


class PostgresStore:
    """Small transactional facade over role-restricted database functions.

    psycopg exceptions retain SQLSTATE: 42501 permission denied, 23505 immutable
    idempotency conflict, 55000 stale lease, P0001 exhausted budget, P0002 missing
    run/trial, 22023 invalid arguments. No retries hide uncertain commit outcomes:
    repeat reserve/complete/create_run/append_graph_event/seal_result with the same
    inputs; reclaim leases after an uncertain claim. Failure/heartbeat are fenced
    by the attempt token, and are not general idempotency endpoints.
    """

    def __init__(self, dsn: str, *, connect_timeout: int = 10, statement_timeout_ms: int = 30_000) -> None:
        if connect_timeout <= 0 or statement_timeout_ms <= 0:
            raise ValueError("connection and statement timeouts must be positive")
        self._dsn = dsn
        self._connect_timeout = connect_timeout
        self._statement_timeout_ms = statement_timeout_ms

    @contextmanager
    def _connection(self, *, audit: bool = False) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, connect_timeout=self._connect_timeout, row_factory=dict_row) as conn:
            if audit:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SELECT pg_catalog.set_config('statement_timeout', %s, true)", (str(self._statement_timeout_ms),))
            identity = conn.execute("""
                SELECT current_user = session_user AS unchanged,
                       (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls
                        OR pg_catalog.pg_has_role(session_user, 'hal_store_owner', 'MEMBER')) AS privileged,
                       (SELECT count(*) FROM unnest(ARRAY['hal_proposer','hal_worker','hal_validator']) r
                        WHERE pg_catalog.pg_has_role(session_user, r, 'MEMBER')) AS memberships,
                       (SELECT count(*) FROM unnest(ARRAY['hal_proposer','hal_worker','hal_validator']) r
                        WHERE pg_catalog.pg_has_role(session_user, r, 'USAGE')) AS inherited
                FROM pg_catalog.pg_roles WHERE rolname = session_user
            """).fetchone()
            if not identity or not identity["unchanged"] or identity["privileged"] or identity["memberships"] != 1 or identity["inherited"] != 1:
                raise PermissionError("application connection needs exactly one inherited research role and no owner/admin privileges")
            yield conn

    def _call(self, function: str, *args: Any) -> Any:
        query = sql.SQL("SELECT honest_alpha.{}({}) AS result").format(
            sql.Identifier(function), sql.SQL(", ").join(sql.Placeholder() for _ in args)
        )
        with self._connection() as conn:
            return conn.execute(query, args).fetchone()["result"]

    def create_run(self, run_id: str, *, snapshot: Mapping[str, Any], policy: Mapping[str, Any], budget: Mapping[str, Any]) -> dict[str, Any]:
        """Validator: freeze snapshot, policy and budget before any proposal work."""
        return self._call("create_run", run_id, _json(snapshot), _json(policy), _json(budget))

    def reserve_trial(self, run_id: str, specification: Mapping[str, Any], *, metadata: Mapping[str, Any] | None = None) -> Reservation:
        """Proposer: canonical dedup, nonrefundable trial reservation and enqueue.

        Include every execution-relevant parameter in specification. Metadata is
        descriptive only; the first accepted metadata survives duplicate calls.
        """
        row = self._call("reserve_trial", run_id, _json(specification), _json({} if metadata is None else metadata))
        return Reservation(UUID(row["trial_id"]), row["run_id"], row["candidate_hash"], row["reservation_index"], row["created"])

    def claim(self, *, run_id: str | None = None, lease_seconds: int = 300) -> Lease | None:
        """Worker: claim one ready item without waiting on other locked queue rows."""
        row = self._call("claim", run_id, lease_seconds)
        if row is None:
            return None
        return Lease(UUID(row["trial_id"]), row["run_id"], UUID(row["lease_token"]), row["lease_owner"], row["attempts"], datetime.fromisoformat(row["lease_until"]), row["specification"])

    def heartbeat(self, lease: Lease, *, lease_seconds: int = 300) -> datetime:
        """Worker: extend an unexpired, owned lease; an expired token cannot revive."""
        return self._call("heartbeat", lease.trial_id, lease.token, lease_seconds)

    def reclaim(self, *, run_id: str | None = None, limit: int = 100) -> int:
        """Worker: requeue expired leases, or terminally fail exhausted attempts."""
        return self._call("reclaim", run_id, limit)

    def complete(self, lease: Lease, result: Mapping[str, Any]) -> dict[str, Any]:
        """Worker: atomically append result and complete once; identical replay is safe.

        These results are research-visible. Never send sealed data to this method.
        """
        return self._call("complete", lease.trial_id, lease.token, _json(result))

    def fail(self, lease: Lease, error: Mapping[str, Any], *, retry_seconds: int = 0) -> str:
        """Worker: release an owned attempt with bounded retry delay; return state."""
        return self._call("fail", lease.trial_id, lease.token, _json(error), retry_seconds)

    def append_graph_event(self, run_id: str, event_key: str, event: Mapping[str, Any]) -> dict[str, Any]:
        """Proposer: append a lineage/proposal note; this grants no promotion authority."""
        return self._call("append_graph_event", run_id, event_key, _json(event))

    def seal_result(self, trial_id: UUID, record_key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validator: append a private result for a completed trial, idempotently."""
        return self._call("seal_result", trial_id, record_key, _json(payload))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            return conn.execute("SELECT r.*, s.reserved_trials FROM honest_alpha.runs r JOIN honest_alpha.run_state s USING(run_id) WHERE run_id = %s", (run_id,)).fetchone()

    def get_trial(self, trial_id: UUID) -> dict[str, Any] | None:
        """Research-visible immutable reservation plus current queue projection."""
        with self._connection() as conn:
            return conn.execute("SELECT t.*, q.state, q.attempts, q.max_attempts, q.available_at, q.lease_owner, q.lease_until FROM honest_alpha.trials t JOIN honest_alpha.queue_status q USING(trial_id, run_id) WHERE t.trial_id = %s", (trial_id,)).fetchone()

    def sealed_records(self, trial_id: UUID) -> list[dict[str, Any]]:
        with self._connection() as conn:
            return conn.execute("SELECT * FROM honest_alpha_sealed.records WHERE trial_id = %s ORDER BY created_at, record_key", (trial_id,)).fetchall()

    def records(self, run_id: str, *, after_sequence: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        """Read audit records in bounded sequence pages; no sealed content is included."""
        if after_sequence < 0 or not 1 <= limit <= 10_000:
            raise ValueError("invalid sequence or page limit")
        with self._connection() as conn:
            return conn.execute("SELECT * FROM honest_alpha.records WHERE run_id = %s AND sequence > %s ORDER BY sequence LIMIT %s", (run_id, after_sequence, limit)).fetchall()

    def verify_chain(self, run_id: str) -> bool:
        """Verify all hashes and the stored tail in one stable database snapshot.

        PostgreSQL supplies the exact JSONB text framing (not Python JSON format).
        A server-side cursor bounds memory. This detects corruption, not an owner
        rewriting both records and their hashes; externally anchor tails for that.
        """
        with self._connection(audit=True) as conn:
            head = conn.execute("SELECT last_sequence, last_hash FROM honest_alpha.run_state WHERE run_id = %s", (run_id,)).fetchone()
            if head is None:
                raise KeyError(run_id)
            sequence, previous = 0, "GENESIS"
            with conn.cursor(name="verify_research_chain") as cursor:
                cursor.execute("SELECT sequence, previous_hash, entry_hash, pg_catalog.jsonb_build_array(run_id, sequence, previous_hash, envelope)::text AS frame FROM honest_alpha.records WHERE run_id = %s ORDER BY sequence", (run_id,))
                for row in cursor:
                    sequence += 1
                    if row["sequence"] != sequence or row["previous_hash"] != previous:
                        return False
                    if hashlib.sha256(row["frame"].encode("utf-8")).hexdigest() != row["entry_hash"]:
                        return False
                    previous = row["entry_hash"]
            return sequence == head["last_sequence"] and previous == head["last_hash"]
