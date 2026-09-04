"""Real PostgreSQL tests in an owned, private, disposable cluster.

Run: PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider tests/test_postgres_store.py
Never connects to an existing PostgreSQL service. No databases/users are dropped.
"""

import os
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from honest_alpha_lab.postgres_store import PostgresStore, migrate

ROOT = Path(__file__).resolve().parents[1]


class Cluster:
    def __init__(self, directory, binaries, port):
        self.directory = directory
        self.binaries = binaries
        self.port = port

    def command(self, executable, *arguments):
        return subprocess.run([str(self.binaries / executable), *map(str, arguments)], check=True, capture_output=True, text=True, timeout=45)

    def start(self):
        self.command("pg_ctl", "-D", self.directory / "data", "-l", self.directory / "server.log", "-o",
                     f"-h '' -k {self.directory / 'socket'} -p {self.port} -c unix_socket_permissions=0700", "-w", "start")

    def stop(self):
        self.command("pg_ctl", "-D", self.directory / "data", "-m", "fast", "-w", "stop")

    def dsn(self, user):
        return make_conninfo(host=str(self.directory / "socket"), port=self.port, dbname="postgres", user=user)

    def store(self, role):
        return PostgresStore(self.dsn(role), statement_timeout_ms=5000)

    def admin(self):
        return psycopg.connect(self.dsn("hal_test_admin"))


@pytest.fixture(scope="module")
def cluster():
    binaries = Path(os.environ.get("HAL_TEST_PG_BIN", "/opt/homebrew/bin"))
    if not all((binaries / binary).exists() for binary in ("initdb", "pg_ctl", "postgres")):
        pytest.skip("real PostgreSQL binaries missing; set HAL_TEST_PG_BIN")
    if os.geteuid() == 0:
        pytest.skip("initdb requires a non-root OS user")
    # mktemp owns the entire target; the short path also fits Unix socket limits.
    directory = Path(subprocess.check_output(["mktemp", "-d", "/tmp/hal-pg.XXXXXX"], text=True).strip())
    directory.chmod(0o700)
    (directory / "socket").mkdir(mode=0o700)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    instance = Cluster(directory, binaries, port)
    try:
        instance.command("initdb", "-D", directory / "data", "-U", "hal_test_admin", "--auth-local=trust", "--auth-host=reject", "--no-locale", "--encoding=UTF8")
        instance.start()
        with psycopg.connect(instance.dsn("hal_test_admin"), autocommit=True) as conn:
            conn.execute((ROOT / "sql" / "000_roles.sql").read_text(), prepare=False)
            for login, role in (
                ("migrator", "hal_store_owner"), ("proposer_one", "hal_proposer"),
                ("proposer_two", "hal_proposer"), ("worker_one", "hal_worker"),
                ("worker_two", "hal_worker"), ("validator_one", "hal_validator"),
                ("mixed", "hal_worker"), ("owner_worker", "hal_store_owner"),
                ("noinherit_worker", "hal_worker"),
            ):
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(login)))
                conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(role), sql.Identifier(login)))
            conn.execute("CREATE ROLE outsider LOGIN")
            conn.execute("GRANT hal_validator TO mixed")
            conn.execute("GRANT hal_worker TO owner_worker")
            conn.execute("ALTER ROLE noinherit_worker NOINHERIT")
        assert migrate(instance.dsn("migrator"))
        yield instance
    finally:
        # Even failed setup/test assertions stop only this cluster, never a service.
        if (directory / "data" / "postmaster.pid").exists():
            instance.stop()
        shutil.rmtree(directory)


@pytest.fixture
def run(cluster):
    run_id = f"test-{uuid4()}"
    cluster.store("validator_one").create_run(run_id, snapshot={"data": "snapshot-1", "code": "code-1"},
        policy={"split": "purged"}, budget={"max_trials": 20, "max_attempts": 2})
    return run_id


