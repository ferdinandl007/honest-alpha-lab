"""Trusted strategy compilation and operational proposal-agent/backtest bridge.

``build_strategy_request(proposal, locked_context, output_directory)`` accepts
{blueprints: [...]} or the CLI's {summary: str, notes: [JSON blueprint, ...]}.
It returns StrategyBuild with typed blueprints and a portfolio workflow request.
``run_strategy_agent`` invokes CliSubagentWorker, compiles and backtests.

Locked context payload: portfolio_request (bars_csv, scope, date boundaries,
execution_policy, optional initial_cash/allocation_policy), bars_sha256,
signal_catalog ({ID: {csv_path, sha256}}), allowed_horizons, allowed_sides,
allowed_rebalance_days, signal_lag_days, max_staleness_days. All are required.
Signal CSV: signal_id OR strategy_id, asset, score, available_at, snapshot_hash.
Optional session is validated against availability. Naive clocks, duplicates,
missing/nonfinite values and insufficient as-of coverage fail explicitly.

Lag and staleness count calendar days; rebalancing counts engine sessions.
Every required asset/component at each decision must exist: this strict missing
policy prevents the engine carrying an old composite after a component expires.
No broker or order-routing capability is provided by this module.
Execution is daily OHLC next-bar limits only, not intraday/close-to-open timing.
As in the existing daily engine, horizon is a signal-age limit, not a forced exit.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from .contracts import AgentKind, ContractError, ResearchBudget, ResearchJob, canonical_hash
from .portfolio import (
    ExecutionPolicy,
    HistoricalDataset,
    MarketBar,
    PointInTimeSignal,
    PortfolioBacktester,
)
from .portfolio_workflow import _rows, _timestamp, run_portfolio_workflow
from .strategies import StrategyDefinition, StrategyFamily, StrategySide


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


@dataclass(frozen=True)
class StrategyBlueprint:
    strategy_id: str
    name: str
    signal_ids: tuple[str, ...]
    combination: str
    horizon_days: int
    side: str
    rebalance_days: int
    execution: str
    rationale: str

    def __post_init__(self):
        if not isinstance(self.strategy_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", self.strategy_id):
            raise ContractError("strategy_id must be a safe nonempty identifier")
        if any(not isinstance(s, str) or not s.strip() for s in (self.name, self.rationale)):
            raise ContractError("blueprint needs a name and rationale")
        if (type(self.signal_ids) is not tuple or not self.signal_ids
                or any(not isinstance(s, str) or not s for s in self.signal_ids)
                or len(set(self.signal_ids)) != len(self.signal_ids)):
            raise ContractError("blueprint signal_ids must be unique nonempty identifiers")
        if self.combination not in ("linear_equal", "rank_equal"):
            raise ContractError("only deterministic equal signal combinations are supported")
        if self.execution != "next_bar_limit" or self.side not in {s.value for s in StrategySide}:
            raise ContractError("unsupported sleeve side or execution")
        if any(type(n) is not int or n < 1 for n in (self.horizon_days, self.rebalance_days)):
            raise ContractError("horizon and rebalance days must be positive integers")

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, Mapping) or set(payload) != set(cls.__dataclass_fields__):
            raise ContractError("blueprint has missing or unknown fields; policy/weights cannot be proposed")
        fields = dict(payload)
        if not isinstance(fields["signal_ids"], (tuple, list)):
            raise ContractError("signal_ids must be an array")
        fields["signal_ids"] = tuple(sorted(fields["signal_ids"]))
        return cls(**fields)

    @property
    def coefficients(self):
        return tuple(1.0 / len(self.signal_ids) for _ in self.signal_ids)

    def definition(self):
        return StrategyDefinition(
            self.strategy_id, self.name, StrategyFamily.SYMBOLIC,
            f"Daily OHLC next-bar limit; {self.combination}: {', '.join(self.signal_ids)}. {self.rationale}",
            self.signal_ids, (self.horizon_days,), StrategySide(self.side), self.rebalance_days,
            self.side != "long_only")


@dataclass(frozen=True)
class LockedStrategyContext:
    """Canonical JSON freezes nested policy and catalog mappings by value."""

    payload_json: str

    @classmethod
    def from_payload(cls, payload):
        required = {"portfolio_request", "bars_sha256", "signal_catalog", "allowed_horizons",
                    "allowed_sides", "allowed_rebalance_days", "signal_lag_days", "max_staleness_days"}
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise ContractError("locked context has missing or unknown fields")
        request = payload["portfolio_request"]
        required_request = {"bars_csv", "scope", "development_start", "development_end",
                            "test_start", "test_end", "execution_policy"}
        if (not isinstance(request, Mapping) or required_request - request.keys()
                or request.keys() - required_request - {"initial_cash", "allocation_policy"}):
            raise ContractError("locked portfolio_request cannot override generated signals or training")
        if request["scope"] != "development":
            raise ContractError("strategy building requires development scope")
        ds, de, ts, te = (date.fromisoformat(request[k]) for k in
                          ("development_start", "development_end", "test_start", "test_end"))
        if not ds < de < ts < te:
            raise ContractError("locked development and test dates must be ordered and disjoint")
        if not {"commission_bps", "slippage_bps", "annual_borrow_rate"} <= request["execution_policy"].keys():
            raise ContractError("locked execution must explicitly declare costs")
        ExecutionPolicy(**request["execution_policy"])
        for key in ("allowed_horizons", "allowed_rebalance_days"):
            if (not isinstance(payload[key], (list, tuple)) or not payload[key]
                    or any(type(n) is not int or n < 1 for n in payload[key])):
                raise ContractError(f"{key} must contain positive integers")
        if (not isinstance(payload["allowed_sides"], (list, tuple)) or not payload["allowed_sides"]
                or any(s not in {side.value for side in StrategySide} for s in payload["allowed_sides"])):
            raise ContractError("invalid locked sides")
        for key in ("signal_lag_days", "max_staleness_days"):
            if type(payload[key]) is not int or payload[key] < (1 if key == "max_staleness_days" else 0):
                raise ContractError("lag must be nonnegative and staleness positive")
        catalog = payload["signal_catalog"]
        if not isinstance(catalog, Mapping) or not catalog:
            raise ContractError("signal_catalog must contain actual CSV references")
        for name, source in catalog.items():
            if (not isinstance(name, str) or not name or not isinstance(source, Mapping)
                    or set(source) != {"csv_path", "sha256"}):
                raise ContractError("catalog entries require identifier, csv_path and sha256")
            if not isinstance(source["csv_path"], str) or not source["csv_path"]:
                raise ContractError("catalog CSV paths must be nonempty strings")
        if any(not isinstance(h, str) or not re.fullmatch(r"[a-f0-9]{64}", h)
               for h in [payload["bars_sha256"], *(s["sha256"] for s in catalog.values())]):
            raise ContractError("locked inputs require SHA256 byte hashes")
        return cls(_json(payload))

    @property
    def payload(self):
        return json.loads(self.payload_json)

    @property
    def policy_hash(self):
        return canonical_hash(self.payload)


@dataclass(frozen=True)
class StrategyBuild:
    blueprints: tuple[StrategyBlueprint, ...]
    request_json: str
    policy_hash: str
    build_hash: str

    @property
    def request(self):
        return json.loads(self.request_json)

    def run_backtest(self, artifact_store=None):
        return run_portfolio_workflow(self.request, artifact_store=artifact_store)


def _read_locked(path, digest):
    content = Path(path).read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ContractError("locked input bytes changed")
    return content


def _csv(columns, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(columns.split(","))
    writer.writerows(rows)
    return stream.getvalue().encode()


def _publish(path, content):
    # Content-addressed names and exclusive creation never overwrite user inputs.
    try:
        with path.open("xb") as handle:
            handle.write(content)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != content:
            raise ContractError("strategy artifact path contains different bytes")


def _parse_proposal(proposal):
    if not isinstance(proposal, Mapping):
        raise ContractError("strategy proposal must be an object")
    if set(proposal) == {"blueprints"}:
        values = proposal["blueprints"]
    elif set(proposal) == {"summary", "notes"}:
        if not isinstance(proposal["summary"], str) or not isinstance(proposal["notes"], list):
            raise ContractError("invalid CLI strategy findings")
        try:
            values = [json.loads(note) for note in proposal["notes"]]
        except (ValueError, TypeError) as exc:
            raise ContractError("each strategy note must be one JSON blueprint") from exc
    else:
        raise ContractError("strategy proposal cannot override locked policy")
    if not isinstance(values, (tuple, list)) or not values:
        raise ContractError("proposal needs at least one blueprint")
    blueprints = tuple(StrategyBlueprint.from_payload(value) for value in values)
    if len({b.strategy_id for b in blueprints}) != len(blueprints):
        raise ContractError("duplicate strategy identifiers")
    return tuple(sorted(blueprints, key=lambda b: b.strategy_id))


def build_strategy_request(proposal, locked_context, output_directory):
    """Compile agent selections under fixed policy; no agent-supplied code/weights."""
    try:
        return _build_strategy_request(proposal, locked_context, output_directory)
    except ContractError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ContractError(f"invalid strategy build input: {exc}") from exc


def _build_strategy_request(proposal, locked_context, output_directory):
    context = (locked_context if isinstance(locked_context, LockedStrategyContext)
               else LockedStrategyContext.from_payload(locked_context))
    # Validate even directly constructed contexts.
    context = LockedStrategyContext.from_payload(context.payload)
    locked = context.payload
    blueprints = _parse_proposal(proposal)
    catalog = locked["signal_catalog"]
    for b in blueprints:
        if set(b.signal_ids) - catalog.keys():
            raise ContractError("blueprint references unknown signal identifiers")
        if (b.horizon_days not in locked["allowed_horizons"] or b.side not in locked["allowed_sides"]
                or b.rebalance_days not in locked["allowed_rebalance_days"]):
            raise ContractError("blueprint sleeve rules violate locked policy")
    request = locked["portfolio_request"]
    bars_blob = _read_locked(request["bars_csv"], locked["bars_sha256"])
    bars, seen = [], set()
    for row in _rows(bars_blob, ("day", "asset", "open", "high", "low", "close", "dollar_volume", "borrow_available")):
        day = date.fromisoformat(row["day"])
        if (day, row["asset"]) in seen or row["borrow_available"].lower() not in ("true", "false"):
            raise ContractError("duplicate bars or invalid borrow flag")
        seen.add((day, row["asset"]))
        bars.append(MarketBar(day, row["asset"], *(float(row[k]) for k in
                              ("open", "high", "low", "close", "dollar_volume")),
                              row["borrow_available"].lower() == "true"))
    ds, de, ts, te = (date.fromisoformat(request[k]) for k in
                      ("development_start", "development_end", "test_start", "test_end"))
    intervals = [tuple(bar for bar in bars if start <= bar.day <= end)
                 for start, end in ((ds, de), (ts, te))]
    if any(len({bar.day for bar in interval}) < 2 for interval in intervals):
        raise ContractError("development and test each need at least two actual bar dates")
    selected_ids = sorted({s for b in blueprints for s in b.signal_ids})
    observations = {}
    for name in selected_ids:
        source = catalog[name]
        blob = _read_locked(source["csv_path"], source["sha256"])
        rows = _rows(blob, ("asset", "score", "available_at", "snapshot_hash"))
        id_column = "signal_id" if "signal_id" in rows[0] else "strategy_id"
        keys = set()
        for row in rows:
            if row.get(id_column) != name:
                continue
            stamp = _timestamp(row["available_at"])
            if row.get("session") and date.fromisoformat(row["session"]) > stamp.date():
                raise ContractError("signal session cannot follow availability")
            score = float(row["score"])
            if not math.isfinite(score) or (row["asset"], stamp) in keys:
                raise ContractError("signal scores must be finite with unique asset/timestamp keys")
            keys.add((row["asset"], stamp))
            observations.setdefault((name, row["asset"]), []).append((stamp, score, row["snapshot_hash"]))
        if not keys:
            raise ContractError(f"catalog signal {name} has no dated CSV observations")
    for values in observations.values():
        values.sort()
    compiled = [[], []]
    for interval_index, interval in enumerate(intervals):
        days = sorted({bar.day for bar in interval})
        for day_index, day in enumerate(days[:-1]):
            cutoff = datetime.combine(day, time(), UTC) - timedelta(days=locked["signal_lag_days"])
            output_stamp = datetime.combine(day, time(), UTC) - timedelta(microseconds=1)
            for blueprint in blueprints:
                if day_index % blueprint.rebalance_days:
                    continue
                # Define the ranking universe from observations already known at
                # cutoff, never from future/current execution bar availability.
                assets = sorted({asset for (name, asset), series in observations.items()
                                 if name in blueprint.signal_ids and series[0][0] < cutoff})
                if not assets:
                    raise ContractError(f"missing/stale signal universe at {day}; missing_policy=error")
                columns, lineage = [], []
                for signal_id in blueprint.signal_ids:
                    values = []
                    for asset in assets:
                        series = observations.get((signal_id, asset), [])
                        index = bisect_left([item[0] for item in series], cutoff) - 1
                        if index < 0 or (day - series[index][0].date()).days > locked["max_staleness_days"]:
                            raise ContractError(f"missing/stale signal {signal_id} for {asset} at {day}; missing_policy=error")
                        stamp, score, snapshot = series[index]
                        values.append(score)
                        lineage.append((signal_id, asset, stamp.isoformat(), snapshot))
                    if blueprint.combination == "rank_equal":
                        # Average ties, centered [-1,1], deterministic at this date.
                        ordered = sorted(values)
                        values = [0.0 if len(values) == 1 else
                                  2 * (ordered.index(v) + ordered.count(v) / 2 - .5) / (len(values) - 1) - 1
                                  for v in values]
                    columns.append(values)
                snapshot = canonical_hash({"policy": context.policy_hash, "sources": lineage,
                                           "blueprint": asdict(blueprint)})
                for i, asset in enumerate(assets):
                    score = math.fsum(column[i] * coefficient for column, coefficient in
                                      zip(columns, blueprint.coefficients, strict=True))
                    if not math.isfinite(score):
                        raise ContractError("combined signal score is not finite")
                    compiled[interval_index].append((blueprint.strategy_id, asset, score,
                                                    output_stamp.isoformat(), snapshot))
    execution = ExecutionPolicy(**request["execution_policy"])
    initial_cash = float(request.get("initial_cash", 1_000_000))
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ContractError("initial cash must be positive and finite")
    training_rows = []
    for blueprint in blueprints:
        signals = tuple(PointInTimeSignal(name, asset, score, _timestamp(stamp), context.policy_hash)
                        for name, asset, score, stamp, _ in compiled[0] if name == blueprint.strategy_id)
        dataset = HistoricalDataset(context.policy_hash, False, intervals[0], signals, allow_unverified=True)
        result = PortfolioBacktester({blueprint.strategy_id: blueprint.definition()}, execution).run(
            dataset, {blueprint.strategy_id: 1.0}, initial_cash, allow_unverified=True)
        previous = initial_cash
        for day, nav in result.nav_by_day:
            if nav <= 0:
                raise ContractError("nonpositive development NAV")
            availability = datetime.combine(day, time.max, UTC)
            training_rows.append((day.isoformat(), blueprint.strategy_id, nav / previous - 1,
                                  availability.isoformat()))
            previous = nav
    signals_blob = _csv("strategy_id,asset,score,available_at,snapshot_hash", compiled[1])
    training_blob = _csv("day,strategy_id,return,available_at", training_rows)
    blobs = {"bars": bars_blob, "signals": signals_blob, "training_returns": training_blob}
    hashes = {name: hashlib.sha256(blob).hexdigest() for name, blob in blobs.items()}
    directory = Path(output_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    for name, blob in blobs.items():
        path = directory / f"{name}-{hashes[name]}.csv"
        _publish(path, blob)
        request[f"{name}_csv"] = str(path)
    definitions = {}
    for b in blueprints:
        fields = asdict(b.definition())
        fields["family"], fields["side"] = fields["family"].value, fields["side"].value
        definitions[b.strategy_id] = fields
    request.update(strategies=definitions, strategy_ids=[b.strategy_id for b in blueprints],
                   input_hashes=hashes, signal_handoff_hashes={"strategy_policy": context.policy_hash})
    build_hash = canonical_hash({"policy": context.policy_hash, "blueprints": blueprints, "inputs": hashes})
    return StrategyBuild(blueprints, _json(request), context.policy_hash, build_hash)


def run_strategy_agent(locked_context, output_directory, *, worker=None, budget=None):
    """Invoke the CLI proposal worker, validate typed findings, then run backtests.

    An injected worker is a trusted deployment dependency, not proposal data.
    Default Codex execution is read-only with no registered tools or broker API.
    """
    from .cli_agents import CliAgentSpec, CliSubagentWorker, FileTaskStore
    from .prompts import STRATEGY_BUILDER_PROMPT
    from .subagents import AgentTask
    from .tools import ToolPolicy, ToolRouter

    context = (locked_context if isinstance(locked_context, LockedStrategyContext)
               else LockedStrategyContext.from_payload(locked_context))
    context = LockedStrategyContext.from_payload(context.payload)
    budget = budget or ResearchBudget()
    if worker is None:
        worker = CliSubagentWorker(AgentKind.STRATEGY_BUILDER,
                                  CliAgentSpec.codex(timeout_seconds=budget.max_runtime_seconds),
                                  FileTaskStore(Path(output_directory) / "agent-tasks"))
    if worker.kind != AgentKind.STRATEGY_BUILDER:
        raise ContractError("strategy agent worker kind must match")

    class NoStrategyTools(ToolPolicy):
        _ALLOWED: ClassVar = {AgentKind.STRATEGY_BUILDER: frozenset()}

    public_context = context.payload
    # Catalog identifiers and policy only; proposal generation need not read data.
    public_context["signal_catalog"] = sorted(public_context["signal_catalog"])
    public_context["portfolio_request"].pop("bars_csv")
    job = ResearchJob(str(uuid4()), AgentKind.STRATEGY_BUILDER, context.policy_hash, budget,
                      STRATEGY_BUILDER_PROMPT.prompt_hash, "strategy-builder")
    task = AgentTask.new(job, STRATEGY_BUILDER_PROMPT.name, public_context)
    result = worker.run(task, STRATEGY_BUILDER_PROMPT, ToolRouter(NoStrategyTools()))
    if result.alpha_candidates:
        raise ContractError("strategy agent must return blueprints as findings, not alpha candidates")
    if (result.usage.trials > budget.max_trials or result.usage.runtime_seconds > budget.max_runtime_seconds
            or result.usage.agent_tokens > budget.max_agent_tokens
            or result.usage.data_cost_usd > budget.max_data_cost_usd):
        raise ContractError("strategy proposal usage exceeds locked budget")
    if len(_parse_proposal(result.output)) > budget.max_trials:
        raise ContractError("strategy blueprint count exceeds trial budget")
    build = build_strategy_request(result.output, context, output_directory)
    report = build.run_backtest()
    return {"status": "research_only", "scope": "development", "orders_authorized": False,
            "execution_scope": "daily_ohlc_next_bar_limit",
            "build_hash": build.build_hash, "policy_hash": build.policy_hash,
            "blueprints": [asdict(b) for b in build.blueprints],
            "request": build.request, "backtest": report,
            "agent_output_hash": result.output_hash, "usage": asdict(result.usage),
            "usage_independently_verified": False}
