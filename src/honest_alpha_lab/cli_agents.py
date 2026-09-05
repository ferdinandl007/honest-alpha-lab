"""File-backed, provider-neutral CLI agent runner.

The runner deliberately makes the filesystem protocol the integration boundary.  A
CLI agent receives an immutable task package and returns a proposal document.
Opt-in researchers can also collect data and write code in a separate scratch
workspace. This protocol is not an OS or filesystem-read isolation boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from time import perf_counter
from typing import Any

from .contracts import (
    AgentKind,
    AlphaCandidate,
    ContractError,
    canonical_hash,
    require_count,
    require_number,
)
from .orchestration import JobUsage, validate_usage
from .prompts import PromptTemplate
from .subagents import AgentTask, SubagentResult
from .tools import ToolRouter


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_jsonable(value), sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _append_jsonl(path: Path, value: object) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_jsonable(value), sort_keys=True) + "\n")


@dataclass(frozen=True, slots=True)
class CliAgentSpec:
    """A non-shell command template for an approved CLI agent executable."""

    name: str
    executable: str
    command_prefix: tuple[str, ...] = ("exec",)
    extra_args: tuple[str, ...] = ()
    timeout_seconds: int = 900
    sandbox: str = "read-only"
    working_directory: str | None = None
    research_mode: bool = False
    research_workspace_root: str | None = None
    allow_unmetered_provider: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.executable or not self.command_prefix:
            raise ContractError("CLI agent needs a name, executable, and command prefix")
        require_number(self.timeout_seconds, "CLI agent timeout", positive=True)
        if type(self.allow_unmetered_provider) is not bool:
            raise ContractError("allow_unmetered_provider must be a boolean")
        if self.sandbox not in {"read-only", "workspace-write"}:
            raise ContractError("CLI agent sandbox must be read-only or workspace-write")
        if self.research_mode and (self.sandbox != "workspace-write" or self.working_directory):
            raise ContractError("research mode requires workspace-write and a dedicated scratch cwd")
        if self.research_workspace_root is not None and not self.research_mode:
            raise ContractError("research_workspace_root requires research mode")
        forbidden = {"--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"}
        if forbidden.intersection(self.extra_args):
            raise ContractError("unsafe Codex bypass flags are forbidden in agent specs")

    @classmethod
    def codex(cls, *, timeout_seconds: int = 900, allow_unmetered_provider: bool = False) -> CliAgentSpec:
        """Safe default for Codex CLI's non-interactive `exec` command."""
        return cls(name="codex", executable="codex", timeout_seconds=timeout_seconds,
                   allow_unmetered_provider=allow_unmetered_provider)

    @classmethod
    def codex_research(
        cls, *, timeout_seconds: int = 900, workspace_root: str | Path | None = None,
        allow_unmetered_provider: bool = False
    ) -> CliAgentSpec:
        """Opt in to network-enabled research in a fresh, retained scratch directory."""
        return cls(
            name="codex", executable="codex", timeout_seconds=timeout_seconds,
            sandbox="workspace-write", research_mode=True,
            research_workspace_root=str(workspace_root) if workspace_root is not None else None,
            allow_unmetered_provider=allow_unmetered_provider,
        )


@dataclass(frozen=True, slots=True)
class CliTaskPackage:
    task_id: str
    directory: Path
    task_path: Path
    prompt_path: Path
    schema_path: Path
    result_path: Path
    events_path: Path
    state_path: Path
    trace_path: Path


class FileTaskStore:
    """Append-only task packages suitable for a local queue or shared object storage."""

    def __init__(self, root: str | Path) -> None:
        # The child process runs from its task directory, so every artifact path
        # supplied on the command line must remain valid after that directory change.
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, task: AgentTask, prompt: str, allowed_tools: Sequence[str]) -> CliTaskPackage:
        if not task.task_id or Path(task.task_id).name != task.task_id or task.task_id in {".", ".."}:
            raise ContractError("task_id must be a single directory name")
        directory = self.root / task.task_id
        if directory.exists():
            raise ContractError(f"task package already exists: {task.task_id}")
        directory.mkdir(parents=True)
        package = CliTaskPackage(
            task.task_id,
            directory,
            directory / "task.json",
            directory / "prompt.md",
            directory / "output.schema.json",
            directory / "result.json",
            directory / "events.jsonl",
            directory / "state.json",
            directory / "trace.jsonl",
        )
        payload = {
            "protocol_version": 1,
            "task_id": task.task_id,
            "job": task.job,
            "prompt_name": task.prompt_name,
            "context": task.context,
            "context_hash": task.context_hash,
            "allowed_tools": tuple(allowed_tools),
        }
        _write_json(package.task_path, payload)
        package.prompt_path.write_text(prompt, encoding="utf-8")
        _write_json(package.schema_path, proposal_output_schema())
        self.set_state(package, "queued", {"task_hash": canonical_hash(payload)})
        return package

    def set_state(self, package: CliTaskPackage, state: str, details: Mapping[str, object] | None = None) -> None:
        record = {
            "task_id": package.task_id,
            "state": state,
            "at": datetime.now(UTC),
            "details": dict(details or {}),
        }
        _write_json(package.state_path, record)
        _append_jsonl(package.trace_path, record)