def parallel(count, operation):
    barrier = threading.Barrier(count)

    def invoke(index):
        barrier.wait(timeout=10)
        return operation(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(invoke, range(count)))


def test_migrations_are_separate_and_idempotent(cluster):
    assert migrate(cluster.dsn("migrator")) is False
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        migrate(cluster.dsn("proposer_one"))
    for login in ("migrator", "hal_test_admin", "mixed", "owner_worker", "outsider", "noinherit_worker"):
        with pytest.raises(PermissionError):
            cluster.store(login).get_run("anything")
    with cluster.admin() as conn:
        assert conn.execute("SELECT bool_and(NOT rolcanlogin AND NOT rolsuper AND NOT rolcreaterole AND NOT rolbypassrls) FROM pg_roles WHERE rolname IN ('hal_store_owner','hal_proposer','hal_worker','hal_validator')").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname IN ('honest_alpha','honest_alpha_sealed') AND r.rolname <> 'hal_store_owner'").fetchone()[0] == 0


def test_run_inputs_are_immutable_and_replays_checked(cluster, run):
    validator = cluster.store("validator_one")
    original = validator.get_run(run)
    replay = validator.create_run(run, snapshot=original["snapshot"], policy=original["policy"], budget=original["budget"])
    assert replay["run_id"] == run
    with pytest.raises(psycopg.errors.UniqueViolation):
        validator.create_run(run, snapshot={"data": "changed"}, policy=original["policy"], budget=original["budget"])
    assert len(validator.records(run)) == 1
    assert validator.verify_chain(run)


def test_canonical_dedup_race_precedes_budget_and_work(cluster):
    run_id = str(uuid4())
    cluster.store("validator_one").create_run(run_id, snapshot={}, policy={}, budget={"max_trials": 1})
    results = parallel(12, lambda i: cluster.store("proposer_one" if i % 2 else "proposer_two").reserve_trial(
        run_id, {"nested": {"b": [1.0, {"n": 2.00}], "a": 1}} if i % 2 else {"nested": {"a": 1.0, "b": [1, {"n": 2}]}}, metadata={"label": i}))
    assert sum(result.created for result in results) == 1
    assert len({result.trial_id for result in results}) == 1
    proposer = cluster.store("proposer_one")
    assert proposer.get_run(run_id)["reserved_trials"] == 1
    with pytest.raises(psycopg.errors.RaiseException, match="budget exhausted"):
        proposer.reserve_trial(run_id, {"different": True})
    assert len(proposer.records(run_id)) == 3
    assert proposer.verify_chain(run_id)


def test_unique_candidate_budget_race_has_no_overspend(cluster):
    run_id = str(uuid4())
    cluster.store("validator_one").create_run(run_id, snapshot={}, policy={}, budget={"max_trials": 5})

    def reserve(index):
        try:
            return cluster.store("proposer_one").reserve_trial(run_id, {"formula": index})
        except psycopg.errors.RaiseException as error:
            assert "budget exhausted" in str(error)
            return None

    results = parallel(12, reserve)
    assert sum(result is not None for result in results) == 5
    with cluster.admin() as conn:
        for table in ("trials", "candidates", "queue"):
            assert conn.execute(sql.SQL("SELECT count(*) FROM honest_alpha.{} WHERE run_id=%s").format(sql.Identifier(table)), (run_id,)).fetchone()[0] == 5
    assert cluster.store("proposer_one").get_run(run_id)["reserved_trials"] == 5
    assert cluster.store("worker_one").verify_chain(run_id)


def test_reservation_rollback_is_atomic(cluster, run):
    with psycopg.connect(cluster.dsn("proposer_one")) as conn:
        conn.execute("SELECT honest_alpha.reserve_trial(%s,%s,%s)", (run, Jsonb({"formula": "rollback"}), Jsonb({})))
        conn.rollback()
    assert cluster.store("proposer_one").get_run(run)["reserved_trials"] == 0
    assert cluster.store("worker_one").claim(run_id=run) is None
    assert len(cluster.store("proposer_one").records(run)) == 1
    assert cluster.store("proposer_one").reserve_trial(run, {"formula": "rollback"}).reservation_index == 1


