"""Offline budget boundary regressions; no provider or network calls."""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest

from honest_alpha_lab.cli_agents import CliAgentSpec, CliSubagentWorker, FileTaskStore
from honest_alpha_lab.contracts import (
    AgentKind,
    AlphaCandidate,
    AlphaStatus,
    ContractError,
    ResearchBudget,
    ResearchJob,
)
from honest_alpha_lab.ledger import AlphaRegistry
from honest_alpha_lab.orchestration import JobUsage, ResearchQueue
from honest_alpha_lab.prompts import get_prompt
from honest_alpha_lab.subagents import AgentTask, SubagentOrchestrator, SubagentResult
from honest_alpha_lab.tools import ToolCall, ToolName, ToolResult, ToolRouter


def task(**limits):
    prompt = get_prompt("symbolic-factor-research")
    job = ResearchJob("job", AgentKind.SYMBOLIC_FACTOR, "snapshot", ResearchBudget(**limits),
                      prompt.prompt_hash, "test")
    return AgentTask.new(job, prompt.name, {"lineage_ids": ["prices"]})


def router_for(tool, bound=1, budget=1):
    router = ToolRouter()
    item = task(max_data_cost_usd=budget)
    router.register(ToolName.PUBLIC_DOCUMENT_FETCH, tool, max_cost_usd=bound)
    router.register_job(item.job)
    call = ToolCall("run", item.job.job_id, item.job.agent_kind,
                    ToolName.PUBLIC_DOCUMENT_FETCH, {})
    return router, call


class Tool:
    def __init__(self, cost=0, error=False):
        self.calls, self.cost, self.error = 0, cost, error

    def execute(self, arguments):
        self.calls += 1
        if self.error:
            raise RuntimeError("uncertain charge")
        return ToolResult({}, self.cost)


def test_denied_tool_never_executes_even_on_retry():
    tool = Tool(2)
    router, call = router_for(tool, bound=2)
    for _ in range(2):
        with pytest.raises(PermissionError):
            router.execute(call)
    assert tool.calls == 0
    assert router.data_cost("job") == 0


def test_registration_requires_trusted_cost_declaration():
    with pytest.raises(TypeError):
        ToolRouter().register(ToolName.PUBLIC_DOCUMENT_FETCH, Tool())


def test_free_and_exact_limit_calls_and_known_refunds():
    router, call = router_for(Tool(), bound=0, budget=0)
    router.execute(call)
    router, call = router_for(Tool(.25), bound=1)
    router.execute(call)
    assert router.data_cost("job") == .25
    router, call = router_for(Tool(1))
    router.execute(call)
    assert router.data_cost("job") == 1
    with pytest.raises(PermissionError):
        router.execute(call)


@pytest.mark.parametrize("cost,error", [(0, True), (float("nan"), False)])
def test_uncertain_or_invalid_result_keeps_reservation(cost, error):
    tool = Tool(cost, error)
    router, call = router_for(tool)
    with pytest.raises((RuntimeError, ContractError)):
        router.execute(call)
    assert router.data_cost("job") == 1
    with pytest.raises(PermissionError):
        router.execute(call)
    assert tool.calls == 1
    assert router.verify()


def test_bound_violation_records_actual_and_blocks_job():
    router, call = router_for(Tool(2), budget=10)
    with pytest.raises(ContractError, match="maximum"):
        router.execute(call)
    assert router.data_cost("job") == 2
    with pytest.raises(PermissionError, match="blocked"):
        router.execute(call)


