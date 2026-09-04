"""Synthetic ledger tests, not financial performance evidence."""

import json
import sqlite3
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pytest

from honest_alpha_lab import paper_trading as paper
from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.portfolio import (
    HistoricalDataset,
    PointInTimeSignal,
    PortfolioBacktester,
)


def make_paper_config(mode="historical_replay"):
    config = {"mode": mode, "initial_cash": 1000, "allocation": {"s": 1},
              "strategies": {"s": {"name": "fixture", "family": "symbolic",
                                    "signal_description": "fixture", "required_inputs": ["score"],
                                    "horizon_days": [60], "side": "long_only", "rebalance_days": 1}},
              "execution_policy": {"commission_bps": 10, "slippage_bps": 5, "annual_borrow_rate": .03,
                                   "max_asset_weight": 1, "participation_rate": 1, "limit_offset_bps": 0}}
    if mode == "forward_shadow":
        config.update(max_signal_age_seconds=3600, max_bar_delay_seconds=3600)
    return config


def signal_event(stamp="2020-01-01T12:00:00+00:00", event_id="signal-1", **updates):
    return {"event_id": event_id, "kind": "signals", "signals": [
        {"strategy_id": "s", "asset": "A", "score": 1, "available_at": stamp,
         "snapshot_hash": "synthetic-source", **updates}]}


def bar_event(day=2, **updates):
    start = datetime(2020, 1, day, tzinfo=UTC)
    return {"event_id": f"bar-{day}", "kind": "bars", "window_start": start.isoformat(),
            "window_end": (start + timedelta(days=1)).isoformat(), "bars": [
                {"day": start.date().isoformat(), "asset": "A", "open": 100, "high": 102,
                 "low": 98, "close": 101, "dollar_volume": 1e6, "borrow_available": True, **updates}]}


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "paper.sqlite"
    paper.create_book(path, "b", make_paper_config())
    return path


def test_restart_idempotency_exact_engine_and_balances(book):
    paper.ingest_book(book, "b", signal_event())
    first = paper.ingest_book(book, "b", bar_event(2))
    assert first["latest"]["fills"] == []
    saved = deepcopy(first["daily"])
    for day in (3, 4, 5):
        result = paper.ingest_book(book, "b", bar_event(day))
        assert result == paper.book_status(book, "b")
        assert result == paper.ingest_book(book, "b", bar_event(day))
    assert result["daily"][:1] == saved
    assert result["event_count"] == 5
    assert result["paper_only"]
    assert result["daily"][1]["fills"]
    config = make_paper_config()
    strategies, execution = paper._config(config)
    bars = tuple(paper._bar(bar_event(day)["bars"][0]) for day in (2, 3, 4, 5))
    signal = PointInTimeSignal("s", "A", 1, datetime(2020, 1, 1, 12, tzinfo=UTC), "fixture")
    expected = PortfolioBacktester(strategies, execution).run(
        HistoricalDataset("fixture", False, bars, (signal,), allow_unverified=True),
        {"s": 1}, 1000, allow_unverified=True)
    assert [(date.fromisoformat(row["day"]), row["paper_nav"]) for row in result["daily"]] == list(expected.nav_by_day)
    latest = result["latest"]
    assert latest["paper_nav"] == pytest.approx(latest["paper_cash"] + latest["positions"].get("A", 0) * 101)
    assert latest["cumulative_commission"] == pytest.approx(sum(fill.commission for fill in expected.fills))
    json.dumps(result, allow_nan=False)


def test_immutable_config_and_sql_triggers(book):
    assert paper.create_book(book, "b", make_paper_config())["event_count"] == 0
    changed = make_paper_config()
    changed["initial_cash"] = 500
    with pytest.raises(ContractError, match="immutable"):
        paper.create_book(book, "b", changed)
    with sqlite3.connect(book) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE books SET config='{}'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM books")


def test_duplicate_keys_and_backdated_inputs(book):
    paper.ingest_book(book, "b", signal_event())
    with pytest.raises(ContractError, match="different content"):
        paper.ingest_book(book, "b", signal_event(score=2))
    with pytest.raises(ContractError, match="duplicate"):
        paper.ingest_book(book, "b", signal_event(event_id="duplicate"))
    paper.ingest_book(book, "b", bar_event(2))
    duplicate = bar_event(2)
    duplicate["event_id"] = "another-day-two"
    with pytest.raises(ContractError, match="chronological"):
        paper.ingest_book(book, "b", duplicate)
    with pytest.raises(ContractError, match="backdated"):
        paper.ingest_book(book, "b", signal_event("2020-01-01T23:00:00+00:00", "late"))
    assert paper.book_status(book, "b")["event_count"] == 2