def test_skip_locked_claim_bypasses_held_queue_row(cluster, run):
    proposer = cluster.store("proposer_one")
    first = proposer.reserve_trial(run, {"formula": 1})
    second = proposer.reserve_trial(run, {"formula": 2})
    with cluster.admin() as locked:
        locked.execute("SELECT trial_id FROM honest_alpha.queue WHERE trial_id=%s FOR UPDATE", (first.trial_id,))
        lease = cluster.store("worker_one").claim(run_id=run)
        assert lease.trial_id == second.trial_id
    assert cluster.store("worker_two").claim(run_id=run).trial_id == first.trial_id


def test_claim_deadline_starts_after_audit_lock_wait(cluster, run):
    reservation = cluster.store("proposer_one").reserve_trial(run, {"formula": 1})
    with ThreadPoolExecutor(max_workers=1) as pool:
        with cluster.admin() as locked:
            locked.execute("SELECT * FROM honest_alpha.run_state WHERE run_id=%s FOR UPDATE", (run,))
            future = pool.submit(cluster.store("worker_one").claim, run_id=run, lease_seconds=1)
            # Wait for actual lock contention instead of assuming thread startup speed.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with cluster.admin() as observer:
                    waiting = observer.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename='worker_one' AND wait_event_type='Lock')").fetchone()[0]
                if waiting:
                    break
                time.sleep(0.02)
            else:
                pytest.fail("claim never reached the locked audit head")
            time.sleep(1.1)
        lease = future.result(timeout=5)
    assert lease.trial_id == reservation.trial_id
    assert lease.expires_at > datetime.now(UTC)
    cluster.store("worker_one").complete(lease, {"after_wait": True})


def test_concurrent_claim_and_completion_exactly_one_record(cluster, run):
    proposer = cluster.store("proposer_one")
    for index in range(6):
        proposer.reserve_trial(run, {"formula": index})
    leases = parallel(10, lambda _: cluster.store("worker_one").claim(run_id=run))
    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == len({lease.trial_id for lease in claimed}) == 6
    lease = claimed[0]
    results = parallel(8, lambda _: cluster.store("worker_one").complete(lease, {"score": 0.5}))
    assert all(result == {"score": 0.5} for result in results)
    with pytest.raises(psycopg.errors.UniqueViolation):
        cluster.store("worker_one").complete(lease, {"score": 0.6})
    completions = [row for row in proposer.records(run) if row["envelope"]["kind"] == "trial_completed"]
    assert len(completions) == 1
    assert completions[0]["envelope"]["actor"] == "worker_one"
    assert proposer.verify_chain(run)


def test_heartbeat_owner_token_and_expiration_fences(cluster, run):
    cluster.store("proposer_one").reserve_trial(run, {"formula": 1})
    worker = cluster.store("worker_one")
    lease = worker.claim(run_id=run, lease_seconds=1)
    expires = worker.heartbeat(lease, lease_seconds=2)
    assert expires > lease.expires_at
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        cluster.store("worker_two").heartbeat(lease)
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        cluster.store("worker_two").complete(lease, {})
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        worker.complete(replace(lease, token=uuid4()), {})
    time.sleep(max(0, (expires - datetime.now(UTC)).total_seconds()) + 0.05)
    for action in (lambda: worker.heartbeat(lease), lambda: worker.complete(lease, {}), lambda: worker.fail(lease, {})):
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            action()
    assert sorted(parallel(2, lambda _: worker.reclaim(run_id=run))) == [0, 1]
    renewed = cluster.store("worker_two").claim(run_id=run)
    assert renewed.attempt == 2 and renewed.token != lease.token
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        worker.complete(lease, {})
    cluster.store("worker_two").complete(renewed, {"valid": True})
    assert worker.verify_chain(run)


