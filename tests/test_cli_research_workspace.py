"""Exercise the research protocol with real local processes, never an LLM."""

import json
import os
import sys
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from honest_alpha_lab.agents import make_job
from honest_alpha_lab.cli_agents import CliAgentSpec, CliSubagentWorker, FileTaskStore
from honest_alpha_lab.contracts import AgentKind, ContractError, InputSnapshot, ResearchBudget
from honest_alpha_lab.prompts import get_prompt
from honest_alpha_lab.subagents import AgentTask
from honest_alpha_lab.tools import ToolRouter

FIXTURE = r'''
import json, os, subprocess, sys, time
from pathlib import Path
from urllib.request import urlopen

mode, url = sys.argv[1:3]
args = sys.argv[3:]
schema = Path(args[args.index("--output-schema") + 1])
result = Path(args[args.index("--output-last-message") + 1])
assert json.loads(schema.read_text())["type"] == "object"
assert "Research mode is enabled" in args[-1]
if mode.startswith("tamper:"):
    (schema.parent / mode.split(":", 1)[1]).write_text("tampered")
if mode == "tamper-timeout":
    (schema.parent / "task.json").write_text("tampered")
    time.sleep(5)
if mode == "exit":
    sys.exit(7)
if mode == "timeout":
    time.sleep(5)
if mode == "missing":
    sys.exit(0)
if mode == "invalid":
    result.write_text("invalid JSON")
    sys.exit(0)
if mode == "symlink":
    result.symlink_to(schema)
    sys.exit(0)
with urlopen(url) as response:
    Path("observations.txt").write_bytes(response.read())
Path("experiment.py").write_text("from pathlib import Path\nprint(sum(map(int, Path('observations.txt').read_text().split())))\n")
answer = subprocess.check_output([sys.executable, "experiment.py"], text=True).strip()
Path("finding.any-format").write_text(answer)
Path("environment.json").write_text(json.dumps(dict(os.environ)))
Path("process.json").write_text(json.dumps({"group": os.getpgrp(), "session": os.getsid(0)}))
candidates = []
if mode in ("candidate", "unapproved"):
    candidates = [{"name": "discovered", "specification": {"custom_research": "anything"},
                   "lineage_ids": ["prices" if mode == "candidate" else "new-source"]}]
result.write_text(json.dumps({"alpha_candidates": candidates,
    "output": {"summary": answer, "notes": ["experiment.py", "finding.any-format"]},
    "usage": {"trials": 1, "runtime_seconds": 0, "data_cost_usd": 0, "agent_tokens": 0}}))
print(json.dumps({"type": "research.completed"}))
'''


@pytest.fixture
def local_source():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"2 3 5")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/observations"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def task(context=None):
    job = make_job(AgentKind.SYMBOLIC_FACTOR,
                   InputSnapshot(("data",), "universe", "code"),
                   ResearchBudget(), "prompt", "research-test")
    return AgentTask.new(job, "symbolic-factor-research", context or {})


def worker(tmp_path, url, mode="discovery", timeout=10):
    spec = replace(
        CliAgentSpec.codex_research(timeout_seconds=timeout, workspace_root=tmp_path / "scratch"),
        executable=sys.executable, command_prefix=("-c", FIXTURE, mode, url),
    )
    return CliSubagentWorker(AgentKind.SYMBOLIC_FACTOR, spec, FileTaskStore(tmp_path / "tasks"))


def run(worker, item):
    return worker.run(item, get_prompt(item.prompt_name), ToolRouter())


def test_public_factory_and_legacy_flags(tmp_path):
    store = FileTaskStore(tmp_path / "tasks")
    package = store.create(task(), "prompt", ())
    legacy = CliAgentSpec.codex(timeout_seconds=42)
    assert not legacy.research_mode
    assert legacy.sandbox == "read-only"
    legacy_command = CliSubagentWorker(AgentKind.SYMBOLIC_FACTOR, legacy, store)._command(package)
    assert "sandbox_workspace_write.network_access=true" not in legacy_command
    research = CliAgentSpec.codex_research(timeout_seconds=42)
    assert research.research_mode and research.timeout_seconds == 42
    command = CliSubagentWorker(AgentKind.SYMBOLIC_FACTOR, research, store)._command(package)
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    for value in ("sandbox_workspace_write.network_access=true", "approval_policy=\"never\"",
                  "sandbox_workspace_write.writable_roots=[]",
                  "sandbox_workspace_write.exclude_slash_tmp=true",
                  "sandbox_workspace_write.exclude_tmpdir_env_var=true"):
        assert value in command