def test_transaction_rolls_back_on_failure_and_retry_has_no_duplicate_fills(book, monkeypatch):
    paper.ingest_book(book, "b", signal_event())
    paper.ingest_book(book, "b", bar_event(2))
    before = paper.book_status(book, "b")
    original = paper._status

    def fail_after_insert(*args):
        raise RuntimeError("simulated crash before commit")

    monkeypatch.setattr(paper, "_status", fail_after_insert)
    with pytest.raises(RuntimeError, match="simulated crash"):
        paper.ingest_book(book, "b", bar_event(3))
    monkeypatch.setattr(paper, "_status", original)
    assert paper.book_status(book, "b") == before
    result = paper.ingest_book(book, "b", bar_event(3))
    assert result["event_count"] == 3
    assert len(result["latest"]["fills"]) == 1


def test_missing_held_asset_aborts_whole_day(book):
    paper.ingest_book(book, "b", signal_event())
    paper.ingest_book(book, "b", bar_event(2))
    paper.ingest_book(book, "b", bar_event(3))
    before = paper.book_status(book, "b")
    with pytest.raises(ContractError, match="missing price"):
        paper.ingest_book(book, "b", bar_event(4, asset="B"))
    assert paper.book_status(book, "b") == before


def set_clock(monkeypatch, stamp):
    monkeypatch.setattr(paper, "_utc_now", lambda: datetime.fromisoformat(stamp))


def test_forward_precommitted_signals_clock_windows_and_restart(tmp_path, monkeypatch):
    path = tmp_path / "forward.sqlite"
    set_clock(monkeypatch, "2020-01-01T11:00:00+00:00")
    paper.create_book(path, "b", make_paper_config("forward_shadow"))
    set_clock(monkeypatch, "2020-01-01T12:01:00+00:00")
    paper.ingest_book(path, "b", signal_event())
    with pytest.raises(ContractError, match="future"):
        paper.ingest_book(path, "b", bar_event(2))
    set_clock(monkeypatch, "2020-01-03T00:01:00+00:00")
    result = paper.ingest_book(path, "b", bar_event(2))
    assert result["mode"] == "forward_shadow"
    set_clock(monkeypatch, "2020-01-04T00:01:00+00:00")
    result = paper.ingest_book(path, "b", bar_event(3))
    assert result["latest"]["fills"]
    # Identical retries work after their original freshness window has expired.
    set_clock(monkeypatch, "2020-01-05T12:00:00+00:00")
    assert paper.ingest_book(path, "b", bar_event(3)) == result
    with pytest.raises(ContractError, match="stale"):
        paper.ingest_book(path, "b", bar_event(4))


@pytest.mark.parametrize("stamp", ["2020-01-01T13:00:00+00:00", "2020-01-01T10:00:00+00:00",
                                  "2019-12-31T23:59:00+00:00", "2020-01-01T12:00:00"])
def test_forward_rejects_invalid_signal_time(tmp_path, monkeypatch, stamp):
    set_clock(monkeypatch, "2020-01-01T11:00:00+00:00")
    path = tmp_path / "forward.sqlite"
    paper.create_book(path, "b", make_paper_config("forward_shadow"))
    set_clock(monkeypatch, "2020-01-01T12:01:00+00:00")
    with pytest.raises(ContractError):
        paper.ingest_book(path, "b", signal_event(stamp))
    assert paper.book_status(path, "b")["event_count"] == 0


def test_future_historical_replay_is_distinct_from_forward_clock(book):
    paper.ingest_book(book, "b", signal_event("2099-01-01T12:00:00+00:00"))
    event = bar_event(2)
    event["window_start"] = "2099-01-02T00:00:00+00:00"
    event["window_end"] = "2099-01-03T00:00:00+00:00"
    event["bars"][0]["day"] = "2099-01-02"
    assert paper.ingest_book(book, "b", event)["mode"] == "historical_replay"


def test_replay_accepts_new_signal_without_changing_prior_output(book):
    paper.ingest_book(book, "b", signal_event())
    prior = paper.ingest_book(book, "b", bar_event(2))
    paper.ingest_book(book, "b", signal_event("2020-01-02T20:00:00+00:00", "signal-2", score=2))
    result = paper.ingest_book(book, "b", bar_event(3))
    assert result["daily"][0] == prior["daily"][0]