def test_retry_delay_and_attempt_limit_do_not_refund_budget(cluster, run):
    cluster.store("proposer_one").reserve_trial(run, {"formula": 1})
    worker = cluster.store("worker_one")
    first = worker.claim(run_id=run)
    assert worker.fail(first, {"error": "temporary"}, retry_seconds=1) == "pending"
    assert worker.claim(run_id=run) is None
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        worker.complete(first, {})
    time.sleep(1.05)
    second = worker.claim(run_id=run)
    assert second.attempt == 2
    assert worker.fail(second, {"error": "permanent"}) == "failed"
    assert worker.claim(run_id=run) is None
    assert worker.get_run(run)["reserved_trials"] == 1
    assert worker.get_trial(first.trial_id)["state"] == "failed"
    assert worker.verify_chain(run)


def test_expired_final_attempt_is_terminal(cluster):
    run_id = str(uuid4())
    cluster.store("validator_one").create_run(run_id, snapshot={}, policy={}, budget={"max_trials": 1, "max_attempts": 1})
    cluster.store("proposer_one").reserve_trial(run_id, {"formula": 1})
    worker = cluster.store("worker_one")
    lease = worker.claim(run_id=run_id, lease_seconds=1)
    time.sleep(1.05)
    assert worker.reclaim(run_id=run_id) == 1
    assert worker.get_trial(lease.trial_id)["state"] == "failed"
    assert worker.claim(run_id=run_id) is None
    assert worker.reclaim(run_id=run_id) == 0


def test_graph_events_are_append_only_idempotent_and_actor_is_session(cluster, run):
    proposer = cluster.store("proposer_one")
    event = {"source": "a", "target": "b", "relation": "derived_from", "actor": "validator_one"}
    assert proposer.append_graph_event(run, "edge-1", event) == event
    assert proposer.append_graph_event(run, "edge-1", event) == event
    with pytest.raises(psycopg.errors.UniqueViolation):
        proposer.append_graph_event(run, "edge-1", {"relation": "changed"})
    with pytest.raises(psycopg.errors.UniqueViolation):
        cluster.store("proposer_two").append_graph_event(run, "edge-1", event)
    events = proposer.records(run, after_sequence=1, limit=1)
    assert len(events) == 1
    assert events[0]["envelope"]["actor"] == "proposer_one"
    assert proposer.verify_chain(run)


def test_sealed_results_are_validator_only_and_never_in_public_chain(cluster, run):
    proposer, worker, validator = (cluster.store(login) for login in ("proposer_one", "worker_one", "validator_one"))
    reservation = proposer.reserve_trial(run, {"formula": 1})
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        validator.seal_result(reservation.trial_id, "holdout", {})
    lease = worker.claim(run_id=run)
    worker.complete(lease, {"training": "public"})
    payload = {"holdout": "PRIVATE_SENTINEL_84f131"}
    first = validator.seal_result(lease.trial_id, "holdout", payload)
    assert validator.seal_result(lease.trial_id, "holdout", payload) == first
    assert validator.sealed_records(lease.trial_id)[0]["payload"] == payload
    with pytest.raises(psycopg.errors.UniqueViolation):
        validator.seal_result(lease.trial_id, "holdout", {"holdout": "changed"})
    for research in (proposer, worker):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            research.sealed_records(lease.trial_id)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            research.seal_result(lease.trial_id, "forged", payload)
        assert "PRIVATE_SENTINEL" not in str(research.records(run))
    assert worker.verify_chain(run)


