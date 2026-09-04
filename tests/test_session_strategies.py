"""Synthetic timestamp/accounting tests; not evidence of strategy performance."""

import csv
import hashlib
import io
import json
from dataclasses import asdict, replace
from datetime import datetime

import pytest

from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.session_strategies import (
    SessionBacktestRequest,
    SessionStrategyConfig,
    run_session_backtest,
)

CAL_COLUMNS = ("exchange", "session", "open_at", "close_at")
BAR_COLUMNS = ("exchange", "asset", "open_at", "end_at", "open", "high", "low", "close", "kind")


def session(day, offset="-05:00", close="16:00"):
    return ["XNYS", day, f"{day}T09:30:00{offset}", f"{day}T{close}:00{offset}"]


def bar(day, at, end, price=100, close=None, high=None, low=None, offset="-05:00", kind="intraday", asset="A"):
    close = price if close is None else close
    return ["XNYS", asset, f"{day}T{at}:00{offset}", f"{day}T{end}:00{offset}",
            price, max(price, close) if high is None else high,
            min(price, close) if low is None else low, close, kind]


def write_csv(tmp_path, name, columns, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(columns)
    writer.writerows(rows)
    raw = stream.getvalue().encode()
    path = tmp_path / name
    path.write_bytes(raw)
    return str(path), hashlib.sha256(raw).hexdigest()


def request(tmp_path, *, calendars=None, bars=None, books=None, end="2024-03-11T09:30:00-04:00", **kwargs):
    calendars = calendars if calendars is not None else [session("2024-03-08"), session("2024-03-11", "-04:00")]
    bars = bars if bars is not None else [
        bar("2024-03-08", "15:55", "16:00", 100, 102),
        bar("2024-03-11", "09:30", "09:35", 110, 999, offset="-04:00"),
    ]
    calendar_csv, calendar_sha256 = write_csv(tmp_path, "calendar.csv", CAL_COLUMNS, calendars)
    bars_csv, bars_sha256 = write_csv(tmp_path, "bars.csv", BAR_COLUMNS, bars)
    return SessionBacktestRequest(
        books=tuple(books or [SessionStrategyConfig("overnight", "A", "XNYS", starting_cash=1000, max_capital=1000)]),
        calendar_csv=calendar_csv, bars_csv=bars_csv, calendar_sha256=calendar_sha256, bars_sha256=bars_sha256,
        start_at=kwargs.pop("start_at", "2024-03-08T09:30:00-05:00"), end_at=end,
        rules_set_at=kwargs.pop("rules_set_at", "2024-03-07T00:00:00Z"), **kwargs,
    )


def result(req):
    return run_session_backtest(req)["books"][0]


def test_friday_monday_dst_exact_opens_and_overnight_nav(tmp_path):
    book = result(request(tmp_path))
    assert book["status"] == "complete"
    trade, = book["trades"]
    assert trade["entry_at"] == "2024-03-08T20:55:00+00:00"
    assert trade["exit_at"] == "2024-03-11T13:30:00+00:00"
    assert (trade["entry_price"], trade["exit_price"]) == (100, 110)
    assert book["final_nav"] == 1100
    assert book["cash"] == 1100
    assert any(p["nav"] == 1020 and p["source"] == "bar_close" for p in book["nav"])
    assert book["total_return"] == pytest.approx(.1)


def test_early_close_and_explicit_holiday_calendar(tmp_path):
    req = request(tmp_path, calendars=[session("2024-07-03", "-04:00", "13:00"), session("2024-07-05", "-04:00")],
                  bars=[bar("2024-07-03", "12:55", "13:00", offset="-04:00"),
                        bar("2024-07-05", "09:30", "09:35", 105, offset="-04:00")],
                  start_at="2024-07-03T09:30:00-04:00", end="2024-07-05T09:30:00-04:00")
    trade, = result(req)["trades"]
    assert trade["entry_at"] == "2024-07-03T16:55:00+00:00"
    assert trade["exit_at"] == "2024-07-05T13:30:00+00:00"


def test_independent_named_books_long_short_capital_and_costs(tmp_path):
    books = [SessionStrategyConfig("long", "A", "XNYS", starting_cash=1000, max_capital=500, leverage=2, cost_bps=10),
             SessionStrategyConfig("short", "A", "XNYS", side="short", starting_cash=2000, max_capital=500)]
    req = request(tmp_path, books=books)
    long, short = run_session_backtest(req)["books"]
    n = 1000 / 1.002
    assert long["final_nav"] == pytest.approx(1000 + .1*n - .001*n - .001*1.1*n)
    assert long["total_costs"] == pytest.approx(.0021*n)
    assert short["final_nav"] == 1950
    assert short["trades"][0]["quantity"] == -5
    assert result(replace(req, books=(books[1],))) == short


def test_delayed_exit_open_never_uses_its_close(tmp_path):
    cfg = SessionStrategyConfig("delay", "A", "XNYS", exit_minutes_after_next_open=5, starting_cash=1000)
    req = request(tmp_path, books=[cfg], end="2024-03-11T09:35:00-04:00", bars=[
        bar("2024-03-08", "15:55", "16:00"),
        bar("2024-03-11", "09:30", "09:35", 110, 112, offset="-04:00"),
        bar("2024-03-11", "09:35", "09:40", 115, 800, offset="-04:00"),
    ])
    assert result(req)["trades"][0]["exit_price"] == 115


def test_zero_offset_requires_explicit_auction(tmp_path):
    cfg = SessionStrategyConfig("auction", "A", "XNYS", entry_minutes_before_close=0)
    req = request(tmp_path, books=[cfg])
    assert result(req)["failure"]["code"] == "missing_auction"
    req = request(tmp_path, books=[cfg], bars=[
        bar("2024-03-08", "16:00", "16:00", 103, kind="auction"),
        bar("2024-03-11", "09:30", "09:35", 110, offset="-04:00"),
    ])
    assert result(req)["trades"][0]["entry_price"] == 103


@pytest.mark.parametrize("missing", ["entry", "exit", "held"])
def test_missing_bars_stop_without_fabrication(tmp_path, missing):
    bars = [bar("2024-03-08", "15:55", "15:56"),
            bar("2024-03-08", "15:56", "16:00", 102),
            bar("2024-03-11", "09:30", "09:35", 110, offset="-04:00")]
    bars.pop({"entry": 0, "held": 1, "exit": 2}[missing])
    book = result(request(tmp_path, bars=bars))
    assert book["status"] == "failed"
    assert book["failure"]["code"] == "missing_bar"
    assert book["trades"] == []
    assert (book["open_position"] is None) == (missing == "entry")
    if missing != "entry":
        assert book["open_position"]["unmatched_reason"] == "stopped_on_failure"


def test_failed_book_does_not_stop_other_book(tmp_path):
    books = [SessionStrategyConfig("missing", "B", "XNYS"), SessionStrategyConfig("valid", "A", "XNYS")]
    output = run_session_backtest(request(tmp_path, books=books))
    assert output["status"] == "failed"
    assert [b["status"] for b in output["books"]] == ["failed", "complete"]


def test_unmatched_last_position_is_marked_not_force_closed(tmp_path):
    req = request(tmp_path, calendars=[session("2024-03-08")],
                  bars=[bar("2024-03-08", "15:55", "16:00", 100, 102)],
                  end="2024-03-08T16:00:00-05:00")
    book = result(req)
    assert book["status"] == "open_position"
    assert book["trades"] == []
    assert book["final_nav"] == 1020
    assert book["open_position"]["unmatched_reason"] == "calendar_exhausted"
    assert book["open_position"]["unrealized_pnl"] == 20


def test_cutoff_does_not_use_unfinished_bar_high_low_close(tmp_path):
    cfg = SessionStrategyConfig("prefix", "A", "XNYS", stop_loss_pct=.05, take_profit_pct=.05)
    req = request(tmp_path, books=[cfg], end="2024-03-08T15:57:00-05:00", bars=[
        bar("2024-03-08", "15:55", "16:00", 100, 200, high=300, low=1),
    ])
    book = result(req)
    assert book["trades"] == []
    assert book["final_nav"] == cfg.starting_cash
    assert book["open_position"]["mark_source"] == "bar_open"


def test_future_data_append_and_changes_preserve_prefix(tmp_path):
    req = request(tmp_path, end="2024-03-08T16:00:00-05:00")
    before = result(req)
    req2 = request(tmp_path, end="2024-03-08T16:00:00-05:00", bars=[
        bar("2024-03-08", "15:55", "16:00", 100, 102),
        bar("2024-03-11", "09:30", "09:35", 1, 10000, offset="-04:00"),
        bar("2024-03-11", "15:55", "16:00", 999, offset="-04:00"),
    ])
    assert result(req2) == before


@pytest.mark.parametrize("side,expected", [("long", 95), ("short", 105)])
def test_ambiguous_intrabar_stop_first_and_bounded_timestamp(tmp_path, side, expected):
    cfg = SessionStrategyConfig("risk", "A", "XNYS", side=side, stop_loss_pct=.05, take_profit_pct=.05)
    req = request(tmp_path, books=[cfg], bars=[bar("2024-03-08", "15:55", "16:00", high=110, low=90)])
    trade, = result(req)["trades"]
    assert trade["reason"] == "stop_loss"
    assert trade["exit_price"] == expected
    assert trade["exit_time_precision"] == "bar_interval"
    assert trade["exit_interval_start"] == trade["entry_at"]


def test_overnight_gap_stop_fills_observed_open(tmp_path):
    cfg = SessionStrategyConfig("risk", "A", "XNYS", exit_minutes_after_next_open=5, stop_loss_pct=.05)
    req = request(tmp_path, books=[cfg], end="2024-03-11T09:35:00-04:00", bars=[
        bar("2024-03-08", "15:55", "16:00"),
        bar("2024-03-11", "09:30", "09:35", 80, offset="-04:00"),
    ])
    trade, = result(req)["trades"]
    assert (trade["exit_price"], trade["reason"], trade["exit_time_precision"]) == (80, "stop_loss", "exact")


def test_json_roundtrip_and_exact_bytes_hash(tmp_path):
    req = request(tmp_path)
    encoded = json.dumps(asdict(req), default=lambda value: value.isoformat())
    assert run_session_backtest(encoded) == run_session_backtest(req)
    with open(req.bars_csv, "ab") as handle:
        handle.write(b"\n")
    with pytest.raises(ContractError, match="SHA-256 mismatch"):
        run_session_backtest(req)


@pytest.mark.parametrize("field,value", [("entry_minutes_before_close", -1), ("exit_minutes_after_next_open", True),
                                         ("side", "both"), ("leverage", float("inf")),
                                         ("cost_bps", -1), ("stop_loss_pct", 1), ("starting_cash", 0)])
def test_invalid_config(field, value):
    with pytest.raises(ContractError):
        SessionStrategyConfig("bad", "A", "XNYS", **{field: value})


def test_rules_must_be_set_before_test_and_names_unique(tmp_path):
    req = request(tmp_path)
    with pytest.raises(ContractError, match="rules_set_at"):
        replace(req, rules_set_at=req.start_at)
    with pytest.raises(ContractError, match="unique"):
        replace(req, books=req.books * 2)
    with pytest.raises(ContractError, match="timezone-aware"):
        replace(req, start_at=datetime(2024, 3, 8))  # noqa: DTZ001 -- intentionally invalid


@pytest.mark.parametrize("case", ["naive", "duplicate", "overlap", "outside", "ohlc"])
def test_strict_bar_validation(tmp_path, case):
    bars = [bar("2024-03-08", "15:55", "16:00")]
    if case == "naive":
        bars[0][2] = "2024-03-08T15:55:00"
    elif case == "duplicate":
        bars *= 2
    elif case == "overlap":
        bars.append(bar("2024-03-08", "15:56", "16:00"))
    elif case == "outside":
        bars = [bar("2024-03-08", "15:55", "16:01")]
    else:
        bars[0][5] = 99
    with pytest.raises(ContractError):
        run_session_backtest(request(tmp_path, bars=bars))


def test_nonpositive_nav_fails_without_fabricated_liquidation(tmp_path):
    cfg = SessionStrategyConfig("short", "A", "XNYS", side="short", starting_cash=1000, leverage=2)
    req = request(tmp_path, books=[cfg], bars=[bar("2024-03-08", "15:55", "16:00", 100, 200)])
    book = result(req)
    assert book["failure"]["code"] == "nonpositive_or_nonfinite_nav"
    assert book["trades"] == []
    assert book["open_position"] is not None


def test_fall_dst_uses_supplied_offset(tmp_path):
    req = request(tmp_path, calendars=[session("2024-11-01", "-04:00"), session("2024-11-04")],
                  bars=[bar("2024-11-01", "15:55", "16:00", offset="-04:00"),
                        bar("2024-11-04", "09:30", "09:35", 101)],
                  start_at="2024-11-01T09:30:00-04:00", end="2024-11-04T09:30:00-05:00")
    trade, = result(req)["trades"]
    assert trade["entry_at"] == "2024-11-01T19:55:00+00:00"
    assert trade["exit_at"] == "2024-11-04T14:30:00+00:00"


def test_multiple_round_trips_compound_cash(tmp_path):
    req = request(tmp_path, calendars=[session("2024-03-08"), session("2024-03-11", "-04:00"),
                                      session("2024-03-12", "-04:00")],
                  bars=[bar("2024-03-08", "15:55", "16:00"),
                        bar("2024-03-11", "09:30", "09:35", 110, offset="-04:00"),
                        bar("2024-03-11", "15:55", "16:00", 100, offset="-04:00"),
                        bar("2024-03-12", "09:30", "09:35", 110, offset="-04:00")],
                  books=[SessionStrategyConfig("compound", "A", "XNYS", starting_cash=1000)],
                  end="2024-03-12T09:30:00-04:00")
    book = result(req)
    assert [t["quantity"] for t in book["trades"]] == [10, 11]
    assert book["final_nav"] == 1210


def test_cutoff_at_next_open_marks_delayed_exit_position(tmp_path):
    cfg = SessionStrategyConfig("delayed", "A", "XNYS", exit_minutes_after_next_open=5, starting_cash=1000)
    book = result(request(tmp_path, books=[cfg]))
    assert book["trades"] == []
    assert book["final_nav"] == 1100
    assert book["open_position"]["mark_at"] == "2024-03-11T13:30:00+00:00"


def test_calendar_hash_and_naive_calendar_rejected(tmp_path):
    req = request(tmp_path)
    with pytest.raises(ContractError, match="SHA-256 mismatch"):
        run_session_backtest(replace(req, calendar_sha256="0" * 64))
    s = session("2024-03-08")
    s[2] = "2024-03-08T09:30:00"
    with pytest.raises(ContractError, match="timezone-aware"):
        run_session_backtest(request(tmp_path, calendars=[s]))


def test_missing_exact_entry_does_not_use_nearest_open(tmp_path):
    book = result(request(tmp_path, bars=[bar("2024-03-08", "15:54", "16:00")]))
    assert book["failure"]["code"] == "missing_bar"
    assert book["cash"] == 1000


def test_take_profit_and_no_reentry_in_same_session(tmp_path):
    cfg = SessionStrategyConfig("target", "A", "XNYS", take_profit_pct=.05)
    req = request(tmp_path, books=[cfg], bars=[bar("2024-03-08", "15:55", "15:56", high=106)])
    book = result(req)
    assert book["status"] == "complete"
    assert len(book["trades"]) == 1
    assert book["trades"][0]["reason"] == "take_profit"
    assert book["trades"][0]["exit_price"] == 105


def test_offsets_outside_early_session_fail(tmp_path):
    cfg = SessionStrategyConfig("bad", "A", "XNYS", entry_minutes_before_close=500)
    assert result(request(tmp_path, books=[cfg]))["failure"]["code"] == "timing_outside_session"


def test_future_exit_ohlc_cannot_trigger_risk_at_scheduled_open(tmp_path):
    cfg = SessionStrategyConfig("risk", "A", "XNYS", stop_loss_pct=.05, take_profit_pct=.05)
    req = request(tmp_path, books=[cfg], bars=[bar("2024-03-08", "15:55", "16:00"),
        bar("2024-03-11", "09:30", "09:35", 101, high=10000, low=1, offset="-04:00")])
    trade, = result(req)["trades"]
    assert trade["reason"] == "scheduled_exit"
    assert trade["exit_price"] == 101


def test_cutoff_at_bar_boundary_uses_new_open_and_gap_stop(tmp_path):
    cfg = SessionStrategyConfig("risk", "A", "XNYS", stop_loss_pct=.05, starting_cash=1000)
    req = request(tmp_path, books=[cfg], end="2024-03-08T15:56:00-05:00", bars=[
        bar("2024-03-08", "15:55", "15:56"),
        bar("2024-03-08", "15:56", "16:00", 80, 200, low=1),
    ])
    trade, = result(req)["trades"]
    assert trade["exit_price"] == 80
    assert trade["exit_time_precision"] == "exact"
