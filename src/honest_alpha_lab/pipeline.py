"""Persistent proposal -> bounded numerical job -> development feedback.

Only trusted services instantiate this module. Agent output is a constrained DSL
proposal, never a database credential, callable, file path or executable command.
"""
from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .artifacts import LocalArtifactStore
from .contracts import ContractError, canonical_hash
from .dsl import Formula
from .numerical import WalkForwardConfig
from .postgres_store import PostgresStore

FORMAT = "numerical-job-v2"
PRICE_FIELDS = tuple(f"{prefix}total_return_{point}"
                     for prefix in ("", "sector_", "market_") for point in ("open", "close"))


def engine_fingerprint() -> str:
    """Bind plans to this installed source and the numerical runtime versions."""
    directory = Path(__file__).resolve().parent
    modules = ("pipeline.py", "dsl.py", "panel.py", "snapshots.py", "numerical.py",
               "contracts.py", "artifacts.py", "regimes.py", "prediction_artifacts.py")
    try:
        lightgbm_version = version("lightgbm")
    except PackageNotFoundError:
        lightgbm_version = "not-installed"
    return canonical_hash({
        "source": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                   for name in modules},
        "python": sys.version,
        "lightgbm": lightgbm_version,
        "packages": {name: version(name) for name in
                     ("numpy", "pandas", "pyarrow", "duckdb", "scikit-learn", "hmmlearn", "scipy")},
    })


@dataclass(frozen=True)
class NumericalPlan:
    snapshot_hash: str
    development_end: str
    fields: tuple[str, ...]
    baseline: tuple[tuple[str, str], ...] = ()
    horizon: int = 20
    beta_window: int = 252
    max_trial_seconds: int = 300
    allow_correctness_fixture: bool = False
    config: WalkForwardConfig = field(default_factory=WalkForwardConfig)

    def __post_init__(self):
        if not isinstance(self.snapshot_hash, str) or not re.fullmatch("[a-f0-9]{64}", self.snapshot_hash):
            raise ContractError("plan needs the actual content-addressed snapshot hash")
        date.fromisoformat(self.development_end)
        if type(self.allow_correctness_fixture) is not bool:
            raise ContractError("fixture permission must be explicit boolean")
        if type(self.horizon) is not int or self.horizon not in {5, 10, 20, 60}:
            raise ContractError("supported horizons are 5, 10, 20, 60 sessions")
        if type(self.beta_window) is not int or self.beta_window < 2:
            raise ContractError("beta window must be an integer >= 2")
        if type(self.max_trial_seconds) is not int or not 1 <= self.max_trial_seconds <= 86400:
            raise ContractError("bounded per-trial runtime must be 1 to 86400 seconds")
        if not self.fields or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_]\w*", name)
                                  for name in self.fields) or len(set(self.fields)) != len(self.fields):
            raise ContractError("plan must declare unique allowed feature names")
        object.__setattr__(self, "fields", tuple(self.fields))
        normalized = tuple((name, Formula.parse(expression).canonical_expression)
                           for name, expression in self.baseline)
        if len({name for name, _ in normalized}) != len(normalized):
            raise ContractError("baseline identifiers must be unique")
        for name, expression in normalized:
            if not name or not Formula.parse(expression).required_fields <= set(self.fields):
                raise ContractError("baseline uses undeclared inputs")
        object.__setattr__(self, "baseline", tuple(sorted(normalized)))
        if not isinstance(self.config, WalkForwardConfig):
            raise ContractError("plan requires a numerical configuration")

    def payload(self):
        return asdict(self)

    @classmethod
    def from_payload(cls, value):
        from .regimes import RegimeConfig

        value = dict(value)
        configuration = dict(value["config"])
        if "regime_configs" in configuration:
            configuration["regime_configs"] = tuple(RegimeConfig(**item)
                                                     for item in configuration["regime_configs"])
        value["config"] = WalkForwardConfig(**configuration)
        return cls(**value)


def freeze_numerical_run(validator: PostgresStore, run_id: str, plan: NumericalPlan,
                         *, max_trials: int, max_attempts: int = 1,
                         max_runtime_seconds: int = 3600):
    if type(max_runtime_seconds) is not int or max_runtime_seconds <= 0:
        raise ContractError("run runtime must be a positive integer")
    return validator.create_run(
        run_id,
        snapshot={"snapshot_hash": plan.snapshot_hash, "scope": "development"},
        policy={"format": FORMAT, "engine_fingerprint": engine_fingerprint(), "plan": plan.payload()},
        budget={"max_trials": max_trials, "max_attempts": max_attempts,
                "max_runtime_seconds": max_runtime_seconds},
    )