@pytest.mark.parametrize("login,statement", [
    ("proposer_one", "SELECT honest_alpha.claim(NULL, 10)"),
    ("proposer_one", "SELECT honest_alpha.create_run('bad','{}','{}','{\"max_trials\":1}')"),
    ("worker_one", "SELECT honest_alpha.reserve_trial('bad','{}','{}')"),
    ("validator_one", "SELECT honest_alpha.reserve_trial('bad','{}','{}')"),
    ("validator_one", "SELECT honest_alpha.claim(NULL, 10)"),
    ("proposer_one", "SELECT honest_alpha._append('bad','trial_completed','{}')"),
    ("worker_one", "SELECT * FROM honest_alpha.queue"),
    ("proposer_one", "SELECT * FROM honest_alpha_sealed.records"),
    ("worker_one", "SELECT * FROM honest_alpha_sealed.records"),
    ("proposer_one", "SET ROLE hal_validator"),
    ("outsider", "SELECT honest_alpha.claim(NULL, 10)"),
    ("worker_one", "CREATE TABLE honest_alpha.injected (id int)"),
])
def test_database_permissions_cannot_be_bypassed_by_raw_sql(cluster, login, statement):
    with psycopg.connect(cluster.dsn(login)) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


@pytest.mark.parametrize("table", ["runs", "candidates", "trials", "records", "graph_events", "run_state", "queue", "schema_version"])
def test_research_roles_cannot_mutate_tables(cluster, table):
    for login in ("proposer_one", "worker_one", "validator_one"):
        with psycopg.connect(cluster.dsn(login)) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(sql.SQL("DELETE FROM honest_alpha.{}").format(sql.Identifier(table)))


@pytest.mark.parametrize("table", ["honest_alpha.runs", "honest_alpha.candidates", "honest_alpha.trials", "honest_alpha.records", "honest_alpha.graph_events", "honest_alpha.schema_version", "honest_alpha_sealed.records"])
def test_immutable_triggers_even_reject_owner_mutation(cluster, table):
    for action in ("DELETE FROM {}", "TRUNCATE {} CASCADE"):
        with cluster.admin() as conn, pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="immutable"):
            conn.execute(sql.SQL(action).format(sql.Identifier(*table.split("."))))


def test_functions_are_hardened_and_search_path_cannot_shadow(cluster, run):
    cluster.store("proposer_one").reserve_trial(run, {"formula": "real"})
    with cluster.admin() as conn:
        functions = conn.execute("SELECT p.oid, p.prosecdef, p.proconfig, p.proowner::regrole::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='honest_alpha'").fetchall()
        assert functions
        for oid, definer, config, owner in functions:
            assert owner == "hal_store_owner"
            assert "search_path=pg_catalog, honest_alpha, pg_temp" in config
            assert not conn.execute("SELECT has_function_privilege('outsider', %s, 'EXECUTE')", (oid,)).fetchone()[0]
    with psycopg.connect(cluster.dsn("worker_one")) as conn:
        conn.execute("CREATE TEMP TABLE queue (trial_id uuid)")
        conn.execute("CREATE TEMP TABLE run_state (run_id text)")
        conn.execute("SET LOCAL search_path = pg_temp, public")
        row = conn.execute("SELECT honest_alpha.claim(%s,30)", (run,)).fetchone()[0]
        assert row["specification"] == {"formula": "real"}
    with psycopg.connect(cluster.dsn("mixed")) as conn:
        conn.execute("SET LOCAL ROLE hal_worker")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="unauthorized database session"):
            conn.execute("SELECT honest_alpha.claim(NULL,30)")


def test_session_authorization_survives_accidental_execute_grant(cluster):
    # Grants are rolled back; this tests the function's independent login check.
    with cluster.admin() as admin:
        admin.execute("GRANT USAGE ON SCHEMA honest_alpha TO outsider")
        admin.execute("GRANT EXECUTE ON FUNCTION honest_alpha.claim(text,integer) TO outsider")
        admin.execute("SET LOCAL SESSION AUTHORIZATION outsider")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="unauthorized database session"):
            admin.execute("SELECT honest_alpha.claim(NULL,30)")