def test_discovery_collects_executes_and_retains_artifacts_without_lineage(tmp_path, local_source, monkeypatch):
    for key in ("HAL_VALIDATOR_DSN", "HAL_WORKER_DSN", "HAL_PROPOSER_DSN",
                "NUMERICAL_API_KEY", "VALIDATOR_TOKEN", "DATABASE_URL", "PGPASSWORD",
                "AWS_SECRET_ACCESS_KEY", "UNEXPECTED_CREDENTIAL"):
        monkeypatch.setenv(key, "must-not-inherit")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only-auth")
    item = task()
    result = run(worker(tmp_path, local_source), item)
    assert result.alpha_candidates == ()
    assert result.output["summary"] == "10"
    package = tmp_path / "tasks" / item.task_id
    state = json.loads((package / "state.json").read_text())
    scratch = Path(state["details"]["research_workspace"])
    assert scratch.parent == tmp_path / "scratch"
    assert not scratch.is_relative_to(package)
    assert (scratch / "finding.any-format").read_text() == "10"
    assert (scratch / "experiment.py").is_file()
    assert not (package / "experiment.py").exists()
    env = json.loads((scratch / "environment.json").read_text())
    assert "must-not-inherit" not in env.values()
    assert env["OPENAI_API_KEY"] == "fixture-only-auth"
    assert Path(env["TMPDIR"]).is_relative_to(scratch)
    process = json.loads((scratch / "process.json").read_text())
    assert process == {"group": os.getpgrp(), "session": os.getsid(0)}
    assert json.loads((package / "result.json").read_text())["output"] == result.output
    assert json.loads((package / "task.json").read_text())["context_hash"] == item.context_hash
    assert state["state"] == "completed"
    prompt = (package / "prompt.md").read_text()
    assert "Research artifacts may use any format" in prompt
    assert "You may propose candidates only" not in prompt
    # A second task gets its own directory and leaves the first one's artifacts intact.
    second = task()
    run(worker(tmp_path, local_source), second)
    other = json.loads((tmp_path / "tasks" / second.task_id / "state.json").read_text())
    assert other["details"]["research_workspace"] != str(scratch)
    assert (scratch / "finding.any-format").read_text() == "10"


@pytest.mark.parametrize("context", [{}, {"lineages": []}, {"lineages": None}])
def test_discovery_does_not_require_lineage_shape(tmp_path, context):
    agent = worker(tmp_path, "unused")
    assert not agent._parse_result(task(context), {"alpha_candidates": [], "output": {}}, 0).alpha_candidates


@pytest.mark.parametrize("mode,context,expected", [
    ("candidate", {}, "no approved data lineage"),
    ("unapproved", {"lineage_ids": ["prices"]}, "unapproved lineage"),
])
def test_candidate_lineage_admission_still_enforced(tmp_path, local_source, mode, context, expected):
    with pytest.raises(ContractError, match=expected):
        run(worker(tmp_path, local_source, mode), task(context))


def test_approved_candidate_keeps_snapshot_and_arbitrary_specification(tmp_path, local_source):
    item = task({"lineage_ids": ["prices"]})
    candidate = run(worker(tmp_path, local_source, "candidate"), item).alpha_candidates[0]
    assert candidate.lineage_ids == ("prices",)
    assert candidate.input_snapshot_hash == item.job.input_snapshot_hash
    assert candidate.specification["custom_research"] == "anything"


@pytest.mark.parametrize("filename", ["task.json", "prompt.md", "output.schema.json", "state.json", "trace.jsonl"])
def test_package_tampering_fails_and_restores_original(tmp_path, local_source, filename):
    item = task()
    with pytest.raises(ContractError, match="modified immutable"):
        run(worker(tmp_path, local_source, "tamper:" + filename), item)
    package = tmp_path / "tasks" / item.task_id
    assert "tampered" not in (package / filename).read_text()
    state = json.loads((package / "state.json").read_text())
    assert state["state"] == "failed"
    assert state["details"]["reason"] == "task_package_modified"
    assert not (package / "result.json").exists()


@pytest.mark.parametrize("mode,reason", [("exit", None), ("timeout", "timeout"),
    ("missing", "missing_result"), ("invalid", "invalid_result"), ("symlink", "missing_result")])
def test_failures_are_recorded_without_admitting_results(tmp_path, local_source, mode, reason):
    item = task()
    with pytest.raises(ContractError):
        run(worker(tmp_path, local_source, mode, timeout=1), item)
    package = tmp_path / "tasks" / item.task_id
    state = json.loads((package / "state.json").read_text())
    assert state["state"] == "failed"
    if reason:
        assert state["details"]["reason"] == reason
    assert not (package / "result.json").exists()


def test_rejects_scratch_inside_task_store_and_conflicting_cwd(tmp_path):
    with pytest.raises(ContractError, match="dedicated scratch"):
        replace(CliAgentSpec.codex_research(), working_directory=str(tmp_path))
    store = FileTaskStore(tmp_path / "tasks")
    spec = CliAgentSpec.codex_research(workspace_root=store.root / "scratch")
    with pytest.raises(ContractError, match="outside the task store"):
        run(CliSubagentWorker(AgentKind.SYMBOLIC_FACTOR, spec, store), task())


def test_timeout_still_checks_immutable_inputs(tmp_path, local_source):
    item = task()
    with pytest.raises(ContractError, match="modified immutable"):
        run(worker(tmp_path, local_source, "tamper-timeout", timeout=1), item)
    package = tmp_path / "tasks" / item.task_id
    assert json.loads((package / "task.json").read_text())["task_id"] == item.task_id
    assert json.loads((package / "state.json").read_text())["details"]["reason"] == "task_package_modified"


def test_task_id_cannot_escape_or_overwrite_package(tmp_path):
    store = FileTaskStore(tmp_path / "tasks")
    item = task()
    with pytest.raises(ContractError, match="single directory"):
        store.create(replace(item, task_id="../escape"), "prompt", ())
    package = store.create(item, "original", ())
    with pytest.raises(ContractError, match="already exists"):
        store.create(item, "replacement", ())
    assert package.prompt_path.read_text() == "original"