def _plan_from_run(run, *, require_active: bool = True, require_current_engine: bool = True):
    allowed_formats = {FORMAT} if require_current_engine else {FORMAT, "numerical-job-v1"}
    if (not run or run["policy"].get("format") not in allowed_formats
            or run["snapshot"].get("scope") != "development"):
        raise ContractError("not a declared numerical development run")
    plan = NumericalPlan.from_payload(run["policy"]["plan"])
    if plan.snapshot_hash != run["snapshot"].get("snapshot_hash"):
        raise ContractError("plan and snapshot commitment differ")
    if require_current_engine and run["policy"]["engine_fingerprint"] != engine_fingerprint():
        raise ContractError("numerical source/runtime changed after run was frozen")
    runtime = run["budget"].get("max_runtime_seconds")
    if type(runtime) is not int or runtime <= 0:
        raise ContractError("run lacks an enforceable runtime budget")
    created = run["created_at"]
    if not isinstance(created, datetime) or created.utcoffset() is None:
        raise ContractError("run needs a server-supplied creation timestamp")
    remaining = runtime - (datetime.now(UTC) - created).total_seconds()
    if require_active and remaining <= 0:
        raise ContractError("run wall-clock budget exhausted")
    return plan, remaining


def submit_formula(proposer: PostgresStore, run_id: str, expression: str,
                   *, submission_id: str, metadata: Mapping | None = None):
    """Log every submitted attempt; reserve only canonical unique computation.

    submission_id is the caller's stable idempotency key. Replaying it cannot
    change its contents; a genuinely different proposal needs a fresh identifier.
    """
    if not isinstance(submission_id, str) or not submission_id or len(submission_id) > 200:
        raise ContractError("proposal needs a bounded stable submission id")
    plan, _ = _plan_from_run(proposer.get_run(run_id))
    if not isinstance(expression, str) or len(expression) > 8192:
        raise ContractError("proposal must be a bounded DSL string")
    proposer.append_graph_event(run_id, f"submission:{submission_id}", {
        "kind": "proposal_attempt", "expression": expression, "metadata": dict(metadata or {}),
    })
    try:
        formula = Formula.parse(expression)
        if not formula.required_fields <= set(plan.fields):
            raise ContractError("proposal uses fields outside the locked plan")
        specification = {"format": FORMAT, "expression": formula.canonical_expression,
                         "formula_hash": formula.formula_hash}
    except ContractError as exc:
        proposer.append_graph_event(run_id, f"rejected:{submission_id}",
                                    {"kind": "proposal_rejected", "reason": str(exc)})
        raise
    reservation = proposer.reserve_trial(run_id, specification, metadata=dict(metadata or {}))
    # No 'created' flag: exact replay and a new duplicate both link to the same job.
    proposer.append_graph_event(run_id, f"reserved:{submission_id}", {
        "kind": "proposal_trial_link", "trial_id": str(reservation.trial_id),
        "formula_hash": formula.formula_hash,
    })
    return reservation


