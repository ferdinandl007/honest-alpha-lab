"""Persistent paper-only daily books; no broker adapter or background service.

API: create_book(db_path, book_id, config), ingest_book(db_path, book_id, event),
book_status(db_path, book_id), run_paper_trading(request).
Runner requests contain action=create|ingest|status, db_path, book_id, and
config or event as appropriate. All responses are JSON-compatible.

Config requires mode=historical_replay|forward_shadow, strategies (mapping of
StrategyDefinition fields by ID), allocation (fixed weights), initial_cash,
execution_policy (explicit commission_bps, slippage_bps, annual_borrow_rate).
Forward mode additionally requires max_signal_age_seconds, max_bar_delay_seconds.

Signal event: {event_id, kind:"signals", signals:[{strategy_id, asset, score,
available_at, snapshot_hash}]}. Bar event: {event_id, kind:"bars", window_start,
window_end, bars:[{day, asset, open, high, low, close, dollar_volume,
borrow_available}]}. A bar event is a complete batch for one UTC trading date;
window_start must be UTC midnight, window_end the following UTC midnight.
Supply actual bars only. No amendments, partial daily batches, or mixed events.

Forward signals must be received on their UTC availability date, after book
creation, before the next bar window opens. Completed bars must begin after book
creation and arrive within max_bar_delay_seconds of window_end. Historical mode
does not assert actual precommitment; it enforces logical signal/bar chronology.
Missing calendar days are allowed (weekends/holidays); the caller owns the session
calendar. Engine limits activate on the next observed bar after a decision.

Each successful daily transaction stores the complete deterministic replay and
new fills. Inputs and daily outputs cannot be updated/deleted through this API;
SQLite triggers also prohibit modification. Full history is replayed per day,
so runtime grows with book history. Engine source hashes are pinned at creation;
changed engine versions require a new book. Local SQLite is not a tamper-proof
external audit ledger. UTC timestamps/lineage do not independently verify alpha.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from functools import wraps
from math import isfinite
from pathlib import Path

from .contracts import ContractError
from .portfolio import (
    ExecutionPolicy,
    HistoricalDataset,
    MarketBar,
    PointInTimeSignal,
    PortfolioBacktester,
)
from .strategies import StrategyDefinition, StrategyFamily, StrategySide


def _utc_now():
    return datetime.now(UTC)


def _stamp(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ContractError("timestamps must be UTC-aware")
    return result.astimezone(UTC)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      default=lambda item: item.isoformat())


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _engine():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("paper_trading.py", "portfolio.py", "strategies.py", "contracts.py")}


def _keys(value, required, optional=()):
    if not isinstance(value, Mapping) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ContractError(f"expected fields {sorted(required)}; optional {sorted(optional)}")


def _config(config):
    _keys(config, ("mode", "strategies", "allocation", "initial_cash", "execution_policy"),
          ("max_signal_age_seconds", "max_bar_delay_seconds"))
    if config["mode"] not in {"historical_replay", "forward_shadow"}:
        raise ContractError("mode must be historical_replay or forward_shadow")
    if not isinstance(config["strategies"], Mapping) or not config["strategies"]:
        raise ContractError("strategies must be a nonempty mapping")
    strategies = {}
    for name, raw in config["strategies"].items():
        if not isinstance(name, str) or not name.strip() or not isinstance(raw, Mapping):
            raise ContractError("strategies require nonempty identifiers and field mappings")
        fields = dict(raw)
        if fields.pop("strategy_id", name) != name:
            raise ContractError("strategy identifier mismatch")
        fields["family"] = StrategyFamily(fields["family"])
        fields["side"] = StrategySide(fields["side"])
        fields["horizon_days"] = tuple(fields["horizon_days"])
        fields["required_inputs"] = tuple(fields["required_inputs"])
        if type(fields["rebalance_days"]) is not int or any(type(day) is not int for day in fields["horizon_days"]):
            raise ContractError("strategy intervals must be integers")
        strategies[name] = StrategyDefinition(strategy_id=name, **fields)
    allocation = config["allocation"]
    if not isinstance(allocation, Mapping) or set(allocation) != set(strategies):
        raise ContractError("allocation must cover exactly the fixed strategies")
    if any(isinstance(w, bool) or not isfinite(w) or w < 0 for w in allocation.values()) or not 0 < sum(allocation.values()) <= 1.000001:
        raise ContractError("allocation weights must be nonnegative with sum in (0,1]")
    if not isfinite(config["initial_cash"]) or config["initial_cash"] <= 0:
        raise ContractError("initial_cash must be finite and positive")
    policy = config["execution_policy"]
    if not isinstance(policy, Mapping) or not {"commission_bps", "slippage_bps", "annual_borrow_rate"} <= policy.keys():
        raise ContractError("execution costs must be explicit")
    execution = ExecutionPolicy(**policy)
    if config["mode"] == "forward_shadow":
        for key in ("max_signal_age_seconds", "max_bar_delay_seconds"):
            value = config.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
                raise ContractError(f"forward mode requires positive {key}")
    return strategies, execution


@contextmanager
def _db(path):
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS books (book_id TEXT PRIMARY KEY, config TEXT NOT NULL, config_hash TEXT NOT NULL, engine TEXT NOT NULL, created_at TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS events (book_id TEXT NOT NULL REFERENCES books(book_id), event_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL, archived_at TEXT NOT NULL, PRIMARY KEY(book_id,event_id), UNIQUE(book_id,seq))")
        connection.execute("CREATE TABLE IF NOT EXISTS daily (book_id TEXT NOT NULL REFERENCES books(book_id), day TEXT NOT NULL, event_id TEXT NOT NULL, output TEXT NOT NULL, output_hash TEXT NOT NULL, PRIMARY KEY(book_id,day))")
        for table in ("books", "events", "daily"):
            for operation in ("UPDATE", "DELETE"):
                connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only paper ledger'); END")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _load(db, book_id):
    row = db.execute("SELECT * FROM books WHERE book_id=?", (book_id,)).fetchone()
    if row is None:
        raise ContractError("unknown paper book")
    config = json.loads(row["config"])
    if _hash(config) != row["config_hash"] or json.loads(row["engine"]) != _engine():
        raise ContractError("immutable config or engine hash mismatch; create a new book")
    return row, config


def _safe(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ContractError:
            raise
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, OverflowError) as exc:
            raise ContractError(f"invalid paper book operation: {exc}") from exc
    return wrapped


@_safe
def create_book(db_path, book_id, config):
    """Idempotent creation only when the existing configuration matches exactly."""
    if not isinstance(book_id, str) or not book_id.strip():
        raise ContractError("book_id is required")
    _config(config)
    encoded = _json(config)
    with _db(db_path) as db:
        existing = db.execute("SELECT 1 FROM books WHERE book_id=?", (book_id,)).fetchone()
        if existing:
            _, prior = _load(db, book_id)
            if _hash(prior) != _hash(config):
                raise ContractError("book configuration is immutable")
        else:
            db.execute("INSERT INTO books VALUES (?,?,?,?,?)",
                       (book_id, encoded, _hash(config), _json(_engine()), _utc_now().isoformat()))
        return _status(db, book_id)


def _history(db, book_id):
    events = db.execute("SELECT * FROM events WHERE book_id=? ORDER BY seq", (book_id,)).fetchall()
    result = []
    for row in events:
        payload = json.loads(row["payload"])
        if _hash(payload) != row["payload_hash"]:
            raise ContractError("stored input hash mismatch")
        result.append((payload, _stamp(row["archived_at"])))
    return result


def _status(db, book_id):
    row, config = _load(db, book_id)
    history = _history(db, book_id)
    outputs = []
    for item in db.execute("SELECT * FROM daily WHERE book_id=? ORDER BY day", (book_id,)):
        output = json.loads(item["output"])
        if _hash(output) != item["output_hash"]:
            raise ContractError("stored output hash mismatch")
        outputs.append(output)
    return {"book_id": book_id, "mode": config["mode"], "paper_only": True,
            "point_in_time_verified": False, "financial_alpha_verified": False,
            "config_hash": row["config_hash"], "created_at": row["created_at"],
            "engine_hash": _hash(json.loads(row["engine"])),
            "config": config,
            "events": [{"event_id": payload["event_id"], "kind": payload["kind"],
                        "payload_hash": _hash(payload), "archived_at": archived.isoformat()}
                       for payload, archived in history],
            "event_count": len(history), "daily": outputs,
            "latest": outputs[-1] if outputs else {"paper_nav": config["initial_cash"],
                                                       "paper_cash": config["initial_cash"], "positions": {},
                                                       "day": None, "fills": [], "drawdown": 0.0,
                                                       "gross_turnover": 0.0, "cumulative_borrow_cost": 0.0,
                                                       "cumulative_commission": 0.0},
            "limitations": ["Local paper ledger; no broker orders or real-money balances.",
                            "Caller data lineage is not independent PIT or alpha verification."]}


@_safe
def book_status(db_path, book_id):
    with _db(db_path) as db:
        return _status(db, book_id)


def _signal(raw):
    _keys(raw, ("strategy_id", "asset", "score", "available_at", "snapshot_hash"), ("session",))
    return PointInTimeSignal(raw["strategy_id"], raw["asset"], raw["score"],
                             _stamp(raw["available_at"]), raw["snapshot_hash"])


def _bar(raw):
    _keys(raw, ("day", "asset", "open", "high", "low", "close", "dollar_volume", "borrow_available"))
    if type(raw["borrow_available"]) is not bool:
        raise ContractError("borrow_available must be a boolean")
    return MarketBar(date.fromisoformat(raw["day"]), raw["asset"], raw["open"], raw["high"],
                     raw["low"], raw["close"], raw["dollar_volume"], raw["borrow_available"])


@_safe
def ingest_book(db_path, book_id, event):
    """Atomically append one signal batch or completed daily bar batch.

    Retrying an identical event_id is a no-op even after the feed freshness window
    expires. Reusing that key with changed content fails. Separate event IDs cannot
    duplicate a bar date or strategy/asset/availability key.
    """
    if not isinstance(event, Mapping) or not isinstance(event.get("event_id"), str) or not event["event_id"].strip():
        raise ContractError("event requires nonempty event_id")
    with _db(db_path) as db:
        book, config = _load(db, book_id)
        prior = db.execute("SELECT payload_hash FROM events WHERE book_id=? AND event_id=?", (book_id, event["event_id"])).fetchone()
        if prior:
            if prior[0] != _hash(event):
                raise ContractError("event_id already exists with different content")
            return _status(db, book_id)
        now = _utc_now()
        history = _history(db, book_id)
        if history and now < history[-1][1]:
            raise ContractError("archive clock moved backwards")
        strategies, execution = _config(config)
        signal_history = [(raw, archived) for payload, archived in history if payload["kind"] == "signals" for raw in payload["signals"]]
        bar_history = [raw for payload, _ in history if payload["kind"] == "bars" for raw in payload["bars"]]
        last_day = max((date.fromisoformat(raw["day"]) for raw in bar_history), default=None)
        forward = config["mode"] == "forward_shadow"
        output = None
        if event.get("kind") == "signals":
            _keys(event, ("event_id", "kind", "signals"))
            if not isinstance(event["signals"], list) or not event["signals"]:
                raise ContractError("signal batch cannot be empty")
            keys = {(raw["strategy_id"], raw["asset"], _stamp(raw["available_at"])) for raw, _ in signal_history}
            previous_stamp = max((key[2] for key in keys), default=None)
            for raw in event["signals"]:
                signal = _signal(raw)
                key = (signal.strategy_id, signal.asset, signal.available_at)
                if key in keys or signal.strategy_id not in strategies:
                    raise ContractError("duplicate signal key or unknown strategy")
                if (last_day and signal.available_at.date() < last_day) or (previous_stamp and signal.available_at < previous_stamp):
                    raise ContractError("backdated signal insertion is forbidden")
                if forward and (signal.available_at > now or signal.available_at < _stamp(book["created_at"])
                                or signal.available_at.date() != now.date()
                                or (now - signal.available_at).total_seconds() > config["max_signal_age_seconds"]):
                    raise ContractError("forward signal is future, stale, or predates book creation")
                keys.add(key)
        elif event.get("kind") == "bars":
            _keys(event, ("event_id", "kind", "window_start", "window_end", "bars"))
            start, end = _stamp(event["window_start"]), _stamp(event["window_end"])
            if start.time() != datetime.min.time() or end != start + timedelta(days=1):
                raise ContractError("bar windows must be one complete UTC day")
            if not isinstance(event["bars"], list) or not event["bars"]:
                raise ContractError("daily bar batch cannot be empty")
            bars = [_bar(raw) for raw in event["bars"]]
            if any(bar.day != start.date() for bar in bars) or len({bar.asset for bar in bars}) != len(bars):
                raise ContractError("bar batch must contain unique assets for its window date")
            if last_day and start.date() <= last_day:
                raise ContractError("bar days must be strictly chronological and immutable")
            if forward and (start < _stamp(book["created_at"]) or end > now
                            or (now - end).total_seconds() > config["max_bar_delay_seconds"]):
                raise ContractError("forward bar is future, stale, or backfilled before book creation")
            if not signal_history:
                raise ContractError("archive signals in a separate transaction before bars")
            if any(_stamp(raw["available_at"]) >= end for raw, _ in signal_history):
                raise ContractError("bar predates already archived signal chronology")
            if forward and any(archived >= start for raw, archived in signal_history
                               if _stamp(raw["available_at"]) < start):
                raise ContractError("eligible signals must be archived before the bar window starts")
            output = _replay(config, strategies, execution, bar_history + event["bars"], signal_history,
                             start.date(), book["config_hash"], history, event, db, book_id)
        else:
            raise ContractError("event kind must be signals or bars")
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (book_id, event["event_id"], len(history) + 1,
                   _json(event), _hash(event), now.isoformat()))
        if output is not None:
            db.execute("INSERT INTO daily VALUES (?,?,?,?,?)", (book_id, output["day"], event["event_id"],
                       _json(output), _hash(output)))
        return _status(db, book_id)


def _replay(config, strategies, execution, raw_bars, raw_signals, day, config_hash, history, event, db, book_id):
    bars = tuple(_bar(raw) for raw in raw_bars)
    identity = _hash({"config": config_hash, "events": [payload for payload, _ in history] + [event]})
    signals = tuple(PointInTimeSignal(s.strategy_id, s.asset, s.score, s.available_at, identity)
                    for raw, _ in raw_signals for s in [_signal(raw)])
    dataset = HistoricalDataset(identity, False, bars, signals, allow_unverified=True)
    result = PortfolioBacktester(strategies, execution).run(dataset, config["allocation"],
                                                           config["initial_cash"], allow_unverified=True)
    # Future appended data must not alter any already committed NAV or fill.
    nav = dict(result.nav_by_day)
    for row in db.execute("SELECT output FROM daily WHERE book_id=?", (book_id,)):
        prior = json.loads(row[0])
        prior_day = date.fromisoformat(prior["day"])
        fills = json.loads(_json([asdict(fill) for fill in result.fills if fill.day == prior_day]))
        if nav[prior_day] != prior["paper_nav"] or fills != prior["fills"]:
            raise ContractError("replay would revise a prior output")
    positions = {}
    for fill in result.fills:
        positions[fill.asset] = positions.get(fill.asset, 0.0) + fill.quantity
    positions = {asset: quantity for asset, quantity in positions.items() if abs(quantity) >= 1e-12}
    cash = config["initial_cash"] - sum(fill.quantity * fill.price + fill.commission for fill in result.fills) - result.borrow_cost
    previous = result.nav_by_day[-2][1] if len(result.nav_by_day) > 1 else config["initial_cash"]
    daily_fills = [fill for fill in result.fills if fill.day == day]
    peak = max(config["initial_cash"], *(value for _, value in result.nav_by_day))
    return {"day": day.isoformat(), "paper_nav": nav[day], "paper_cash": cash, "positions": positions,
            "fills": [asdict(fill) for fill in daily_fills], "rejected_orders": list(result.rejected_orders),
            "gross_turnover": sum(abs(f.quantity * f.price) for f in daily_fills) / previous if previous > 0 else None,
            "drawdown": nav[day] / peak - 1, "cumulative_borrow_cost": result.borrow_cost,
            "cumulative_commission": sum(fill.commission for fill in result.fills),
            "input_chain_hash": identity, "config_hash": config_hash,
            "full_replay": asdict(result)}


@_safe
def run_paper_trading(request):
    action = request.get("action") if isinstance(request, Mapping) else None
    extra = {"create": "config", "ingest": "event", "status": None}.get(action)
    if action not in {"create", "ingest", "status"}:
        raise ContractError("action must be create, ingest, or status")
    _keys(request, ("action", "db_path", "book_id", *([extra] if extra else [])))
    if action == "create":
        return create_book(request["db_path"], request["book_id"], request["config"])
    if action == "ingest":
        return ingest_book(request["db_path"], request["book_id"], request["event"])
    return book_status(request["db_path"], request["book_id"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="Append-only local paper book; never sends broker orders")
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    try:
        print(_json(run_paper_trading(json.loads(Path(args.request).read_text()))))
    except (ContractError, OSError, ValueError) as exc:
        parser.exit(2, f"paper book: {exc}\n")


if __name__ == "__main__":
    main()
