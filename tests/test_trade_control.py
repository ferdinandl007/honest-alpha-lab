"""No broker calls: authorization, risk and crash-boundary fixtures."""
import json
import threading
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from honest_alpha_lab.console_server import handler_factory
from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.trade_control import TradeControl
from honest_alpha_lab.webull_paper import (
    DATA_HOST,
    LIVE_ARMING,
    PLACE,
    SANDBOX_HOST,
    SandboxOrderJournal,
    WebullHKClient,
)


def policy(**changes):
    return {"mode": "shadow", "approval_required": True, "account_id": "fixture",
            "symbols": ["AAPL"], "max_order_notional": 500, "max_daily_notional": 700, **changes}


def proposal(id="intent", **changes):
    return {"id": id, "book": "overnight", "symbol": "AAPL", "side": "BUY", "quantity": 2,
            "limit_price": 100, "reference_price": 100, "quote_at": datetime.now(UTC).isoformat(),
            "rationale": "fixture only", **changes}


def control(tmp_path, **changes):
    service = TradeControl(tmp_path / "control.sqlite")
    service.configure_book("overnight", policy(**changes))
    service.halt(False)
    return service


def test_default_halt_and_approval_and_at_most_once(tmp_path):
    service = TradeControl(tmp_path / "control.sqlite")
    assert service.status()["halted"]
    service.configure_book("overnight", policy())
    service.propose(proposal())
    with pytest.raises(ContractError, match="halted"):
        service.dispatch("intent")
    service.halt(False)
    with pytest.raises(ContractError, match="approval"):
        service.dispatch("intent")
    service.review("intent", approve=True)
    assert service.dispatch("intent")["state"] == "shadow_recorded"
    with pytest.raises(ContractError):
        service.dispatch("intent")


def test_mode_switch_back_does_not_resurrect_approval(tmp_path):
    service = control(tmp_path)
    service.propose(proposal())
    service.review("intent", approve=True)
    service.configure_book("overnight", policy(mode="paper"))
    service.configure_book("overnight", policy())
    with pytest.raises(ContractError, match="policy changed"):
        service.dispatch("intent")


def test_auto_daily_cap_and_changed_ids(tmp_path):
    service = control(tmp_path, approval_required=False, max_daily_notional=500)
    for index in range(3):
        service.propose(proposal(str(index)))
    assert service.dispatch("0")["state"] == "shadow_recorded"
    service.dispatch("1")
    with pytest.raises(ContractError, match="daily"):
        service.dispatch("2")
    with pytest.raises(ContractError, match="cannot change"):
        service.propose(proposal("2", quantity=1))


def test_ready_queue_does_not_use_truncated_display_history(tmp_path):
    service = control(tmp_path)
    service.propose(proposal("old-approved", side="SELL"))
    service.review("old-approved", approve=True)
    for index in range(200):
        service.propose(proposal(f"new-pending-{index}"))
    assert "old-approved" not in {p["id"] for p in service.status()["proposals"]}
    results = service.dispatch_ready()
    assert results == [{"id": "old-approved", "result": {
        "state": "shadow_recorded", "note": "Intent only; fills require the separate paper book."
    }}]


def test_ready_batch_filters_ineligible_orders_before_limit(tmp_path):
    service = control(tmp_path, max_daily_notional=100000)
    for index in range(205):
        service.propose(proposal(f"ready-{index}"))
        service.review(f"ready-{index}", approve=True)
    first = service.dispatch_ready()
    second = service.dispatch_ready()
    assert len(first) == 200 and len(second) == 5
    assert first[0]["id"] == "ready-0"
    assert len({r["id"] for r in first + second}) == 205
    assert service.dispatch_ready() == []


def test_stale_quotes_and_policy_revisions_cannot_starve_ready_batch(tmp_path, monkeypatch):
    import honest_alpha_lab.trade_control as module

    now = datetime.now(UTC).timestamp()
    monkeypatch.setattr(module, "_now", lambda: now)
    service = control(tmp_path, max_daily_notional=100000)
    for index in range(201):
        service.propose(proposal(f"stale-{index}", quote_at=datetime.fromtimestamp(now, UTC).isoformat()))
        service.review(f"stale-{index}", approve=True)
    now += 61
    service.propose(proposal("fresh", quote_at=datetime.fromtimestamp(now, UTC).isoformat()))
    service.review("fresh", approve=True)
    assert [r["id"] for r in service.dispatch_ready()] == ["fresh"]


def test_approval_authorizes_dispatch_after_resume(tmp_path):
    service = control(tmp_path)
    service.halt(True)
    service.propose(proposal())
    service.review("intent", approve=True)
    assert "error" in service.dispatch_ready()[0]["result"]
    service.halt(False)
    assert service.dispatch_ready()[0]["result"]["state"] == "shadow_recorded"


