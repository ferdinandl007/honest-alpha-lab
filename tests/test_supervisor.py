"""Durable scheduling and genuine process tests; not alpha benchmarks."""
import json
import multiprocessing
import os
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.supervisor import CampaignSpec, ResearchSupervisor, SupervisorStore


def spec(**kwargs):
    return CampaignSpec("proposal", {}, **kwargs)


def expire_clock(monkeypatch, offset):
    real = time.time()
    monkeypatch.setattr("honest_alpha_lab.supervisor.time.time", lambda: real + offset)


def test_restart_refills_without_duplicate_pending_work(tmp_path):
    path = tmp_path / "state.sqlite"
    first = SupervisorStore(path)
    first.register("alpha", spec())
    first.enqueue()
    second = SupervisorStore(path)
    second.register("alpha", spec())
    second.enqueue()
    assert len(second.status()["jobs"]) == 1
    lease = second.claim()
    second.finish(lease, {"observations": 1})
    second.finish(lease, {"observations": 1})  # Same completion is idempotent.
    second.enqueue()
    assert len(second.status()["jobs"]) == 1
    with pytest.raises(ContractError):
        second.register("alpha", spec(max_attempts=9))


def test_expired_lease_retries_same_job_and_fences_old_worker(tmp_path, monkeypatch):
    store = SupervisorStore(tmp_path / "state.sqlite")
    store.register("alpha", spec(retry_delay_seconds=1))
    store.enqueue()
    old = store.claim(lease_seconds=1)
    expire_clock(monkeypatch, 2)
    assert store.claim() is None  # Schedules, but does not bypass retry delay.
    expire_clock(monkeypatch, 2)
    new = store.claim()
    assert new.job_id == old.job_id and new.attempt == 2 and new.token != old.token
    with pytest.raises(ContractError, match="current"):
        store.finish(old, {"stale": True})
    store.finish(new, {"recovered": True})


def test_daily_dispatch_budget_persists_and_resumes_next_utc_day(tmp_path, monkeypatch):
    store = SupervisorStore(tmp_path / "state.sqlite")
    store.register("alpha", spec(interval_seconds=1, max_jobs_per_utc_day=1))
    store.enqueue()
    lease = store.claim()
    store.finish(lease, {})
    expire_clock(monkeypatch, 2)
    store.enqueue()
    assert store.claim() is None
    reopened = SupervisorStore(store.path)
    assert reopened.claim() is None
    expire_clock(monkeypatch, 86400)
    assert reopened.claim().sequence == 2


def test_pause_keeps_pending_work_and_resume_reuses_it(tmp_path):
    store = SupervisorStore(tmp_path / "state.sqlite")
    store.register("alpha", spec())
    store.enqueue()
    store.set_active("alpha", False)
    assert store.claim() is None
    store.set_active("alpha", True)
    assert store.claim().sequence == 1


def test_attempts_exhaust_then_next_campaign_iteration(tmp_path, monkeypatch):
    store = SupervisorStore(tmp_path / "state.sqlite")
    store.register("alpha", spec(max_attempts=1, interval_seconds=1))
    store.enqueue()
    lease = store.claim()
    store.finish(lease, None, error="SourceUnavailable")
    assert store.status()["jobs"][0]["state"] == "failed"
    expire_clock(monkeypatch, 2)
    store.enqueue()
    assert store.claim().sequence == 2


def claim_from_process(path, sender):
    lease = SupervisorStore(path).claim()
    sender.send(lease.job_id if lease else None)
    sender.close()


def test_real_processes_cannot_double_claim(tmp_path):
    store = SupervisorStore(tmp_path / "state.sqlite")
    store.register("alpha", spec())
    store.enqueue()
    ctx = multiprocessing.get_context("spawn")
    children = []
    for _ in range(3):
        receiver, sender = ctx.Pipe(duplex=False)
        process = ctx.Process(target=claim_from_process, args=(store.path, sender))
        process.start()
        sender.close()
        children.append((process, receiver))
    claimed = []
    for process, receiver in children:
        assert receiver.poll(10)
        claimed.append(receiver.recv())
        receiver.close()
        process.join(10)
        assert process.exitcode == 0
    assert sum(item is not None for item in claimed) == 1


def successful_job(lease, root):
    return {"job_id": lease.job_id, "sequence": lease.sequence}


def slow_job(lease, root):
    time.sleep(10)