def proposal_output_schema() -> Mapping[str, object]:
    """The only structured output a CLI proposal agent may produce."""
    candidate = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "specification", "lineage_ids"],
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "specification": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "value"],
                    "properties": {
                        "key": {"type": "string", "minLength": 1},
                        "value": {"type": "string"},
                    },
                },
            },
            "lineage_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["alpha_candidates", "output", "usage"],
        "properties": {
            "alpha_candidates": {"type": "array", "items": candidate},
            "output": {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary", "notes"],
                "properties": {
                    "summary": {"type": "string"},
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
            },
            "usage": {
                "type": "object",
                "additionalProperties": False,
                "required": ["trials", "runtime_seconds", "data_cost_usd", "agent_tokens"],
                "properties": {
                    "trials": {"type": "integer", "minimum": 0},
                    "runtime_seconds": {"type": "number", "minimum": 0},
                    "data_cost_usd": {"type": "number", "minimum": 0},
                    "agent_tokens": {"type": "integer", "minimum": 0},
                },
            },
        },
    }


class CliSubagentWorker:
    """Runs Codex or another compatible CLI against an immutable file task package."""

    def __init__(self, kind: AgentKind, spec: CliAgentSpec, task_store: FileTaskStore) -> None:
        self.kind = kind
        self._spec = spec
        self._task_store = task_store

    def run(self, task: AgentTask, prompt: PromptTemplate, tools: ToolRouter) -> SubagentResult:
        if not self._spec.allow_unmetered_provider:
            raise ContractError(
                "CLI provider token and dollar spending cannot be enforced by this runner. "
                "Trusted configuration must explicitly set allow_unmetered_provider=True "
                "to acknowledge this billing risk; the runtime limit is not a spending cap.")
        task.job.budget.__post_init__()
        timeout = min(self._spec.timeout_seconds, task.job.budget.max_runtime_seconds)
        allowed_tools = tuple(name.value for name in tools.allowed_for(self.kind))
        workspace = self._create_research_workspace() if self._spec.research_mode else None
        rendered = _render_cli_prompt(task, prompt.render(task.context), allowed_tools, workspace)
        package = self._task_store.create(task, rendered, allowed_tools)
        result_path = workspace / "result.json" if workspace else package.result_path
        command = self._command(package, result_path=result_path)
        self._task_store.set_state(package, "running", {
            "command_hash": canonical_hash(command),
            "research_workspace": str(workspace) if workspace else None,
            "allow_unmetered_provider": True,
            "usage_independently_verified": False,
        })
        protected = {path: path.read_bytes() for path in package.directory.iterdir() if path.is_file()}
        started = perf_counter()
        failure_reason = None
        try:
            completed = subprocess.run(
                command,
                cwd=str(workspace) if workspace else self._working_directory(package),
                env=_child_environment(workspace),
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            failure_reason = "timeout"
            raise ContractError(f"CLI agent timed out after {timeout}s") from error
        except OSError as error:
            failure_reason = "launch_error"
            raise ContractError(f"CLI agent could not launch: {error}") from error
        finally:
            # Verify immutable inputs even when the child fails or times out.
            changed = []
            for path, original in protected.items():
                if path.is_symlink() or not path.is_file() or path.read_bytes() != original:
                    changed.append(path.name)
                    if path.is_symlink():
                        path.unlink()
                    if not path.exists() or path.is_file():
                        path.write_bytes(original)
            if workspace:
                changed.extend(path.name for path in package.directory.iterdir() if path not in protected)
            if changed:
                self._task_store.set_state(package, "failed", {
                    "reason": "task_package_modified", "files": changed,
                    "runtime_seconds": perf_counter() - started, "provider_usage_unknown": True})
                raise ContractError("CLI agent modified immutable task-package files")
            if failure_reason:
                self._task_store.set_state(package, "failed", {
                    "reason": failure_reason, "runtime_seconds": perf_counter() - started,
                    "provider_usage_unknown": True})
        runtime = perf_counter() - started
        package.events_path.write_text(completed.stdout, encoding="utf-8")
        (package.directory / "stderr.log").write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            self._task_store.set_state(
                package, "failed", {"exit_code": completed.returncode, "runtime_seconds": runtime,
                                    "provider_usage_unknown": True}
            )
            raise ContractError(f"CLI agent exited with status {completed.returncode}")
        if not result_path.is_file() or result_path.is_symlink():
            self._task_store.set_state(package, "failed", {"reason": "missing_result",
                "runtime_seconds": runtime, "provider_usage_unknown": True})
            raise ContractError("CLI agent did not produce its required result.json")
        try:
            response = json.loads(result_path.read_text(encoding="utf-8"))
            result = self._parse_result(task, response, runtime)
        except (json.JSONDecodeError, TypeError, ValueError, ContractError) as error:
            self._task_store.set_state(package, "failed", {"reason": "invalid_result",
                "runtime_seconds": runtime, "provider_usage_unknown": True})
            raise ContractError(f"CLI agent returned an invalid proposal document: {error}") from error
        if workspace:
            _write_json(package.result_path, response)
        self._task_store.set_state(
            package,
            "completed",
            {"result_hash": result.output_hash, "candidate_count": len(result.alpha_candidates),
             "runtime_seconds": runtime, "usage_independently_verified": False,
             "allow_unmetered_provider": True,
             "research_workspace": str(workspace) if workspace else None},
        )
        return result

    def _working_directory(self, package: CliTaskPackage) -> str:
        return self._spec.working_directory or str(package.directory)

    def _create_research_workspace(self) -> Path:
        root = Path(self._spec.research_workspace_root or tempfile.gettempdir()).resolve()
        if root == self._task_store.root or root.is_relative_to(self._task_store.root):
            raise ContractError("research workspace must be outside the task store")
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="hal-research-", dir=root)).resolve()

    def _command(self, package: CliTaskPackage, *, result_path: Path | None = None) -> list[str]:
        command = [self._spec.executable, *self._spec.command_prefix]
        if self._spec.name == "codex":
            command.extend(
                [
                    "--json",
                    "--output-schema",
                    str(package.schema_path),
                    "--output-last-message",
                    str(result_path or package.result_path),
                    "--sandbox",
                    self._spec.sandbox,
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                ]
            )
        command.extend(self._spec.extra_args)
        if self._spec.name == "codex" and self._spec.research_mode:
            command.extend([
                "-c", "approval_policy=\"never\"",
                "-c", "sandbox_workspace_write.network_access=true",
                "-c", "sandbox_workspace_write.writable_roots=[]",
                "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            ])
        command.append(package.prompt_path.read_text(encoding="utf-8"))
        return command

    def _parse_result(self, task: AgentTask, response: object, runtime: float) -> SubagentResult:
        job = task.job
        require_number(runtime, "measured runtime")
        if not isinstance(response, Mapping):
            raise ContractError("CLI result must be a JSON object")
        raw_candidates = response.get("alpha_candidates", ())
        raw_output = response.get("output", {})
        raw_usage = response.get("usage", {})
        if not isinstance(raw_candidates, list) or not isinstance(raw_output, Mapping) or not isinstance(raw_usage, Mapping):
            raise ContractError("CLI result has invalid top-level fields")
        if len(raw_candidates) > job.budget.max_trials:
            raise ContractError("candidate count exceeds immutable trial budget")
        lineage_ids = _approved_lineage_ids(task.context) if raw_candidates else set()
        candidates = tuple(
            AlphaCandidate.new(
                job.agent_kind,
                _required_string(item, "name"),
                _required_mapping(item, "specification"),
                job.input_snapshot_hash,
                _candidate_lineages(item, lineage_ids),
            )
            for item in raw_candidates
        )
        usage = JobUsage(
            trials=max(len(candidates), _nonnegative_int(raw_usage.get("trials", len(candidates)), "trials")),
            runtime_seconds=max(runtime, _nonnegative_number(raw_usage.get("runtime_seconds", 0.0), "runtime_seconds")),
            data_cost_usd=_nonnegative_number(raw_usage.get("data_cost_usd", 0.0), "data_cost_usd"),
            agent_tokens=_nonnegative_int(raw_usage.get("agent_tokens", 0), "agent_tokens"),
        )
        validate_usage(job, usage)
        return SubagentResult(candidates, dict(raw_output), usage)


def _child_environment(workspace: Path | None) -> dict[str, str]:
    """Inherit runtime essentials and CLI authentication, never service credentials."""
    allowed = {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
        "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP", "CODEX_HOME",
        "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    if workspace:
        temporary = workspace / "tmp"
        temporary.mkdir()
        environment.update({key: str(temporary) for key in ("TMPDIR", "TEMP", "TMP")})
    return environment


def _render_cli_prompt(
    task: AgentTask, rendered_prompt: str, allowed_tools: Sequence[str], workspace: Path | None = None
) -> str:
    capabilities = (
        f"Research mode is enabled. Your writable scratch workspace is `{workspace}`. "
        "Collect permitted public or already licensed sources, write and execute research code, "
        "and explore signal hypotheses beyond the catalog. Research artifacts may use any format. "
        "Keep source evidence, timestamps, code and failures in scratch. Discovery does not require "
        "an approved lineage or an executable formula. Return alpha_candidates=[] for discovery-only "
        "work, with findings and artifact paths in output.summary and output.notes. Only emitted "
        "candidates require task-approved lineage; new sources remain research until admitted. "
        "The structured final response is a handoff envelope, not a restriction on research methods. "
        "Do not accept new paid terms or exceed the task budget. "
        if workspace else "You may propose candidates only. "
    )
    return (
        "# Honest Alpha Lab CLI Task\n\n"
        f"Task package: `{task.task_id}`. The complete task context is reproduced below; "
        "do not inspect, modify, or create task-package files.\n"
        f"Immutable input snapshot: `{task.job.input_snapshot_hash}`.\n"
        f"Allowed logical tools: {', '.join(allowed_tools) or 'none'}.\n\n"
        f"{capabilities}Do not claim validation, access a sealed test, alter task files, "
        "or make registry lifecycle decisions. Your final response must conform to output.schema.json.\n\n"
        "In the wire response, each candidate `specification` is an array of `{key, value}` entries; "
        "include every required field as a separate entry. The platform turns it into an immutable object.\n\n"
        f"{rendered_prompt}\n"
    )


def _approved_lineage_ids(context: Mapping[str, object]) -> set[str]:
    """Extract only task-approved lineage identifiers from its immutable context."""
    identifiers: set[str] = set()
    values = context.get("lineages", context.get("lineage_ids", ()))
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ContractError("CLI task context must carry approved data lineage")
    for value in values:
        if isinstance(value, str):
            identifiers.add(value)
        elif hasattr(value, "dataset_id"):
            identifiers.add(str(value.dataset_id))
        elif isinstance(value, Mapping) and isinstance(value.get("dataset_id"), str):
            identifiers.add(value["dataset_id"])
    if not identifiers:
        raise ContractError("CLI task has no approved data lineage")
    return identifiers


def _candidate_lineages(item: object, allowed: set[str]) -> tuple[str, ...]:
    if not isinstance(item, Mapping):
        raise ContractError("candidate must be an object")
    values = item.get("lineage_ids")
    if not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values):
        raise ContractError("candidate lineage_ids must be a non-empty string list")
    if not set(values).issubset(allowed):
        raise ContractError("candidate returned an unapproved lineage id")
    return tuple(values)


def _required_string(item: object, name: str) -> str:
    if not isinstance(item, Mapping) or not isinstance(item.get(name), str) or not item[name]:
        raise ContractError(f"candidate {name} is required")
    return item[name]


def _required_mapping(item: object, name: str) -> Mapping[str, object]:
    if not isinstance(item, Mapping):
        raise ContractError(f"candidate {name} must be an object")
    value = item.get(name)
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, list) or not value:
        raise ContractError(f"candidate {name} must be an object or non-empty field list")
    normalized: dict[str, object] = {}
    for field in value:
        if (
            not isinstance(field, Mapping)
            or not isinstance(field.get("key"), str)
            or not field["key"]
            or not isinstance(field.get("value"), str)
        ):
            raise ContractError(f"candidate {name} field list is invalid")
        if field["key"] in normalized:
            raise ContractError(f"candidate {name} repeats field {field['key']!r}")
        normalized[field["key"]] = field["value"]
    return normalized


def _nonnegative_int(value: object, name: str) -> int:
    require_count(value, f"usage {name}")
    return value


def _nonnegative_number(value: object, name: str) -> float:
    require_number(value, f"usage {name}")
    return float(value)
