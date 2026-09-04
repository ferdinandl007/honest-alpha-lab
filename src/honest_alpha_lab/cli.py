"""Command-line entry points for bounded, auditable research runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from uuid import uuid4

from .cli_agents import CliAgentSpec, CliSubagentWorker, FileTaskStore
from .contracts import AgentKind, AgentRunStatus, EvaluationPolicy, ResearchBudget, ResearchJob
from .prompts import PromptTemplate
from .research_vault import ResearchVault
from .subagents import AgentRun, AgentTask
from .tools import ToolRouter


def _persistent_numerical(args: argparse.Namespace) -> int:
    """Service commands use environment credentials; never echo their values."""
    import psycopg

    from .artifacts import LocalArtifactStore
    from .pipeline import (
        NumericalJobService,
        NumericalPlan,
        development_feedback,
        freeze_numerical_run,
        submit_formula,
    )
    from .postgres_store import PostgresStore

    variable = {"freeze-run": "HAL_VALIDATOR_DSN", "submit-formula": "HAL_PROPOSER_DSN",
                "work-one": "HAL_WORKER_DSN", "research-feedback": "HAL_PROPOSER_DSN"}[args.command]
    if not os.environ.get(variable):
        raise ValueError(f"configure {variable} for this service; never put credentials in plan files")
    store = PostgresStore(os.environ[variable])
    try:
        if args.command == "freeze-run":
            plan = NumericalPlan.from_payload(json.loads(Path(args.plan).read_text()))
            run = freeze_numerical_run(store, args.run_id, plan, max_trials=args.max_trials,
                                      max_attempts=args.max_attempts,
                                      max_runtime_seconds=args.max_runtime_seconds)
            result = {"run_id": args.run_id, "snapshot_hash": run["snapshot_hash"],
                      "policy_hash": run["policy_hash"], "scope": "development"}
        elif args.command == "submit-formula":
            reservation = submit_formula(store, args.run_id, args.formula, submission_id=args.submission_id)
            result = {**asdict(reservation), "trial_id": str(reservation.trial_id)}
        elif args.command == "work-one":
            result = NumericalJobService(store, args.snapshot_root, args.artifact_root).process_one(args.run_id)
            if result is None:
                result = {"status": "no_claim", "run_complete": "unknown"}
        else:
            result = development_feedback(store, LocalArtifactStore(args.artifact_root), args.run_id)
    except psycopg.Error as exc:
        # Connection strings can be present in driver diagnostics. Keep them out
        # of agent-readable stderr and structured command results.
        print(json.dumps({"error_type": type(exc).__name__, "sqlstate": exc.sqlstate}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


def _import_snapshot(args: argparse.Namespace) -> int:
    from .snapshots import ParquetSnapshot, SnapshotDeclaration

    declaration = SnapshotDeclaration(**json.loads(Path(args.declaration).read_text()))
    snapshot = ParquetSnapshot.create(args.output_root, observations=args.observations,
                                      universe=args.universe, sessions=args.sessions,
                                      declaration=declaration)
    print(json.dumps({"snapshot_directory": str(snapshot.directory),
                      "snapshot_hash": snapshot.snapshot_hash,
                      "content_verified": True, "independently_approved": False}, indent=2))
    return 0


def _build_features(args: argparse.Namespace) -> int:
    from .artifacts import LocalArtifactStore
    from .features import build_from_payload

    build = build_from_payload(json.loads(Path(args.request).read_text()),
                               LocalArtifactStore(args.source_artifacts))
    result = build.publish(LocalArtifactStore(args.output_artifacts))
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


def _collect_document(args: argparse.Namespace) -> int:
    from .artifacts import LocalArtifactStore
    from .collection import CollectionBudget, CollectionPolicy, PublicDocumentCollectionTool

    policy = CollectionPolicy(**json.loads(Path(args.policy).read_text()))
    budget = CollectionBudget(args.budget_store, args.budget_id, policy)
    result = PublicDocumentCollectionTool(budget, LocalArtifactStore(args.artifacts)).execute({"url": args.url})
    print(json.dumps(result.output, sort_keys=True, indent=2, allow_nan=False))
    return 0 if result.output["status"] == "collected" else 1


def _supervisor(args: argparse.Namespace) -> int:
    import signal
    from threading import Event

    from .supervisor import CampaignSpec, ResearchSupervisor, SupervisorStore

    if args.command != "supervise" and not Path(args.state).is_file():
        raise ValueError("supervisor state does not exist")
    store = SupervisorStore(args.state)
    if args.command == "supervisor-status":
        print(json.dumps(store.status(), indent=2, sort_keys=True))
        return 0
    if args.command in {"pause-campaign", "resume-campaign"}:
        store.set_active(args.campaign_id, args.command == "resume-campaign")
        return 0
    configuration = json.loads(Path(args.config).read_text())
    if not isinstance(configuration, dict) or not configuration:
        raise ValueError("supervisor configuration must map campaign ids to specifications")
    for campaign_id, payload in configuration.items():
        store.register(campaign_id, CampaignSpec(**payload))
    stop = Event()
    prior = {name: signal.signal(name, lambda *_: stop.set()) for name in (signal.SIGTERM, signal.SIGINT)}
    try:
        ResearchSupervisor(store, args.work_root).serve(stop)
    finally:
        for name, handler in prior.items():
            signal.signal(name, handler)
    return 0


def _evaluate_formula(args: argparse.Namespace) -> int:
    """Numerical development run; never a registry promotion or sealed test."""
    from .artifacts import LocalArtifactStore
    from .contracts import ContractError
    from .dsl import Formula
    from .numerical import WalkForwardConfig, WalkForwardEvaluator
    from .panel import estimate_incremental_beta, next_open_residual_labels
    from .prediction_artifacts import store_predictions
    from .snapshots import ParquetSnapshot

    snapshot = ParquetSnapshot(args.snapshot)
    purpose = snapshot.manifest["declaration"]["purpose"]
    if purpose == "correctness_fixture" and not args.allow_correctness_fixture:
        raise ContractError("correctness fixtures are not financial benchmarks")
    formula = Formula.parse(args.formula)
    baseline = {f"baseline-{i}": Formula.parse(value) for i, value in enumerate(args.baseline)}
    price_fields = tuple(f"{prefix}total_return_{point}"
                         for prefix in ("", "sector_", "market_") for point in ("open", "close"))
    fields = tuple(dict.fromkeys([*args.field, *price_fields]))
    panel = snapshot.panel(fields=fields, end=date.fromisoformat(args.development_end))
    beta = estimate_incremental_beta(panel, args.beta_window)
    labels = next_open_residual_labels(panel, args.horizon, beta)
    config = WalkForwardConfig(min_train_days=args.train_days, test_days=args.test_days,
                               min_assets=args.min_assets, model=args.model,
                               seed=args.seed, bootstrap_samples=args.bootstrap_samples)
    report, predictions = WalkForwardEvaluator(config).evaluate(
        formula, panel, labels, snapshot_hash=snapshot.snapshot_hash, baseline=baseline)
    store = LocalArtifactStore(args.output_directory)
    handoff = store_predictions(snapshot, panel, predictions, formula.formula_hash, store)
    record = {"report": asdict(report), **handoff,
              "snapshot_purpose": purpose, "formula": args.formula, "baseline": args.baseline,
              "provenance_status": "declared_not_independently_approved",
              "registry_status": "not_promoted", "financial_alpha_verified": False}
    report_hash = store.put((json.dumps(record, sort_keys=True, indent=2, allow_nan=False) + "\n").encode())
    print(json.dumps({"report_artifact": str(store.root / report_hash),
                      "predictions_artifact": str(store.root / handoff["predictions_artifact_hash"]),
                      "signals_artifact": str(store.root / handoff["signals_artifact_hash"]),
                      "strategy_id": handoff["strategy_id"],
                      "scope": report.scope, "snapshot_purpose": purpose,
                      "rank_ic": report.rank_ic, "incremental_rank_ic": report.incremental_rank_ic,
                      "financial_alpha_verified": False}, indent=2))
    return 0


def _backtest_portfolio(args: argparse.Namespace) -> int:
    from .artifacts import LocalArtifactStore
    from .portfolio_workflow import run_portfolio_workflow

    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    store = LocalArtifactStore(args.output_directory)
    report = run_portfolio_workflow(request, artifact_store=store)
    digest = store.put((json.dumps(report, sort_keys=True, indent=2,
                                  allow_nan=False, default=str) + "\n").encode())
    print(json.dumps({"report_artifact": str(store.root / digest),
                      "scope": "development", "financial_alpha_verified": False}, indent=2))
    return 0


def _proposal_json(candidate) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "name": candidate.name,
        "specification": dict(candidate.specification),
        "lineage_ids": list(candidate.lineage_ids),
        "status": candidate.status.value,
    }


def _run_symbolic(args: argparse.Namespace) -> int:
    brief = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not brief:
        raise ValueError("prompt file is empty")
    template = PromptTemplate(
        name="ad-hoc-symbolic-factor",
        agent_kind=AgentKind.SYMBOLIC_FACTOR,
        version="1",
        instructions=brief,
        output_contract=(
            "Use the task output schema. Place the five requested candidate objects in "
            "alpha_candidates; output and usage are protocol metadata."
        ),
    )
    budget = ResearchBudget(
        max_trials=args.max_trials,
        max_runtime_seconds=args.timeout_seconds,
        max_data_cost_usd=0.0,
        max_agent_tokens=args.max_agent_tokens,
    )
    # The CLI runner receives an externally declared immutable snapshot hash. This
    # command deliberately makes no assertion that the referenced data are present
    # or validated locally.
    job = ResearchJob(
        job_id=str(uuid4()),
        agent_kind=AgentKind.SYMBOLIC_FACTOR,
        input_snapshot_hash=args.input_snapshot_hash,
        budget=budget,
        prompt_hash=template.prompt_hash,
        requested_by="honest-alpha-lab-cli",
    )
    task = AgentTask(
        task_id=str(uuid4()),
        job=job,
        prompt_name=template.name,
        context={
            "lineage_ids": tuple(args.lineage_id),
            "research_brief": brief,
            "shared_research_memory": ResearchVault(args.vault).shared_context(),
        },
    )
    extra_args: tuple[str, ...] = ("--model", args.model) if args.model else ()
    worker = CliSubagentWorker(
        AgentKind.SYMBOLIC_FACTOR,
        CliAgentSpec(
            name="codex",
            executable="codex",
            timeout_seconds=args.timeout_seconds,
            extra_args=extra_args,
        ),
        FileTaskStore(args.task_store),
    )
    result = worker.run(task, template, ToolRouter())
    vault = ResearchVault(args.vault)
    vault.record_round(
        args.round_id or f"cli-symbolic-{task.task_id}",
        (task,),
        (
            AgentRun(
                str(uuid4()), task.task_id, AgentRunStatus.COMPLETED, result.output_hash
            ),
        ),
        result.alpha_candidates,
    )
    print(
        json.dumps(
            {
                "task_id": task.task_id,
                "candidate_count": len(result.alpha_candidates),
                "candidates": [_proposal_json(candidate) for candidate in result.alpha_candidates],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="honest-alpha-lab")
    subcommands = parser.add_subparsers(dest="command")
    symbolic = subcommands.add_parser(
        "run-symbolic", help="run one proposal-only symbolic-factor CLI task"
    )
    symbolic.add_argument("--prompt-file", required=True)
    symbolic.add_argument("--input-snapshot-hash", required=True)
    symbolic.add_argument("--lineage-id", action="append", required=True)
    symbolic.add_argument("--task-store", default="var/agent-tasks")
    symbolic.add_argument("--vault", default="research-vault")
    symbolic.add_argument("--round-id")
    symbolic.add_argument("--model")
    symbolic.add_argument("--timeout-seconds", type=int, default=900)
    symbolic.add_argument("--max-trials", type=int, default=5)
    symbolic.add_argument("--max-agent-tokens", type=int, default=100_000)
    symbolic.set_defaults(handler=_run_symbolic)
    ingest = subcommands.add_parser("import-snapshot", help="archive exact Parquet exports and provenance declarations")
    ingest.add_argument("--observations", required=True)
    ingest.add_argument("--universe", required=True)
    ingest.add_argument("--sessions", required=True)
    ingest.add_argument("--declaration", required=True)
    ingest.add_argument("--output-root", default="var/snapshots")
    ingest.set_defaults(handler=_import_snapshot)
    features = subcommands.add_parser("build-features", help="materialize evidenced events/proxies; no automatic approval")
    features.add_argument("--request", required=True)
    features.add_argument("--source-artifacts", required=True)
    features.add_argument("--output-artifacts", required=True)
    features.set_defaults(handler=_build_features)
    collect = subcommands.add_parser("collect-document", help="budgeted public HTTPS discovery; does not approve data")
    collect.add_argument("--url", required=True)
    collect.add_argument("--policy", required=True)
    collect.add_argument("--budget-id", required=True)
    collect.add_argument("--budget-store", required=True)
    collect.add_argument("--artifacts", required=True)
    collect.set_defaults(handler=_collect_document)
    supervisor = subcommands.add_parser("supervise", help="run recurring research campaigns until stopped")
    supervisor.add_argument("--config", required=True)
    supervisor.add_argument("--state", required=True)
    supervisor.add_argument("--work-root", required=True)
    supervisor.set_defaults(handler=_supervisor)
    for name in ("supervisor-status", "pause-campaign", "resume-campaign"):
        operation = subcommands.add_parser(name)
        operation.add_argument("--state", required=True)
        if name != "supervisor-status":
            operation.add_argument("--campaign-id", required=True)
        operation.set_defaults(handler=_supervisor)
    evaluate = subcommands.add_parser("evaluate-formula", help="run numerical walk-forward on a content-verified Parquet snapshot")
    evaluate.add_argument("--snapshot", required=True)
    evaluate.add_argument("--formula", required=True)
    evaluate.add_argument("--field", action="append", required=True)
    evaluate.add_argument("--baseline", action="append", default=[])
    evaluate.add_argument("--development-end", required=True, help="last permitted development session, YYYY-MM-DD")
    evaluate.add_argument("--horizon", type=int, choices=(5, 10, 20, 60), default=20)
    evaluate.add_argument("--beta-window", type=int, default=252)
    evaluate.add_argument("--train-days", type=int, default=252)
    evaluate.add_argument("--test-days", type=int, default=63)
    evaluate.add_argument("--min-assets", type=int, default=20)
    evaluate.add_argument("--model", choices=("ridge", "elastic_net", "lightgbm"), default="ridge")
    evaluate.add_argument("--seed", type=int, default=0)
    evaluate.add_argument("--bootstrap-samples", type=int, default=1000)
    evaluate.add_argument("--output-directory", default="var/numerical-artifacts")
    evaluate.add_argument("--allow-correctness-fixture", action="store_true")
    evaluate.set_defaults(handler=_evaluate_formula)
    portfolio = subcommands.add_parser("backtest-portfolio", help="compare conventional allocation on dated development inputs")
    portfolio.add_argument("--request", required=True)
    portfolio.add_argument("--output-directory", default="var/portfolio-artifacts")
    portfolio.set_defaults(handler=_backtest_portfolio)
    freeze = subcommands.add_parser("freeze-run", help="validator service: lock a persistent numerical run")
    freeze.add_argument("--run-id", required=True)
    freeze.add_argument("--plan", required=True)
    freeze.add_argument("--max-trials", type=int, required=True)
    freeze.add_argument("--max-attempts", type=int, default=1)
    freeze.add_argument("--max-runtime-seconds", type=int, default=3600)
    freeze.set_defaults(handler=_persistent_numerical)
    submit = subcommands.add_parser("submit-formula", help="proposer service: log and reserve a canonical formula")
    submit.add_argument("--run-id", required=True)
    submit.add_argument("--formula", required=True)
    submit.add_argument("--submission-id", required=True)
    submit.set_defaults(handler=_persistent_numerical)
    worker = subcommands.add_parser("work-one", help="numerical service: execute one leased job with bounded runtime")
    worker.add_argument("--run-id", required=True)
    worker.add_argument("--snapshot-root", required=True)
    worker.add_argument("--artifact-root", default="var/numerical-artifacts")
    worker.set_defaults(handler=_persistent_numerical)
    feedback = subcommands.add_parser("research-feedback", help="read measured development feedback, excluding sealed results")
    feedback.add_argument("--run-id", required=True)
    feedback.add_argument("--artifact-root", default="var/numerical-artifacts")
    feedback.set_defaults(handler=_persistent_numerical)
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.command is None:
        policy = EvaluationPolicy()
        print(
            f"honest-alpha-lab ready: horizons={policy.horizons}, universe={policy.universe_id}"
        )
        return
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