@pytest.mark.parametrize("slow", [False, True])
def test_real_job_completion_or_timeout_is_durable(tmp_path, slow):
    store = SupervisorStore(tmp_path / "state.sqlite")
    store.register("alpha", spec(timeout_seconds=1 if slow else 10, max_attempts=1))
    supervisor = ResearchSupervisor(store, tmp_path / "work", executor=slow_job if slow else successful_job)
    started = time.monotonic()
    output = supervisor.step()
    assert time.monotonic() - started < 8
    assert ("error" in output) is slow
    assert SupervisorStore(store.path).status()["jobs"][0]["state"] == ("failed" if slow else "completed")


def test_stopping_service_spends_attempt_and_retains_job(tmp_path):
    store = SupervisorStore(tmp_path / "state.sqlite")
    store.register("alpha", spec())
    stop = Event()
    stop.set()
    result = ResearchSupervisor(store, tmp_path / "work", executor=slow_job).step(stop)
    assert result["error"] == "ServiceStopping"
    assert store.status()["jobs"][0]["state"] == "pending"


def test_campaign_result_recovery_does_not_call_agent_twice(tmp_path, monkeypatch):
    from honest_alpha_lab.campaign_workers import execute
    from honest_alpha_lab.cli_agents import CliSubagentWorker
    from honest_alpha_lab.subagents import SubagentResult

    invocations = []

    def research(worker, task, prompt, tools):
        invocations.append(task.context)
        return SubagentResult(output={"summary": "A source lead, not an alpha"})

    monkeypatch.setattr(CliSubagentWorker, "run", research)
    store = SupervisorStore(tmp_path / "state.sqlite")
    proposal = CampaignSpec("proposal", {"agent_kind": "alternative_dataset_creator",
        "prompt_name": "semantic-dataset-discovery", "brief": "Find economic proxies"})
    store.register("alpha", proposal)
    store.enqueue()
    lease = store.claim()
    first = execute(lease, tmp_path / "work")
    assert execute(replace(lease, attempt=2), tmp_path / "work") == first
    assert len(invocations) == 1
    assert invocations[0]["input_scope"] == "discovery_context_not_market_data"
    assert invocations[0]["lineage_ids"] == []


def test_second_research_job_receives_first_findings(tmp_path, monkeypatch):
    from honest_alpha_lab.campaign_workers import execute
    from honest_alpha_lab.cli_agents import CliSubagentWorker
    from honest_alpha_lab.subagents import SubagentResult

    contexts = []

    def research(worker, task, prompt, tools):
        contexts.append(task.context)
        return SubagentResult(output={"summary": "Useful prior finding"})

    monkeypatch.setattr(CliSubagentWorker, "run", research)
    store = SupervisorStore(tmp_path / "state.sqlite")
    store.register("alpha", CampaignSpec("proposal", {"agent_kind": "text_event",
        "prompt_name": "text-event-research", "brief": "Find events"}, interval_seconds=1))
    store.enqueue()
    lease = store.claim()
    store.finish(lease, execute(lease, tmp_path / "work"))
    expire_clock(monkeypatch, 2)
    store.enqueue()
    execute(store.claim(), tmp_path / "work")
    assert contexts[1]["campaign_memory"][0]["output"]["summary"] == "Useful prior finding"


def orphan_watchdog_job(lease, root):
    Path(root, "worker.pid").write_text(str(os.getpid()))
    time.sleep(20)


def service_process(path, root):
    ResearchSupervisor(SupervisorStore(path), root, executor=orphan_watchdog_job).step()


@pytest.mark.skipif(os.name != "posix", reason="process-group lifetime is a POSIX integration")
def test_worker_watchdog_survives_supervisor_crash(tmp_path):
    import signal

    store = SupervisorStore(tmp_path / "state.sqlite")
    store.register("alpha", spec(timeout_seconds=2, max_attempts=1))
    root = tmp_path / "work"
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=service_process, args=(store.path, root))
    process.start()
    pid_path = root / "worker.pid"
    deadline = time.monotonic() + 5
    while not pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_path.exists()
    worker_pid = int(pid_path.read_text())
    os.kill(process.pid, signal.SIGKILL)
    process.join(5)
    # No supervising parent remains; the child's own timer must end execution.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(worker_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("worker outlived its deadline after supervisor crash")
    with sqlite3.connect(store.path) as db:
        row = db.execute("SELECT state,attempt FROM jobs").fetchone()
    assert row == ("running", 1)  # State awaits fenced lease recovery, not guessed completion.


def test_example_campaign_configuration_is_valid():
    configuration = json.loads(Path("config/supervisor.example.json").read_text())
    assert len(configuration) == 3
    for payload in configuration.values():
        CampaignSpec(**payload)
