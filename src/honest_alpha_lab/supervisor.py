"""Always-on scheduling of finite research jobs with durable single-host state.

The supervisor is a service, not one unlimited experiment. PostgreSQL continues
to own numerical trials. This SQLite store owns recurring research campaigns,
leases, retry accounting and a chronological operational event log.
"""
from __future__ import annotations

import json
import math
import multiprocessing
import os
import signal
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Timer
from uuid import NAMESPACE_URL, uuid4, uuid5

from .contracts import ContractError, canonical_hash


@dataclass(frozen=True)
class CampaignSpec:
    kind: str
    payload: dict
    interval_seconds: int = 300
    timeout_seconds: int = 900
    max_attempts: int = 2
    max_jobs_per_utc_day: int = 6  # Counts execution attempts, including retries.
    retry_delay_seconds: int = 30

    def __post_init__(self):
        if self.kind not in {"proposal", "numerical", "portfolio"} or not isinstance(self.payload, dict):
            raise ContractError("campaign needs a supported kind and object payload")
        json.dumps(self.payload, allow_nan=False)
        for name in ("interval_seconds", "timeout_seconds", "max_attempts", "max_jobs_per_utc_day", "retry_delay_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ContractError("campaign budgets and intervals must be positive integers")


@dataclass(frozen=True)
class CampaignLease:
    job_id: str
    campaign_id: str
    sequence: int
    attempt: int
    token: str
    spec: CampaignSpec


class SupervisorStore:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY, spec TEXT NOT NULL, spec_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, sequence INTEGER NOT NULL DEFAULT 0,
                next_due REAL NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
                token TEXT, lease_until REAL, result TEXT, UNIQUE(campaign_id,sequence));
              CREATE TABLE IF NOT EXISTS attempts (
                job_id TEXT NOT NULL, attempt INTEGER NOT NULL, campaign_id TEXT NOT NULL,
                started_at REAL NOT NULL, PRIMARY KEY(job_id,attempt));
              CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
                kind TEXT NOT NULL, job_id TEXT, payload TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS job_queue ON jobs(state,available_at);
              CREATE INDEX IF NOT EXISTS daily_attempts ON attempts(campaign_id,started_at);
            """)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _event(db, kind, job_id=None, **payload):
        db.execute("INSERT INTO events(created_at,kind,job_id,payload) VALUES(?,?,?,?)",
                   (time.time(), kind, job_id, json.dumps(payload, sort_keys=True, allow_nan=False)))

    def register(self, campaign_id, spec: CampaignSpec):
        if not isinstance(campaign_id, str) or not campaign_id or len(campaign_id) > 100:
            raise ContractError("campaign id must contain 1 to 100 characters")
        encoded = json.dumps(asdict(spec), sort_keys=True, allow_nan=False)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT spec_hash FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if old and old[0] != canonical_hash(spec):
                raise ContractError("campaign specification is immutable; pause it and register a new version")
            if not old:
                db.execute("INSERT INTO campaigns(id,spec,spec_hash) VALUES(?,?,?)", (campaign_id, encoded, canonical_hash(spec)))
                self._event(db, "campaign_registered", campaign_id=campaign_id, spec_hash=canonical_hash(spec))

    def set_active(self, campaign_id, active: bool):
        if type(active) is not bool:
            raise ContractError("active must be a boolean")
        with self._db() as db:
            if db.execute("UPDATE campaigns SET active=? WHERE id=?", (int(active), campaign_id)).rowcount != 1:
                raise ContractError("unknown campaign")
            self._event(db, "campaign_resumed" if active else "campaign_paused", campaign_id=campaign_id)

    def enqueue(self):
        """Refill active campaigns once; no duplicate open job per campaign."""
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            campaigns = db.execute("SELECT * FROM campaigns WHERE active=1 AND next_due<=? ORDER BY id", (now,)).fetchall()
            for campaign in campaigns:
                if db.execute("SELECT 1 FROM jobs WHERE campaign_id=? AND state IN ('pending','running')", (campaign["id"],)).fetchone():
                    continue
                sequence = campaign["sequence"] + 1
                job_id = str(uuid5(NAMESPACE_URL, f"honest-alpha:{campaign['id']}:{sequence}"))
                db.execute("INSERT INTO jobs(id,campaign_id,sequence,state,available_at) VALUES(?,?,?,'pending',?)",
                           (job_id, campaign["id"], sequence, now))
                db.execute("UPDATE campaigns SET sequence=? WHERE id=?", (sequence, campaign["id"]))
                self._event(db, "job_enqueued", job_id, campaign_id=campaign["id"], sequence=sequence)

    def claim(self, lease_seconds=None):
        if lease_seconds is not None and (not math.isfinite(lease_seconds) or lease_seconds <= 0):
            raise ContractError("lease duration must be positive and finite")
        now = time.time()
        day_start = now - now % 86400
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute("SELECT j.*,c.spec FROM jobs j JOIN campaigns c ON c.id=j.campaign_id WHERE state='running' AND lease_until<=?", (now,)).fetchall()
            for job in expired:
                spec = CampaignSpec(**json.loads(job["spec"]))
                self._fail(db, job, spec, "lease_expired", now)
            pending = db.execute("SELECT j.*,c.spec FROM jobs j JOIN campaigns c ON c.id=j.campaign_id WHERE j.state='pending' AND c.active=1 AND j.available_at<=? ORDER BY j.available_at,j.id", (now,)).fetchall()
            for job in pending:
                spec = CampaignSpec(**json.loads(job["spec"]))
                used = db.execute("SELECT COUNT(*) FROM attempts WHERE campaign_id=? AND started_at>=?", (job["campaign_id"], day_start)).fetchone()[0]
                if used >= spec.max_jobs_per_utc_day:
                    continue  # The service stays alive; tomorrow's budget can resume it.
                attempt, token = job["attempt"] + 1, str(uuid4())
                duration = spec.timeout_seconds + 5 if lease_seconds is None else lease_seconds
                db.execute("UPDATE jobs SET state='running',attempt=?,token=?,lease_until=? WHERE id=?", (attempt, token, now + duration, job["id"]))
                db.execute("INSERT INTO attempts VALUES(?,?,?,?)", (job["id"], attempt, job["campaign_id"], now))
                self._event(db, "job_claimed", job["id"], attempt=attempt)
                return CampaignLease(job["id"], job["campaign_id"], job["sequence"], attempt, token, spec)
        return None

    def heartbeat(self, lease, lease_seconds=30):
        now = time.time()
        with self._db() as db:
            changed = db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND state='running' AND token=? AND lease_until>?",
                                 (now + lease_seconds, lease.job_id, lease.token, now)).rowcount
            if changed != 1:
                raise ContractError("supervisor lease lost")

    def finish(self, lease, result, *, error=None):
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (lease.job_id,)).fetchone()
            if job and job["state"] == "completed" and job["token"] == lease.token and job["result"] == encoded and error is None:
                return
            if not job or job["state"] != "running" or job["token"] != lease.token or job["lease_until"] <= now:
                raise ContractError("cannot publish without the current supervisor lease")
            if error:
                self._fail(db, job, lease.spec, error, now)
            else:
                db.execute("UPDATE jobs SET state='completed',result=? WHERE id=?", (encoded, lease.job_id))
                db.execute("UPDATE campaigns SET next_due=? WHERE id=?", (now + lease.spec.interval_seconds, lease.campaign_id))
                self._event(db, "job_completed", lease.job_id, result_hash=canonical_hash(result))

    def _fail(self, db, job, spec, error, now):
        terminal = job["attempt"] >= spec.max_attempts
        db.execute("UPDATE jobs SET state=?,available_at=?,token=NULL,lease_until=NULL,result=? WHERE id=?",
                   ("failed" if terminal else "pending", now + spec.retry_delay_seconds,
                    json.dumps({"error_type": error}), job["id"]))
        if terminal:
            db.execute("UPDATE campaigns SET next_due=? WHERE id=?", (now + spec.interval_seconds, job["campaign_id"]))
        self._event(db, "job_failed" if terminal else "retry_scheduled", job["id"], error_type=error, attempt=job["attempt"])

    def status(self):
        with self._db() as db:
            return {"campaigns": [dict(row) for row in db.execute("SELECT id,active,sequence,next_due FROM campaigns ORDER BY id")],
                    "jobs": [dict(row) for row in db.execute("SELECT id,campaign_id,sequence,state,attempt,available_at,lease_until FROM jobs ORDER BY rowid")],
                    "events": [dict(row) for row in db.execute("SELECT * FROM events ORDER BY sequence DESC LIMIT 30")]}


def execute_campaign_job(lease, work_root):
    """Dispatch only trusted built-in handlers; agent output never selects code."""
    from .campaign_workers import execute

    return execute(lease, Path(work_root))


def _job_child(sender, lease, work_root, executor):
    if os.name == "posix":
        os.setsid()
    def expire():
        if os.name == "posix":
            os.killpg(os.getpid(), signal.SIGKILL)
        else:
            os._exit(124)
    watchdog = Timer(lease.spec.timeout_seconds, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        sender.send({"ready": True})
        sender.send({"result": executor(lease, work_root)})
    except Exception as exc:  # noqa: BLE001 -- never emit credentials from worker diagnostics
        sender.send({"error": type(exc).__name__})
    finally:
        sender.close()
        watchdog.cancel()
        if os.name == "posix":
            # Also clean up inherited-group descendants if the supervisor died.
            os.killpg(os.getpid(), signal.SIGKILL)


def _stop_child(process, group_started=False):
    if process.pid is None:
        return
    if not process.is_alive():
        process.join(timeout=0)
        return  # The worker's own finalizer already terminated its group.
    if os.name == "posix":
        # The child becomes group leader before executing work. Do not signal
        # an inherited parent group if startup stopped before setsid().
        try:
            if group_started or os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGTERM)
            elif process.is_alive():
                process.terminate()
        except ProcessLookupError:
            pass
        except PermissionError:
            process.join(timeout=0.1)
            if process.is_alive():
                raise
            return  # macOS can report EPERM for an already-exited group.
    elif process.is_alive():
        process.terminate()
    process.join(timeout=2)
    if process.is_alive() or group_started:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                process.join(timeout=0.1)
                if process.is_alive():
                    raise
        else:
            process.kill()
        process.join(timeout=2)


class ResearchSupervisor:
    def __init__(self, store, work_root, *, executor=execute_campaign_job):
        self.store, self.work_root, self.executor = store, str(Path(work_root).resolve()), executor
        Path(self.work_root).mkdir(parents=True, exist_ok=True)

    def step(self, stop: Event | None = None):
        self.store.enqueue()
        lease = self.store.claim()
        if lease is None:
            return None
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_job_child, args=(sender, lease, self.work_root, self.executor))
        deadline, heartbeat = time.monotonic() + lease.spec.timeout_seconds, time.monotonic() + 5
        message = {"error": "DeadlineExceeded"}
        group_started = False
        try:
            process.start()
            sender.close()
            while time.monotonic() < deadline:
                if stop is not None and stop.is_set():
                    message = {"error": "ServiceStopping"}
                    break
                if receiver.poll(min(0.2, max(0, deadline - time.monotonic()))):
                    try:
                        message = receiver.recv()
                    except EOFError:
                        message = {"error": "WorkerExited"}
                    if not message.get("ready"):
                        break
                    group_started = True
                    message = {"error": "DeadlineExceeded"}
                if time.monotonic() >= heartbeat:
                    self.store.heartbeat(lease, lease.spec.timeout_seconds + 5)
                    heartbeat = time.monotonic() + 5
        finally:
            sender.close()
            receiver.close()
            _stop_child(process, group_started)
        self.store.finish(lease, message.get("result"), error=message.get("error"))
        return {"job_id": lease.job_id, **message}

    def serve(self, stop: Event, idle_seconds=1):
        """Stay alive across idle queues and daily dispatch-budget exhaustion."""
        while not stop.is_set():
            try:
                self.step(stop)
            except (ContractError, sqlite3.Error, OSError) as exc:
                # Leave uncertain attempts reserved for lease recovery. Never
                # regenerate work solely because a database response was lost.
                print(json.dumps({"service_error": type(exc).__name__}), flush=True)
            stop.wait(idle_seconds)