def _evaluate_child(connection, payload, snapshot_root, artifact_root):
    """Trusted fixed worker target. No arbitrary code is accepted from a proposal."""
    try:
        from .numerical import WalkForwardEvaluator
        from .panel import estimate_incremental_beta, next_open_residual_labels
        from .prediction_artifacts import store_predictions
        from .snapshots import ParquetSnapshot

        plan = NumericalPlan.from_payload(payload["plan"])
        if payload["engine_fingerprint"] != engine_fingerprint():
            raise ContractError("worker runtime differs from frozen run")
        specification = payload["specification"]
        if set(specification) != {"format", "expression", "formula_hash"} or specification["format"] != FORMAT:
            raise ContractError("unrecognized executable proposal contract")
        formula = Formula.parse(specification["expression"])
        if formula.formula_hash != specification["formula_hash"] or not formula.required_fields <= set(plan.fields):
            raise ContractError("proposal identity or allowed fields do not match")
        root = Path(snapshot_root).resolve()
        directory = (root / plan.snapshot_hash).resolve()
        if directory.parent != root:
            raise ContractError("snapshot escaped configured data root")
        snapshot = ParquetSnapshot(directory)
        if snapshot.snapshot_hash != plan.snapshot_hash:
            raise ContractError("resolved snapshot does not match frozen plan")
        purpose = snapshot.manifest["declaration"]["purpose"]
        if purpose == "correctness_fixture" and not plan.allow_correctness_fixture:
            raise ContractError("correctness fixtures cannot be financial benchmarks")
        panel = snapshot.panel(fields=tuple(dict.fromkeys((*plan.fields, *PRICE_FIELDS))),
                               end=date.fromisoformat(plan.development_end))
        labels = next_open_residual_labels(panel, plan.horizon,
                                          estimate_incremental_beta(panel, plan.beta_window))
        report, predictions = WalkForwardEvaluator(plan.config).evaluate(
            formula, panel, labels, snapshot_hash=plan.snapshot_hash,
            baseline={name: Formula.parse(expression) for name, expression in plan.baseline})
        artifacts = LocalArtifactStore(artifact_root)
        handoff = store_predictions(snapshot, panel, predictions, formula.formula_hash, artifacts)
        report_hash = artifacts.put(json.dumps(asdict(report), sort_keys=True,
                                                allow_nan=False).encode())
        connection.send({"status": "evaluated", "scope": "development",
                         "formula_hash": formula.formula_hash, "snapshot_hash": plan.snapshot_hash,
                         "report_artifact_hash": report_hash, **handoff,
                         "snapshot_purpose": purpose, "financial_alpha_verified": False})
    except (ContractError, FileNotFoundError) as exc:
        connection.send({"status": "not_evaluated", "scope": "development",
                         "reason": str(exc), "financial_alpha_verified": False})
    except Exception as exc:  # noqa: BLE001 -- process boundary returns only a redacted failure type
        connection.send({"status": "worker_error", "scope": "development",
                         "error_type": type(exc).__name__})
    finally:
        connection.close()


class NumericalJobService:
    """One bounded queue item per call; external supervisors control concurrency.

    Children inherit the trusted worker's OS environment, not a proposer context.
    Deployment must isolate this worker from sealed data and agent processes.
    Database fencing prevents late workers from publishing a replacement's result.
    """
    def __init__(self, worker: PostgresStore, snapshot_root: str | Path,
                 artifact_root: str | Path, *, lease_seconds: int = 60):
        if type(lease_seconds) is not int or lease_seconds < 3 or lease_seconds > 86400:
            raise ContractError("lease_seconds must be in [3, 86400]")
        self.worker = worker
        self.snapshot_root = Path(snapshot_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self.lease_seconds = lease_seconds

    def process_one(self, run_id: str):
        # Validation before claim prevents workers taking an incompatible run.
        run = self.worker.get_run(run_id)
        plan, remaining = _plan_from_run(run)
        budget_deadline = time.monotonic() + remaining
        self.worker.reclaim(run_id=run_id)
        lease = self.worker.claim(run_id=run_id, lease_seconds=self.lease_seconds)
        if lease is None:
            return None  # not a claim that pending/running work is globally finished
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_evaluate_child, args=(
            sender, {"plan": plan.payload(), "specification": lease.specification,
                     "engine_fingerprint": run["policy"]["engine_fingerprint"]},
            str(self.snapshot_root), str(self.artifact_root)))
        started = time.monotonic()
        timeout = min(plan.max_trial_seconds, budget_deadline - started)
        heartbeat_at = started + self.lease_seconds / 3
        try:
            process.start()
            sender.close()
            response = None
            while True:
                now = time.monotonic()
                if now - started >= timeout:
                    raise TimeoutError("numerical execution exceeded its frozen wall-clock budget")
                if now >= heartbeat_at:
                    self.worker.heartbeat(lease, lease_seconds=self.lease_seconds)
                    heartbeat_at = now + self.lease_seconds / 3
                if receiver.poll(min(.1, max(.001, timeout - (now - started)))):
                    response = receiver.recv()
                    break
                if not process.is_alive():
                    raise RuntimeError("numerical child exited without a result")
            if response.get("scope") != "development" or response.get("status") == "worker_error":
                raise RuntimeError(f"numerical worker failed: {response.get('error_type', 'invalid scope')}")
            response["trial_id"] = str(lease.trial_id)
            response["attempt"] = lease.attempt
            response["runtime_seconds"] = time.monotonic() - started
            self.worker.complete(lease, response)
            return response
        except Exception as exc:
            # SQLSTATE 55000 can indicate loss of ownership. Never revive that lease.
            if getattr(exc, "sqlstate", None) != "55000":
                try:
                    self.worker.fail(lease, {"kind": type(exc).__name__,
                                             "runtime_seconds": time.monotonic() - started})
                except Exception as failure:  # noqa: BLE001 -- preserve the original failure on uncertain DB commit
                    exc.add_note(f"Failure recording was not confirmed: {type(failure).__name__}")
            raise
        finally:
            sender.close()
            receiver.close()
            if process.pid is not None:
                process.join(timeout=1)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5)
                process.close()