def test_audit_failure_retains_reservation(monkeypatch):
    tool = Tool()
    router, call = router_for(tool)
    monkeypatch.setattr(router._traces, "append", lambda _: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        router.execute(call)
    assert router.data_cost("job") == 1
    assert tool.calls == 0


def test_concurrent_call_cannot_spend_outstanding_reservation():
    entered, release = Event(), Event()

    class SlowTool:
        def execute(self, arguments):
            entered.set()
            assert release.wait(5)
            return ToolResult({}, 1)

    router, call = router_for(SlowTool())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(router.execute, call)
        try:
            assert entered.wait(5)
            with pytest.raises(PermissionError):
                pool.submit(router.execute, call).result(5)
        finally:
            release.set()
        first.result(5)
    assert router.data_cost("job") == 1


@pytest.mark.parametrize("value", [True, False, "1", -1, float("nan"), float("inf"), -float("inf")])
def test_numeric_boundaries_reject_invalid_values(value):
    for field in ("max_trials", "max_runtime_seconds", "max_data_cost_usd", "max_agent_tokens"):
        with pytest.raises(ContractError):
            ResearchBudget(**{field: value})
    for field in ("trials", "runtime_seconds", "data_cost_usd", "agent_tokens"):
        with pytest.raises(ContractError):
            JobUsage(**{field: value})
    with pytest.raises(ContractError):
        ToolResult({}, value)
    with pytest.raises(ContractError):
        ToolRouter().register(ToolName.PUBLIC_DOCUMENT_FETCH, Tool(), max_cost_usd=value)


def test_counts_are_integral_but_runtime_can_be_fractional():
    for cls, name in ((ResearchBudget, "max_trials"), (JobUsage, "agent_tokens")):
        with pytest.raises(ContractError):
            cls(**{name: .5})
    assert JobUsage(runtime_seconds=.5).runtime_seconds == .5


def test_queue_rejects_rebound_job_and_decreasing_cumulative_usage():
    item = task(max_trials=2)
    queue = ResearchQueue()
    queue.submit(item.job)
    with pytest.raises(ContractError):
        queue.record_usage(replace(item.job, budget=ResearchBudget()), JobUsage(trials=3))
    queue.record_usage(item.job, JobUsage(trials=1))
    with pytest.raises(ContractError):
        queue.record_usage(item.job, JobUsage())
    with pytest.raises(PermissionError):
        queue.record_usage(item.job, JobUsage(trials=3))
    assert queue.usage("job").trials == 1


def parser(tmp_path):
    return CliSubagentWorker(AgentKind.SYMBOLIC_FACTOR, CliAgentSpec.codex(), FileTaskStore(tmp_path))


def candidate():
    return {"name": "example", "specification": {"dsl": "rank(close)"}, "lineage_ids": ["prices"]}


def test_parser_enforces_actual_count_and_preserves_discovery(tmp_path):
    worker, item = parser(tmp_path), task(max_trials=1)
    with pytest.raises(ContractError, match="count"):
        worker._parse_result(item, {"alpha_candidates": [candidate(), candidate()], "usage": {"trials": 0}}, 0)
    result = worker._parse_result(item, {"alpha_candidates": [candidate()], "usage": {"trials": 0}}, .5)
    assert result.usage.trials == 1
    assert result.usage.runtime_seconds == .5
    assert not worker._parse_result(item, {"alpha_candidates": [], "output": {}}, 0).alpha_candidates


@pytest.mark.parametrize("usage", [
    {"trials": 2}, {"agent_tokens": 11}, {"data_cost_usd": .01},
    {"runtime_seconds": 3}, {"data_cost_usd": float("nan")}, {"trials": True},
])
def test_direct_parser_rejects_bad_or_over_budget_usage(tmp_path, usage):
    with pytest.raises(ContractError):
        parser(tmp_path)._parse_result(task(max_trials=1, max_agent_tokens=10, max_runtime_seconds=2),
                                       {"alpha_candidates": [], "usage": usage}, 0)


def test_orchestrator_prechecks_entire_candidate_batch():
    item, registry = task(max_trials=1), AlphaRegistry()
    orchestrator = SubagentOrchestrator(registry)
    candidates = tuple(AlphaCandidate.new(item.job.agent_kind, str(i), {}, "snapshot", ("prices",)) for i in range(2))
    worker = SimpleNamespace(kind=item.job.agent_kind,
                             run=lambda *args: SubagentResult(candidates, usage=JobUsage()))
    orchestrator.register_worker(worker)
    orchestrator.submit(item)
    assert orchestrator.run_next().status.value == "failed"
    assert not registry.all()


@pytest.mark.parametrize("existing", [False, True])
def test_orchestrator_candidate_id_conflict_cannot_partially_publish(existing):
    item, registry = task(max_trials=2), AlphaRegistry()
    first = AlphaCandidate.new(item.job.agent_kind, "first", {}, "snapshot", ("prices",))
    second = replace(first, candidate_id="existing") if existing else first
    if existing:
        registry.register(second)
    before = registry.all()
    orchestrator = SubagentOrchestrator(registry)
    orchestrator.register_worker(SimpleNamespace(kind=item.job.agent_kind,
        run=lambda *args: SubagentResult((first, second))))
    orchestrator.submit(item)
    assert orchestrator.run_next().status.value == "failed"
    assert registry.all() == before


def test_orchestrator_measures_runtime_even_if_worker_reports_zero(monkeypatch):
    item, registry = task(max_runtime_seconds=1), AlphaRegistry()
    candidate = AlphaCandidate.new(item.job.agent_kind, "first", {}, "snapshot", ("prices",))
    clock = iter((10.0, 12.0))
    monkeypatch.setattr("honest_alpha_lab.subagents.perf_counter", lambda: next(clock))
    orchestrator = SubagentOrchestrator(registry)
    orchestrator.register_worker(SimpleNamespace(kind=item.job.agent_kind,
        run=lambda *args: SubagentResult((candidate,), usage=JobUsage())))
    orchestrator.submit(item)
    assert orchestrator.run_next().status.value == "failed"
    assert not registry.all()


def test_unmetered_cli_requires_explicit_boolean_acknowledgement(tmp_path, monkeypatch):
    monkeypatch.setattr("honest_alpha_lab.cli_agents.subprocess.run", lambda *a, **kw: pytest.fail("launched"))
    worker, item = parser(tmp_path), task()
    with pytest.raises(ContractError, match="allow_unmetered_provider=True"):
        worker.run(item, get_prompt(item.prompt_name), ToolRouter())
    assert not list(tmp_path.iterdir())
    with pytest.raises(ContractError):
        CliAgentSpec.codex(allow_unmetered_provider="true")


def test_orchestrator_rejects_later_prepromoted_candidate_before_publishing_first():
    item, registry = task(max_trials=2), AlphaRegistry()
    orchestrator = SubagentOrchestrator(registry)
    first = AlphaCandidate.new(item.job.agent_kind, "first", {}, "snapshot", ("prices",))
    second = replace(first, candidate_id="second", status=AlphaStatus.ACCEPTED)
    worker = SimpleNamespace(kind=item.job.agent_kind,
                             run=lambda *args: SubagentResult((first, second)))
    orchestrator.register_worker(worker)
    orchestrator.submit(item)
    assert orchestrator.run_next().status.value == "failed"
    assert not registry.all()


@pytest.mark.parametrize("command,extra", [
    ("run-symbolic", ["--prompt-file", "brief", "--input-snapshot-hash", "hash", "--lineage-id", "prices"]),
    ("build-strategies", ["--request", "request.json"]),
])
def test_cli_acknowledgement_is_explicit(command, extra):
    from honest_alpha_lab.cli import _parser
    assert not _parser().parse_args([command, *extra]).allow_unmetered_provider
    assert _parser().parse_args([command, *extra, "--allow-unmetered-provider"]).allow_unmetered_provider


def test_timeout_preserves_measured_runtime_and_unknown_provider_usage(tmp_path, monkeypatch):
    import subprocess
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr("honest_alpha_lab.cli_agents.subprocess.run", fake_run)
    spec = CliAgentSpec.codex(allow_unmetered_provider=True)
    worker = CliSubagentWorker(AgentKind.SYMBOLIC_FACTOR, spec, FileTaskStore(tmp_path))
    item = task(max_runtime_seconds=1)
    with pytest.raises(ContractError, match="timed out after 1s"):
        worker.run(item, get_prompt(item.prompt_name), ToolRouter())
    details = json.loads((tmp_path / item.task_id / "state.json").read_text())["details"]
    assert details["provider_usage_unknown"] is True
    assert details["runtime_seconds"] >= 0


@pytest.mark.parametrize("acknowledged", [False, True])
def test_campaign_passes_trusted_acknowledgement_to_worker(tmp_path, monkeypatch, acknowledged):
    from honest_alpha_lab.campaign_workers import execute
    from honest_alpha_lab.supervisor import CampaignLease, CampaignSpec
    seen = []
    def fake_run(self, item, prompt, tools):
        seen.append(self._spec.allow_unmetered_provider)
        return self._parse_result(item, {"alpha_candidates": []}, 0)
    monkeypatch.setattr(CliSubagentWorker, "run", fake_run)
    spec = CampaignSpec("proposal", {
        "agent_kind": "symbolic_factor", "prompt_name": "symbolic-factor-research",
        "brief": "offline test", "allow_unmetered_provider": acknowledged,
    })
    lease = CampaignLease("job", "campaign", 1, 1, "token", spec)
    execute(lease, tmp_path)
    assert seen == [acknowledged]


def test_success_audit_failure_does_not_refund_known_result(tmp_path, monkeypatch):
    tool = Tool(.1)
    router, call = router_for(tool)
    append = router._traces.append
    def fail_success(trace):
        if trace.event_type == "tool_call":
            raise RuntimeError("audit unavailable")
        return append(trace)
    monkeypatch.setattr(router._traces, "append", fail_success)
    with pytest.raises(RuntimeError):
        router.execute(call)
    assert router.data_cost("job") == 1
    assert tool.calls == 1


def test_separate_job_and_decimal_accounting():
    router, call = router_for(Tool(.1), bound=.1, budget=.3)
    for _ in range(3):
        router.execute(call)
    assert router.data_cost("job") == .3
    with pytest.raises(PermissionError):
        router.execute(call)
    router.register_job(replace(task(max_data_cost_usd=.3).job, job_id="other"))
    router.execute(replace(call, job_id="other"))
    assert router.data_cost("other") == .1


def test_parser_preserves_wire_field_list_and_partial_usage(tmp_path):
    raw = candidate()
    raw["specification"] = [{"key": "dsl", "value": "rank(close)"}]
    result = parser(tmp_path)._parse_result(task(),
        {"alpha_candidates": [raw], "usage": {"agent_tokens": 12}}, .25)
    assert result.alpha_candidates[0].specification == {"dsl": "rank(close)"}
    assert result.usage == JobUsage(trials=1, runtime_seconds=.25, agent_tokens=12)


@pytest.mark.parametrize("job_timeout,spec_timeout", [(2, 10), (10, 2)])
def test_cli_launch_uses_smaller_timeout(tmp_path, monkeypatch, job_timeout, spec_timeout):
    observed = []

    def fake_run(command, **kwargs):
        observed.append(kwargs["timeout"])
        from pathlib import Path
        Path(kwargs["cwd"], "result.json").write_text(json.dumps({"alpha_candidates": []}))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("honest_alpha_lab.cli_agents.subprocess.run", fake_run)
    spec = CliAgentSpec.codex(timeout_seconds=spec_timeout, allow_unmetered_provider=True)
    worker = CliSubagentWorker(AgentKind.SYMBOLIC_FACTOR, spec, FileTaskStore(tmp_path))
    item = task(max_runtime_seconds=job_timeout)
    worker.run(item, get_prompt(item.prompt_name), ToolRouter())
    assert observed == [2]
