"""Trusted campaign handlers connecting the supervisor to real research tools."""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .contracts import AgentKind, ContractError, ResearchBudget, ResearchJob, canonical_hash


def _checkpoint(path, payload):
    from .cli_agents import _jsonable
    from .snapshots import _exclusive_write

    _exclusive_write(path, json.dumps(_jsonable(payload), sort_keys=True, allow_nan=False).encode())


def _recent_memory(root):
    paths = sorted(root.glob("*/proposal.json"), key=lambda path: path.stat().st_mtime)[-40:]
    return tuple(json.loads(path.read_text()) for path in paths)


def execute(lease, work_root: Path):
    from .artifacts import LocalArtifactStore
    from .pipeline import NumericalJobService
    from .postgres_store import PostgresStore

    payload = lease.spec.payload
    if lease.spec.kind == "numerical":
        # Numerical handlers have no agent and use only trusted deployment paths.
        return NumericalJobService(PostgresStore(os.environ["HAL_WORKER_DSN"]),
                                   payload["snapshot_root"], payload["artifact_root"]).process_one(payload["run_id"])
    if lease.spec.kind != "proposal":
        raise ContractError("unsupported campaign handler")
    from .cli_agents import CliAgentSpec, CliSubagentWorker, FileTaskStore
    from .pipeline import _plan_from_run, development_feedback, submit_formula
    from .prompts import get_prompt
    from .research_vault import ResearchVault
    from .subagents import AgentTask
    from .tools import ToolRouter

    root = work_root / "jobs"
    directory = root / lease.job_id
    directory.mkdir(parents=True, exist_ok=True)
    _checkpoint(directory / "spec.json", {"campaign_id": lease.campaign_id, "spec": lease.spec})
    completed = directory / "result.json"
    if completed.exists():
        return json.loads(completed.read_text())
    kind = AgentKind(payload["agent_kind"])
    if kind not in {AgentKind.SYMBOLIC_FACTOR, AgentKind.TEXT_EVENT, AgentKind.ALTERNATIVE_DATASET_CREATOR, AgentKind.ALTERNATIVE_DATA}:
        raise ContractError("campaign researcher cannot be a validator")
    template = get_prompt(payload["prompt_name"])
    if template.agent_kind != kind:
        raise ContractError("campaign prompt and agent kind differ")
    proposer, plan = None, None
    if payload.get("numerical_run_id"):
        proposer = PostgresStore(os.environ["HAL_PROPOSER_DSN"])
        plan, _ = _plan_from_run(proposer.get_run(payload["numerical_run_id"]))
    input_path = directory / "input.json"
    if not input_path.exists():
        context = dict(payload.get("context", {}))
        context.update(research_brief=payload["brief"], campaign_sequence=lease.sequence,
                       lineage_ids=payload.get("lineage_ids", []), campaign_memory=_recent_memory(root))
        if payload.get("vault_path"):
            context["shared_research_memory"] = ResearchVault(payload["vault_path"]).shared_context()
        if plan:
            context["locked_numerical_inputs"] = {"fields": plan.fields, "horizon": plan.horizon, "baseline": plan.baseline,
                                                   "development_end": plan.development_end}
            context["measured_development_feedback"] = development_feedback(proposer,
                LocalArtifactStore(payload["artifact_root"]), payload["numerical_run_id"])
        snapshot_hash = plan.snapshot_hash if plan else canonical_hash({"campaign": lease.campaign_id, "payload": payload})
        context["input_scope"] = "numerical_snapshot" if plan else "discovery_context_not_market_data"
        _checkpoint(input_path, {"context": context, "snapshot_hash": snapshot_hash,
                                 "prompt_hash": template.prompt_hash,
                                 "created_at": datetime.now(UTC).isoformat()})
    frozen = json.loads(input_path.read_text())
    if frozen["prompt_hash"] != template.prompt_hash:
        raise ContractError("research prompt changed after the job input was frozen")
    budget = ResearchBudget(max_trials=payload.get("max_trials", 5), max_runtime_seconds=lease.spec.timeout_seconds,
                            max_agent_tokens=payload.get("max_agent_tokens", 100_000))
    job = ResearchJob(lease.job_id, kind, frozen["snapshot_hash"], budget, template.prompt_hash,
                      "always-on-supervisor", datetime.fromisoformat(frozen["created_at"]))
    task = AgentTask(lease.job_id, job, template.name, frozen["context"])
    task_store = FileTaskStore(work_root / "agent-tasks")
    worker = CliSubagentWorker(kind, CliAgentSpec.codex_research(
        timeout_seconds=lease.spec.timeout_seconds, workspace_root=work_root / "research-scratch"), task_store)
    proposal_path = directory / "proposal.json"
    if not proposal_path.exists():
        package_dir = task_store.root / task.task_id
        if package_dir.exists():
            # Recover a completed CLI response without another billable invocation.
            response_path = package_dir / "result.json"
            if not response_path.exists():
                raise ContractError("previous CLI invocation has no recoverable final response; retained as interrupted")
            result = worker._parse_result(task, json.loads(response_path.read_text()), 0)
        else:
            result = worker.run(task, template, ToolRouter())
        candidates = tuple(replace(candidate, candidate_id=str(uuid5(NAMESPACE_URL, f"{lease.job_id}:{index}")),
                                   created_at=job.created_at) for index, candidate in enumerate(result.alpha_candidates))
        _checkpoint(proposal_path, {"job_id": lease.job_id, "campaign_id": lease.campaign_id,
                                    "output": result.output, "candidates": candidates,
                                    "usage": result.usage, "usage_independently_verified": False})
    proposal = json.loads(proposal_path.read_text())
    submissions = []
    for candidate in proposal["candidates"]:
        expression = candidate["specification"].get("dsl")
        if proposer is None or not isinstance(expression, str):
            submissions.append({"candidate_id": candidate["candidate_id"], "status": "data_needed"})
        else:
            reservation = submit_formula(proposer, payload["numerical_run_id"], expression,
                submission_id=f"{lease.job_id}:{candidate['candidate_id']}", metadata={
                    "candidate_id": candidate["candidate_id"], "name": candidate["name"], "agent_kind": kind.value})
            # Do not persist a changing 'created' flag: retry/recovery shares identity.
            submissions.append({"candidate_id": candidate["candidate_id"], "trial_id": str(reservation.trial_id)})
    output = {"proposal": proposal, "submissions": submissions, "scope": "development",
              "financial_alpha_verified": False}
    _checkpoint(completed, output)
    return output