def development_feedback(reader: PostgresStore, artifacts: LocalArtifactStore,
                         run_id: str, *, limit: int = 40):
    """Return bounded measured development summaries, never sealed outcomes.

    Not a reward or approval. These diagnostics can guide the next proposal while
    costs, provenance, portfolio and final-validation obligations remain open.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ContractError("feedback limit must be in [1, 1000]")
    run = reader.get_run(run_id)
    plan, _ = _plan_from_run(run, require_active=False, require_current_engine=False)
    summaries = []
    candidates = {}
    trials = {}
    after = 0
    while records := reader.records(run_id, after_sequence=after):
        after = records[-1]["sequence"]
        for record in records:
            envelope = record["envelope"]
            if envelope["kind"] == "candidate_registered":
                candidate = envelope["payload"]
                candidates[candidate["candidate_hash"]] = candidate
            if envelope["kind"] == "trial_reserved":
                trial = envelope["payload"]
                trials[trial["trial_id"]] = trial["candidate_hash"]
            if envelope["kind"] != "trial_completed":
                continue
            result = envelope["payload"]["result"]
            if result.get("scope") != "development":
                continue
            if result.get("trial_id") != envelope["payload"]["trial_id"]:
                raise ContractError("feedback trial identity mismatch")
            candidate = candidates.get(trials.get(result["trial_id"]))
            if candidate is None:
                raise ContractError("feedback lacks its immutable candidate registration")
            summary = {"trial_id": result["trial_id"], "status": result["status"],
                       "expression": candidate["specification"].get("expression"),
                       "candidate_id": candidate["metadata"].get("candidate_id"),
                       "name": candidate["metadata"].get("name"),
                       "financial_alpha_verified": False}
            if result["status"] == "evaluated":
                report = json.loads(artifacts.get(result["report_artifact_hash"]))
                if (report.get("scope") != "development" or report.get("snapshot_hash") != plan.snapshot_hash
                        or report.get("formula_hash") != result.get("formula_hash")
                        or report.get("policy_hash") != canonical_hash(asdict(plan.config))
                        or report.get("horizon") != plan.horizon
                        or result.get("formula_hash") != candidate["specification"].get("formula_hash")
                        or report.get("library_hash") != canonical_hash({
                            name: Formula.parse(expression).formula_hash for name, expression in plan.baseline})):
                    raise ContractError("feedback artifact is not bound to the locked development run")
                for key in ("rank_ic", "incremental_rank_ic", "max_library_similarity", "score_coverage"):
                    value = report[key]
                    if not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise ContractError("feedback contains invalid numerical evidence")
                    summary[key] = value
                summary["snapshot_purpose"] = result["snapshot_purpose"]
                summary["timing_contract"] = report.get("timing_contract", "legacy_unknown")
                summary["label_source_panel_hash"] = report.get("label_source_panel_hash")
                summary["input_provenance"] = report.get("input_provenance", {"status": "legacy_unknown"})
                summary["regime_diagnostics"] = [
                    {key: state.get(key) for key in (
                        "method", "fold_index", "status", "diagnostic", "converged",
                        "conditional_rank_ic", "conditional_baseline_rank_ic",
                        "conditional_augmented_rank_ic", "conditional_incremental_rank_ic")}
                    for state in report.get("regime_reports", [])
                ]
                summary["signals_artifact_hash"] = result.get("signals_artifact_hash")
                summary["strategy_id"] = result.get("strategy_id")
            else:
                summary["reason"] = result.get("reason", "not evaluated")
            summaries.append(summary)
            summaries = summaries[-limit:]
    return tuple(summaries)
