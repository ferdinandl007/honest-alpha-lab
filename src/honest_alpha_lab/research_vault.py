"""An append-only, Obsidian-compatible research memory for alpha discovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .contracts import AlphaCandidate, ContractError, canonical_hash
from .evaluation import EvaluationResult
from .reward import RewardResult
from .subagents import AgentRun, AgentTask
from .validation import ValidationPlan


UTC = timezone.utc


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


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unnamed"


def _write_once(path: Path, contents: str) -> None:
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(contents)
    except FileExistsError as exc:
        raise ContractError(f"research vault artifact is immutable: {path.name}") from exc


@dataclass(frozen=True, slots=True)
class VaultRound:
    round_id: str
    task_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]


class ResearchVault:
    """Stores discovery and evaluation evidence as human-readable linked notes.

    The Markdown works directly in Obsidian. JSON companions make the same immutable
    evidence available to automated task-context construction.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        for name in ("candidates", "datasets", "rounds", "evaluations", "validation-plans"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def shared_context(self, limit: int = 40) -> Mapping[str, object]:
        records = self._candidate_records()[-limit:]
        prior = [
            {
                "candidate_id": record["candidate_id"],
                "name": record["name"],
                "agent_kind": record["agent_kind"],
                "specification": record["specification"],
                "lineage_ids": record["lineage_ids"],
            }
            for record in records
        ]
        evaluations = self._evaluation_records()
        return {
            "vault_path": str(self.root),
            "already_tried": prior,
            "already_evaluated": evaluations[-limit:],
            "diversity_instruction": (
                "Propose a meaningfully different economic mechanism or feature construction. "
                "Do not reissue an already-tried specification; explain the expected distinction."
            ),
        }

    def record_round(
        self,
        round_id: str,
        tasks: Sequence[AgentTask],
        runs: Sequence[AgentRun],
        candidates: Sequence[AlphaCandidate],
    ) -> VaultRound:
        round_path = self.root / "rounds" / f"{_slug(round_id)}.md"
        candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
        lines = [
            "---",
            f"round_id: {round_id}",
            f"recorded_at: {datetime.now(UTC).isoformat()}",
            "---",
            "",
            f"# Research round {round_id}",
            "",
            "## Tasks",
        ]
        lines.extend(
            f"- `{task.task_id}` — {task.job.agent_kind.value}, snapshot `{task.job.input_snapshot_hash}`"
            for task in tasks
        )
        lines.extend(["", "## Runs"])
        lines.extend(
            f"- `{run.run_id}` — {run.status.value}{f': {run.error}' if run.error else ''}"
            for run in runs
        )
        lines.extend(["", "## Proposed candidates"])
        lines.extend(f"- [[candidates/{candidate.candidate_id}]]" for candidate in candidates)
        _write_once(round_path, "\n".join(lines) + "\n")
        for candidate in candidates:
            self._record_candidate(round_id, candidate)
        self._write_index()
        return VaultRound(round_id, tuple(task.task_id for task in tasks), candidate_ids)

    def record_evaluation(
        self, evaluation: EvaluationResult, reward: RewardResult
    ) -> Path:
        if evaluation.sealed:
            raise ContractError("sealed outcomes must not enter research memory")
        if evaluation.candidate_id != reward.candidate_id:
            raise ContractError("evaluation and reward candidate IDs must match")
        record = {
            "evaluation": evaluation,
            "reward": reward,
            "recorded_at": datetime.now(UTC),
        }
        identity = canonical_hash(record)[:16]
        path = self.root / "evaluations" / f"{evaluation.candidate_id}-{identity}.json"
        _write_once(path, json.dumps(_jsonable(record), indent=2, sort_keys=True) + "\n")
        self._write_index()
        return path

    def record_validation_plan(self, plan: ValidationPlan) -> Path:
        """Write a fixed robustness checklist without changing the candidate proposal."""
        if not (self.root / "candidates" / f"{plan.candidate_id}.json").exists():
            raise ContractError("a validation plan needs an existing immutable candidate")
        path = self.root / "validation-plans" / f"{plan.candidate_id}-{plan.plan_id[:16]}.json"
        _write_once(path, json.dumps(_jsonable(plan), indent=2, sort_keys=True) + "\n")
        lines = [
            "---",
            f"plan_id: {plan.plan_id}",
            f"candidate_id: {plan.candidate_id}",
            f"snapshot: {plan.snapshot_hash}",
            f"policy: {plan.policy_hash}",
            "status: not_run",
            "---",
            "",
            f"# Validation plan: [[candidates/{plan.candidate_id}]]",
            "",
        ]
        for check in plan.checks:
            lines.extend([
                f"## {check.category}: {check.check_id}",
                check.requirement,
                "",
                f"**Pass:** {check.pass_criterion}",
                "",
                f"**Evidence:** {check.required_evidence}",
                "",
            ])
        _write_once(path.with_suffix(".md"), "\n".join(lines))
        self._write_index()
        return path

    def _record_candidate(self, round_id: str, candidate: AlphaCandidate) -> None:
        payload = {
            "candidate_id": candidate.candidate_id,
            "agent_kind": candidate.agent_kind.value,
            "name": candidate.name,
            "specification": candidate.specification,
            "input_snapshot_hash": candidate.input_snapshot_hash,
            "lineage_ids": candidate.lineage_ids,
            "round_id": round_id,
            "created_at": candidate.created_at,
        }
        json_path = self.root / "candidates" / f"{candidate.candidate_id}.json"
        _write_once(json_path, json.dumps(_jsonable(payload), indent=2, sort_keys=True) + "\n")
        links = [f"[[rounds/{_slug(round_id)}]]", f"[[agent-kinds/{candidate.agent_kind.value}]]"]
        links.extend(f"[[datasets/{_slug(lineage_id)}]]" for lineage_id in candidate.lineage_ids)
        markdown = [
            "---",
            f"candidate_id: {candidate.candidate_id}",
            f"agent_kind: {candidate.agent_kind.value}",
            f"snapshot: {candidate.input_snapshot_hash}",
            "status: proposed",
            "---",
            "",
            f"# {candidate.name}",
            "",
            "## Research links",
            *[f"- {link}" for link in links],
            "",
            "## Immutable specification",
            "```json",
            json.dumps(_jsonable(candidate.specification), indent=2, sort_keys=True),
            "```",
        ]
        _write_once(
            self.root / "candidates" / f"{candidate.candidate_id}.md",
            "\n".join(markdown) + "\n",
        )
        for lineage_id in candidate.lineage_ids:
            dataset_path = self.root / "datasets" / f"{_slug(lineage_id)}.md"
            if not dataset_path.exists():
                dataset_path.write_text(
                    f"# {lineage_id}\n\nCandidates using this dataset appear through Obsidian backlinks.\n",
                    encoding="utf-8",
                )

    def _candidate_records(self) -> list[Mapping[str, object]]:
        records: list[Mapping[str, object]] = []
        for path in sorted((self.root / "candidates").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, Mapping):
                records.append(data)
        return records

    def _evaluation_records(self) -> list[Mapping[str, object]]:
        records: list[Mapping[str, object]] = []
        for path in sorted((self.root / "evaluations").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, Mapping):
                reward = data.get("reward", {})
                evaluation = data.get("evaluation", {})
                if isinstance(reward, Mapping) and isinstance(evaluation, Mapping):
                    # Unknown or sealed scope is not development feedback. This
                    # also protects reads of legacy/imported vault records.
                    if evaluation.get("sealed") is not False:
                        continue
                    records.append(
                        {
                            "candidate_id": evaluation.get("candidate_id"),
                            "reward": reward.get("reward"),
                            "eligible_for_registry_validation": reward.get(
                                "eligible_for_registry_validation"
                            ),
                            "similarity": evaluation.get("similarity"),
                        }
                    )
        return records

    def _write_index(self) -> None:
        candidates = self._candidate_records()
        evaluations = self._evaluation_records()
        plans = sorted((self.root / "validation-plans").glob("*.json"))
        index = [
            "# Honest Alpha Lab research memory",
            "",
            "This is a derived navigation page. Candidate and evaluation records are immutable.",
            "",
            "## Candidates",
        ]
        index.extend(
            f"- [[candidates/{record['candidate_id']}]] — {record['name']} ({record['agent_kind']})"
            for record in candidates
        )
        index.extend(["", "## Evaluations"])
        index.extend(
            f"- `{record['candidate_id']}` — reward `{record['reward']}`, similarity `{record['similarity']}`"
            for record in evaluations
        )
        index.extend(["", "## Validation plans"])
        index.extend(f"- [[validation-plans/{path.stem}]]" for path in plans)
        (self.root / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