def test_exhausted_book_cannot_starve_another_book(tmp_path):
    service = control(tmp_path, approval_required=False, max_daily_notional=500)
    service.propose(proposal("exhaust", quantity=5))
    service.dispatch("exhaust")
    for index in range(200):
        service.propose(proposal(f"over-budget-{index}"))
    service.configure_book("other", policy(approval_required=False))
    service.propose(proposal("other-ready", book="other"))
    assert [r["id"] for r in service.dispatch_ready()] == ["other-ready"]
    assert service.dispatch_ready() == []


def test_unarmed_book_cannot_starve_shadow_book(tmp_path, monkeypatch):
    service = control(tmp_path)
    monkeypatch.setenv("HAL_ENABLE_LIVE_TRADING", LIVE_ARMING)
    service.configure_book("live-fixture", policy(mode="live"))
    for index in range(200):
        service.propose(proposal(f"live-{index}", book="live-fixture"))
        service.review(f"live-{index}", approve=True)
    monkeypatch.delenv("HAL_ENABLE_LIVE_TRADING")
    service.propose(proposal("shadow-ready"))
    service.review("shadow-ready", approve=True)
    assert [r["id"] for r in service.dispatch_ready()] == ["shadow-ready"]


@pytest.mark.parametrize("changes", [{"limit_price": 200}, {"symbol": "MSFT"}, {"quantity": .5}, {"quantity": float('nan')}, {"quantity": 10}, {"quote_at": "2000-01-01T00:00:00Z"}])
def test_invalid_risk_proposals(tmp_path, changes):
    with pytest.raises(ContractError):
        control(tmp_path).propose(proposal(**changes))


def test_live_and_unattended_require_separate_deployment_gates(tmp_path, monkeypatch):
    monkeypatch.delenv("HAL_ENABLE_LIVE_TRADING", raising=False)
    with pytest.raises(ContractError, match="deployment"):
        control(tmp_path, mode="live")
    monkeypatch.setenv("HAL_ENABLE_LIVE_TRADING", LIVE_ARMING)
    monkeypatch.delenv("HAL_ENABLE_AUTO_LIVE", raising=False)
    with pytest.raises(ContractError, match="unattended"):
        control(tmp_path, mode="live", approval_required=False)


def test_broker_hosts_credentials_and_unknown_submission_are_separate(tmp_path, monkeypatch):
    for suffix in ("APP_KEY", "APP_SECRET", "ACCESS_TOKEN"):
        monkeypatch.setenv("HAL_WEBULL_SANDBOX_"+suffix, "fixture-not-a-real-credential")
    calls = []
    def transport(*args):
        calls.append(args)
        raise TimeoutError("sensitive should not leak")
    client = WebullHKClient(transport=transport)
    journal = SandboxOrderJournal(tmp_path / "orders.sqlite")
    kwargs = {"account_id":"fixture", "client_order_id":"same", "symbol":"AAPL", "side":"BUY", "quantity":1,
              "limit_price":100, "enable_sandbox_orders":True}
    with pytest.raises(ContractError, match="transport failed"):
        journal.submit(client, **kwargs)
    assert journal.submit(client, **kwargs)["state"] == "unknown"
    assert len(calls) == 1 and calls[0][1] == SANDBOX_HOST
    assert "x-app-secret" not in calls[0][-1]
    with pytest.raises(ContractError):
        WebullHKClient(mode="data_only")._request("POST", PLACE, payload={})
    assert WebullHKClient(mode="data_only").host == DATA_HOST


def test_console_separates_proposer_operator_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_CONSOLE_TOKEN", "o"*40)
    monkeypatch.setenv("HAL_PROPOSAL_TOKEN", "p"*40)
    service = control(tmp_path)
    handler = handler_factory(service, static_root=tmp_path, paper_db=tmp_path / "paper.sqlite", origin="http://trusted")
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/api/"
        def call(route, token, payload=None, origin="http://trusted"):
            return urlopen(Request(url+route, data=json.dumps(payload).encode() if payload else None,
                headers={"Authorization": "Bearer "+token, "Content-Type":"application/json", "Origin":origin}), timeout=3)
        try:
            with pytest.raises(HTTPError) as failure:
                call("status", "p"*40)
            assert failure.value.code == 401
            assert call("proposals", "p"*40, proposal()).status == 200
            with pytest.raises(HTTPError) as failure:
                call("review", "p"*40, {"id":"intent", "approve":True})
            assert failure.value.code == 401
            with pytest.raises(HTTPError) as failure:
                call("halt", "o"*40, {"halted":False}, origin="https://evil.example")
            assert failure.value.code == 403
            assert call("review", "o"*40, {"id":"intent", "approve":True}).status == 200
        finally:
            server.shutdown()
            thread.join()
