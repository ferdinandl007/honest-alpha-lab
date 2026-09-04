"""Historical, precommitted session books; no broker or live-order integration.

Public API: SessionStrategyConfig, SessionBacktestRequest.from_dict / from_json,
run_session_backtest(request) -> JSON-compatible dict.

Calendar CSV: exchange,session,open_at,close_at (session is an ISO date).
Bars CSV: exchange,asset,open_at,end_at,open,high,low,close,kind.
All timestamps must include an offset. Intraday bars cover [open_at,end_at);
kind=auction is a point record at the explicit calendar close with identical OHLC.
Bars must tile held regular-session intervals: missing observations fail the book.
No trading-day inference, price interpolation, symbol selection, or data approval.

Offsets are elapsed minutes. Entry and scheduled exit use the exact bar OPEN,
never its later close. Stop/target orders are fixed fractions of entry price;
gaps fill at the observed open, intrabar hits at the threshold, stop first when
both hit. Intrabar fill times are only bounded by the bar and reported as such.
Unfinished bars contribute only their open. No after-hours monitoring is assumed.
Fractional quantities are supported. Costs are charged on each fill's notional;
max_capital caps allocated equity and leverage caps entry gross exposure including
entry costs. Borrow/financing fees and intrabar margin calls are not modeled.
Nonpositive observed NAV fails the book. Short books assume borrow availability.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from .contracts import ContractError


def _time(value: Any) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("naive timestamp")
        return result.astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"timezone-aware ISO timestamp required: {value!r}") from exc


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name} must be numeric")
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ContractError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ContractError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


@dataclass(frozen=True)
class SessionStrategyConfig:
    book_name: str
    asset: str
    exchange: str
    entry_minutes_before_close: int = 5
    exit_minutes_after_next_open: int = 0
    side: str = "long"
    starting_cash: float = 100_000.0
    max_capital: float = 100_000.0
    leverage: float = 1.0
    cost_bps: float = 0.0
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None

    def __post_init__(self) -> None:
        for name in ("book_name", "asset", "exchange"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"{name} must be a nonempty string")
        for name in ("entry_minutes_before_close", "exit_minutes_after_next_open"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ContractError(f"{name} must be a nonnegative integer")
        if self.side not in ("long", "short"):
            raise ContractError("side must be long or short")
        for name in ("starting_cash", "max_capital", "leverage", "cost_bps"):
            object.__setattr__(self, name, _number(getattr(self, name), name,
                                                 positive=name != "cost_bps"))
        if self.cost_bps >= 10_000:
            raise ContractError("cost_bps must be below 10000")
        for name in ("stop_loss_pct", "take_profit_pct"):
            value = getattr(self, name)
            if value is not None:
                value = _number(value, name, positive=True)
                if value >= 1:
                    raise ContractError(f"{name} must be a fraction below 1")
                object.__setattr__(self, name, value)


@dataclass(frozen=True)
class SessionBacktestRequest:
    books: tuple[SessionStrategyConfig, ...]
    calendar_csv: str
    bars_csv: str
    calendar_sha256: str
    bars_sha256: str
    start_at: datetime
    end_at: datetime
    rules_set_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "books", tuple(self.books))
        if not self.books or any(not isinstance(b, SessionStrategyConfig) for b in self.books):
            raise ContractError("books must contain SessionStrategyConfig objects")
        if len({b.book_name for b in self.books}) != len(self.books):
            raise ContractError("book names must be unique")
        for name in ("start_at", "end_at", "rules_set_at"):
            object.__setattr__(self, name, _time(getattr(self, name)))
        if not self.rules_set_at < self.start_at <= self.end_at:
            raise ContractError("require rules_set_at < start_at <= end_at")
        for name in ("calendar_sha256", "bars_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(
                c not in "0123456789abcdef" for c in value
            ):
                raise ContractError(f"{name} must be a lowercase SHA-256 digest")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SessionBacktestRequest:
        try:
            data = dict(value)
            data["books"] = tuple(SessionStrategyConfig(**book) for book in data["books"])
            return cls(**data)
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError(f"invalid session request: {exc}") from exc

    @classmethod
    def from_json(cls, value: str) -> SessionBacktestRequest:
        try:
            data = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ContractError("invalid session request JSON") from exc
        if not isinstance(data, dict):
            raise ContractError("session request must be an object")
        return cls.from_dict(data)


@dataclass(frozen=True)
class _Session:
    exchange: str
    session: str
    open_at: datetime
    close_at: datetime


@dataclass(frozen=True)
class _Bar:
    exchange: str
    asset: str
    open_at: datetime
    end_at: datetime
    open: float
    high: float
    low: float
    close: float
    kind: str


def _rows(path: str, digest: str, required: set[str]) -> list[dict[str, str]]:
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ContractError(f"SHA-256 mismatch: {path}")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ContractError(f"CSV requires columns: {sorted(required)}")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ContractError("duplicate CSV headers")
        rows = list(reader)
        if not rows or any(None in row or any(not row[k] for k in required) for row in rows):
            raise ContractError("empty CSV or missing/extra row fields")
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise ContractError(f"invalid CSV: {path}") from exc


def _load(request: SessionBacktestRequest) -> tuple[list[_Session], dict[tuple, _Bar]]:
    sessions = []
    for row in _rows(request.calendar_csv, request.calendar_sha256,
                     {"exchange", "session", "open_at", "close_at"}):
        try:
            date.fromisoformat(row["session"])
        except ValueError as exc:
            raise ContractError("calendar session must be an ISO date") from exc
        s = _Session(row["exchange"], row["session"], _time(row["open_at"]),
                     _time(row["close_at"]))
        if s.open_at >= s.close_at:
            raise ContractError("calendar open_at must precede close_at")
        sessions.append(s)
    sessions.sort(key=lambda s: (s.exchange, s.open_at))
    seen = set()
    previous: dict[str, _Session] = {}
    for s in sessions:
        if (s.exchange, s.session) in seen:
            raise ContractError("duplicate calendar session")
        if s.exchange in previous and previous[s.exchange].close_at >= s.open_at:
            raise ContractError("overlapping calendar sessions")
        seen.add((s.exchange, s.session))
        previous[s.exchange] = s
    bars = {}
    for row in _rows(request.bars_csv, request.bars_sha256,
                     {"exchange", "asset", "open_at", "end_at", "open", "high", "low", "close", "kind"}):
        b = _Bar(row["exchange"], row["asset"], _time(row["open_at"]),
                 _time(row["end_at"]), *(_number(row[k], k, positive=True)
                                        for k in ("open", "high", "low", "close")), row["kind"])
        if b.kind not in ("intraday", "auction"):
            raise ContractError("bar kind must be intraday or auction")
        if not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high:
            raise ContractError("invalid OHLC bounds")
        if b.kind == "auction":
            if b.open_at != b.end_at or len({b.open, b.high, b.low, b.close}) != 1:
                raise ContractError("auction requires a point timestamp and identical OHLC")
        elif b.open_at >= b.end_at:
            raise ContractError("intraday open_at must precede end_at")
        key = (b.exchange, b.asset, b.open_at, b.kind)
        if key in bars:
            raise ContractError("duplicate bar timestamp")
        bars[key] = b
    # Only supplied calendar sessions define tradable time. No inferred sessions.
    groups: dict[tuple, list[_Bar]] = {}
    calendars: dict[str, list[_Session]] = {}
    for s in sessions:
        calendars.setdefault(s.exchange, []).append(s)
    opens = {exchange: [s.open_at for s in values] for exchange, values in calendars.items()}
    for b in bars.values():
        index = bisect_right(opens.get(b.exchange, []), b.open_at) - 1
        s = calendars[b.exchange][index] if index >= 0 else None
        if s is None or not (b.open_at == s.close_at if b.kind == "auction" else
                             s.open_at <= b.open_at < b.end_at <= s.close_at):
            raise ContractError("bar outside supplied calendar session")
        if b.kind == "intraday":
            groups.setdefault((b.exchange, b.asset), []).append(b)
    for group in groups.values():
        group.sort(key=lambda b: b.open_at)
        if any(a.end_at > b.open_at for a, b in pairwise(group)):
            raise ContractError("overlapping intraday bars")
    return sessions, bars


class _BookFailure(Exception):
    def __init__(self, code: str, at: datetime):
        self.code, self.at = code, at


def _run_book(config: SessionStrategyConfig, request: SessionBacktestRequest,
              calendar: list[_Session], bars: dict[tuple, _Bar]) -> dict[str, Any]:
    sessions = [s for s in calendar if s.exchange == config.exchange]
    cash = config.starting_cash
    position: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    nav: list[dict[str, Any]] = []
    total_cost = 0.0
    failure = None
    rate = config.cost_bps / 10_000
    sign = 1 if config.side == "long" else -1

    def observe(at: datetime, price: float, source: str) -> None:
        if position is not None:
            position.update(mark_price=price, mark_at=at.isoformat(), mark_source=source)
        value = cash + (position["quantity"] * price if position else 0)
        prev = nav[-1]["nav"] if nav else config.starting_cash
        nav.append({"at": at.isoformat(), "nav": value, "cash": cash,
                    "return": value / prev - 1 if prev > 0 else None, "source": source})
        if not math.isfinite(value) or value <= 0:
            raise _BookFailure("nonpositive_or_nonfinite_nav", at)

    def bar_at(at: datetime, kind: str = "intraday") -> _Bar:
        b = bars.get((config.exchange, config.asset, at, kind))
        if b is None:
            raise _BookFailure("missing_auction" if kind == "auction" else "missing_bar", at)
        return b

    def exit_position(at: datetime, price: float, reason: str,
                      interval_start: datetime | None = None) -> None:
        nonlocal cash, position, total_cost
        assert position is not None
        p = position
        fee = abs(p["quantity"]) * price * rate
        cash += p["quantity"] * price - fee
        total_cost += fee
        gross = p["quantity"] * (price - p["entry_price"])
        trades.append({"entry_at": p["entry_at"], "exit_at": at.isoformat(),
                       "entry_price": p["entry_price"], "exit_price": price,
                       "quantity": p["quantity"], "side": config.side, "reason": reason,
                       "exit_time_precision": "bar_interval" if interval_start else "exact",
                       "exit_interval_start": interval_start.isoformat() if interval_start else None,
                       "gross_pnl": gross, "costs": p["entry_cost"] + fee,
                       "net_pnl": gross - p["entry_cost"] - fee,
                       "return_on_entry_notional": (gross - p["entry_cost"] - fee)
                       / abs(p["quantity"] * p["entry_price"])})
        position = None
        observe(at, price, reason)

    def monitor(start: datetime, until: datetime, *, include_until_open: bool = False) -> None:
        cursor = start
        while position is not None and (cursor < until or (include_until_open and cursor == until)):
            b = bar_at(cursor)
            observe(cursor, b.open, "bar_open")
            p = position
            stop = (p["entry_price"] * (1 - sign * config.stop_loss_pct)
                    if config.stop_loss_pct is not None else None)
            target = (p["entry_price"] * (1 + sign * config.take_profit_pct)
                      if config.take_profit_pct is not None else None)
            for level, reason in ((stop, "stop_loss"), (target, "take_profit")):
                if level is None:
                    continue
                crossed = sign * (b.open - level)
                if (crossed <= 0 if reason == "stop_loss" else crossed >= 0):
                    exit_position(cursor, b.open, reason)
                    return
            if b.end_at > until:
                # OHLC from an unfinished interval is unavailable at the cutoff.
                return
            for level, reason in ((stop, "stop_loss"), (target, "take_profit")):
                if level is not None and b.low <= level <= b.high:
                    exit_position(b.end_at, level, reason, b.open_at)
                    return
            observe(b.end_at, b.close, "bar_close")
            cursor = b.end_at

    try:
        if not sessions:
            raise _BookFailure("missing_exchange_calendar", request.start_at)
        for index, s in enumerate(sessions):
            if s.open_at > request.end_at:
                break
            entry_at = s.close_at - timedelta(minutes=config.entry_minutes_before_close)
            exit_at = s.open_at + timedelta(minutes=config.exit_minutes_after_next_open)
            if s.close_at < request.start_at:
                continue
            if not s.open_at <= entry_at <= s.close_at or exit_at >= s.close_at:
                raise _BookFailure("timing_outside_session", s.open_at)
            if exit_at > entry_at:
                raise _BookFailure("exit_overlaps_next_entry", s.open_at)
            if position is not None:
                monitor(s.open_at, min(exit_at, request.end_at),
                        include_until_open=request.end_at < exit_at)
                if position is not None and exit_at <= request.end_at:
                    exit_position(exit_at, bar_at(exit_at).open, "scheduled_exit")
            if not request.start_at <= entry_at <= request.end_at:
                continue
            if position is not None:
                raise _BookFailure("position_still_open", entry_at)
            kind = "auction" if config.entry_minutes_before_close == 0 else "intraday"
            b = bar_at(entry_at, kind)
            # Solve N <= leverage * (NAV - entry_cost), with allocated-equity cap.
            notional = min(cash, config.max_capital) * config.leverage / (1 + rate * config.leverage)
            fee = notional * rate
            quantity = sign * notional / b.open
            if not math.isfinite(notional) or not math.isfinite(quantity):
                raise _BookFailure("nonfinite_position_size", entry_at)
            cash -= quantity * b.open + fee
            total_cost += fee
            next_session = sessions[index + 1] if index + 1 < len(sessions) else None
            position = {"entry_at": entry_at.isoformat(), "entry_price": b.open,
                        "entry_cost": fee, "quantity": quantity,
                        "scheduled_exit_at": (next_session.open_at + timedelta(
                            minutes=config.exit_minutes_after_next_open)).isoformat()
                        if next_session else None}
            observe(entry_at, b.open, "auction" if kind == "auction" else "entry_open")
            if kind == "intraday":
                monitor(entry_at, min(s.close_at, request.end_at),
                        include_until_open=request.end_at < s.close_at)
    except _BookFailure as exc:
        failure = {"code": exc.code, "at": exc.at.isoformat()}
    final_nav = nav[-1]["nav"] if nav else config.starting_cash
    status = "failed" if failure else "open_position" if position else "complete"
    if position:
        position = dict(position)
        position["unrealized_pnl"] = position["quantity"] * (
            position["mark_price"] - position["entry_price"])
        position["unmatched_reason"] = ("stopped_on_failure" if failure else
            "calendar_exhausted" if position["scheduled_exit_at"] is None else "end_at_reached")
    return {"book_name": config.book_name, "config": asdict(config), "status": status,
            "failure": failure, "starting_cash": config.starting_cash, "cash": cash,
            "final_nav": final_nav, "total_return": final_nav / config.starting_cash - 1,
            "total_costs": total_cost, "trades": trades, "nav": nav,
            "open_position": position,
            "valuation_at": nav[-1]["at"] if nav else request.start_at.isoformat()}


def run_session_backtest(request: SessionBacktestRequest | Mapping[str, Any] | str) -> dict[str, Any]:
    """Run independent historical books. Invalid inputs raise ContractError;
    missing execution/held bars stop the affected book with status='failed'.
    start_at/end_at are inclusive; an entry exactly at end_at remains unmatched.
    No claims of independent historical approval follow from input hashes.
    """
    if isinstance(request, str):
        request = SessionBacktestRequest.from_json(request)
    elif isinstance(request, Mapping):
        request = SessionBacktestRequest.from_dict(request)
    if not isinstance(request, SessionBacktestRequest):
        raise ContractError("expected SessionBacktestRequest, mapping, or JSON object")
    sessions, bars = _load(request)
    books = [_run_book(config, request, sessions, bars) for config in request.books]
    return {"schema_version": 1, "mode": "historical_only", "rules_set_at": request.rules_set_at.isoformat(),
            "start_at": request.start_at.isoformat(), "end_at": request.end_at.isoformat(),
            "calendar_sha256": request.calendar_sha256, "bars_sha256": request.bars_sha256,
            "status": ("failed" if any(b["status"] == "failed" for b in books) else
                       "open_position" if any(b["status"] == "open_position" for b in books)
                       else "complete"),
            "books": books}