def test_future_owner_functions_default_to_no_public_execution(cluster):
    with psycopg.connect(cluster.dsn("migrator")) as conn:
        conn.execute("SET LOCAL ROLE hal_store_owner")
        conn.execute("CREATE FUNCTION honest_alpha.future_permission_probe() RETURNS integer LANGUAGE sql AS 'SELECT 1'")
        assert not conn.execute("SELECT has_function_privilege('outsider', 'honest_alpha.future_permission_probe()', 'EXECUTE')").fetchone()[0]
        conn.rollback()


def test_immutable_update_rejected(cluster, run):
    with cluster.admin() as conn, pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="immutable"):
        conn.execute("UPDATE honest_alpha.runs SET policy='{}' WHERE run_id=%s", (run,))


def test_chain_detects_tampering_and_missing_tail(cluster, run):
    proposer = cluster.store("proposer_one")
    proposer.reserve_trial(run, {"formula": 1})
    assert proposer.verify_chain(run)
    # Only the disposable cluster administrator can disable these guards.
    with cluster.admin() as conn:
        conn.execute("ALTER TABLE honest_alpha.records DISABLE TRIGGER immutable")
        original = conn.execute("SELECT envelope FROM honest_alpha.records WHERE run_id=%s AND sequence=2", (run,)).fetchone()[0]
        conn.execute("UPDATE honest_alpha.records SET envelope='{}' WHERE run_id=%s AND sequence=2", (run,))
    try:
        assert not proposer.verify_chain(run)
    finally:
        with cluster.admin() as conn:
            conn.execute("UPDATE honest_alpha.records SET envelope=%s WHERE run_id=%s AND sequence=2", (Jsonb(original), run))
            conn.execute("ALTER TABLE honest_alpha.records ENABLE TRIGGER immutable")
    assert proposer.verify_chain(run)
    with cluster.admin() as conn:
        conn.execute("UPDATE honest_alpha.run_state SET last_sequence=last_sequence+1 WHERE run_id=%s", (run,))
    try:
        assert not proposer.verify_chain(run)
    finally:
        with cluster.admin() as conn:
            conn.execute("UPDATE honest_alpha.run_state SET last_sequence=last_sequence-1 WHERE run_id=%s", (run,))
    with pytest.raises(KeyError):
        proposer.verify_chain("missing")


@pytest.mark.parametrize("budget", [{"max_trials": 0}, {"max_trials": 1.5}, {}, {"max_trials": "2"}, {"max_trials": 1, "max_attempts": 0}, {"max_trials": 1, "max_attempts": None}, {"max_trials": 1, "max_attempts": "2"}])
def test_invalid_budgets_rejected_in_database(cluster, budget):
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        cluster.store("validator_one").create_run(str(uuid4()), snapshot={}, policy={}, budget=budget)


def test_invalid_json_and_lease_arguments(cluster, run):
    with pytest.raises(ValueError):
        cluster.store("proposer_one").reserve_trial(run, {"value": float("nan")})
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        cluster.store("worker_one").claim(run_id=run, lease_seconds=0)
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        cluster.store("worker_one").reclaim(run_id=run, limit=0)


def test_committed_records_queue_and_leases_survive_server_restart(cluster, run):
    proposer = cluster.store("proposer_one")
    first = proposer.reserve_trial(run, {"formula": "running"})
    second = proposer.reserve_trial(run, {"formula": "pending"})
    worker = cluster.store("worker_one")
    lease = worker.claim(run_id=run, lease_seconds=60)
    assert lease.trial_id == first.trial_id
    before = proposer.records(run)
    cluster.stop()
    cluster.start()
    reconnected = cluster.store("worker_one")
    assert reconnected.records(run) == before
    assert reconnected.get_run(run)["reserved_trials"] == 2
    assert reconnected.get_trial(first.trial_id)["state"] == "running"
    assert reconnected.get_trial(second.trial_id)["state"] == "pending"
    assert reconnected.complete(lease, {"after_restart": True}) == {"after_restart": True}
    assert reconnected.claim(run_id=run).trial_id == second.trial_id
    assert reconnected.verify_chain(run)