def test_bars_need_previously_archived_signals(book):
    with pytest.raises(ContractError, match="archive signals"):
        paper.ingest_book(book, "b", bar_event(2))


@pytest.mark.parametrize("changes", [{"mode": "live"}, {"execution_policy": {}},
                                     {"allocation": {"s": 2}}, {"initial_cash": float("nan")}])
def test_bad_config(tmp_path, changes):
    config = make_paper_config()
    config.update(changes)
    with pytest.raises(ContractError):
        paper.create_book(tmp_path / "bad.sqlite", "b", config)


def test_engine_change_refuses_replay(book, monkeypatch):
    monkeypatch.setattr(paper, "_engine", lambda: {"different": "version"})
    with pytest.raises(ContractError, match="engine hash"):
        paper.book_status(book, "b")


def test_runner_and_module_cli(tmp_path, capsys):
    request = {"action": "create", "db_path": str(tmp_path / "runner.sqlite"), "book_id": "r", "config": make_paper_config()}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    paper.main(["--request", str(path)])
    result = json.loads(capsys.readouterr().out)
    assert result["paper_only"] is True
    request.pop("config")
    request["action"] = "status"
    assert paper.run_paper_trading(request) == result


def test_concurrent_retry_commits_once(book):
    from concurrent.futures import ThreadPoolExecutor

    paper.ingest_book(book, "b", signal_event())
    paper.ingest_book(book, "b", bar_event(2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(paper.ingest_book, book, "b", bar_event(3)) for _ in range(2)]
        results = [future.result() for future in futures]
    assert results[0] == results[1]
    assert results[0]["event_count"] == 3
    assert len(results[0]["latest"]["fills"]) == 1


def test_engine_stops_and_borrow_costs_survive_daily_replay(tmp_path):
    path = tmp_path / "short.sqlite"
    config = make_paper_config()
    config["strategies"]["s"]["side"] = "short_only"
    paper.create_book(path, "b", config)
    paper.ingest_book(path, "b", signal_event())
    paper.ingest_book(path, "b", bar_event(2))
    paper.ingest_book(path, "b", bar_event(3))
    result = paper.ingest_book(path, "b", bar_event(4, high=120))
    assert any(fill["reason"] == "stop_loss" for fill in result["latest"]["fills"])
    assert result["latest"]["cumulative_borrow_cost"] > 0
    assert paper.book_status(path, "b") == result


def test_invalid_daily_windows_and_duplicate_assets(book):
    paper.ingest_book(book, "b", signal_event())
    event = bar_event(2)
    event["window_end"] = "2020-01-02T12:00:00+00:00"
    with pytest.raises(ContractError, match="complete UTC day"):
        paper.ingest_book(book, "b", event)
    event = bar_event(2)
    event["bars"].append(deepcopy(event["bars"][0]))
    with pytest.raises(ContractError, match="unique assets"):
        paper.ingest_book(book, "b", event)
    assert paper.book_status(book, "b")["event_count"] == 1


def test_forward_rejects_previous_day_signal_even_with_long_age_limit(tmp_path, monkeypatch):
    config = make_paper_config("forward_shadow")
    config["max_signal_age_seconds"] = 86400
    path = tmp_path / "forward.sqlite"
    set_clock(monkeypatch, "2020-01-01T11:00:00+00:00")
    paper.create_book(path, "b", config)
    set_clock(monkeypatch, "2020-01-02T00:01:00+00:00")
    with pytest.raises(ContractError, match="stale"):
        paper.ingest_book(path, "b", signal_event("2020-01-01T23:59:00+00:00"))


def test_forward_rejects_creation_day_backfill(tmp_path, monkeypatch):
    path = tmp_path / "forward.sqlite"
    set_clock(monkeypatch, "2020-01-01T11:00:00+00:00")
    paper.create_book(path, "b", make_paper_config("forward_shadow"))
    set_clock(monkeypatch, "2020-01-01T12:01:00+00:00")
    paper.ingest_book(path, "b", signal_event())
    set_clock(monkeypatch, "2020-01-02T00:01:00+00:00")
    with pytest.raises(ContractError, match="backfilled"):
        paper.ingest_book(path, "b", bar_event(1))
