"""Loopback-only UI fixture. No trading imports, credentials, or external calls.

Run from the repository root after building web: python tests/manual_approval_fixture.py
The access field can contain any fixture text. All authorization is in memory.
"""
import json
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


STATE = {"halted": False, "live_armed": True, "books": [{
    "id": "fixture-only", "mode": "live", "approval_required": True,
    "account_id": "NOT-A-BROKER-ACCOUNT", "symbols": ["AAPL"],
    "max_order_notional": 500, "max_daily_notional": 1000, "revision": "fixture",
}], "proposals": [{"id": "fixture-intent", "book": "fixture-only", "symbol": "AAPL",
    "side": "BUY", "quantity": 1, "limit_price": 100, "reference_price": 100,
    "quote_at": datetime.now(UTC).isoformat(), "rationale": "UI FIXTURE ONLY — no broker connection",
    "state": "pending", "expires_at": 0}]}


class FixtureHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).resolve().parents[1] / "web/dist/client"), **kwargs)

    def reply(self, value, status=200):
        encoded = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == "/api/status":
            return self.reply(STATE)
        return super().do_GET()

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0))
        if not 0 < size < 4096:
            return self.reply({"error": "fixture request invalid"}, 400)
        body = json.loads(self.rfile.read(size))
        if self.path == "/api/review" and body.get("id") == "fixture-intent":
            STATE["proposals"][0]["state"] = "approved" if body.get("approve") is True else "rejected"
            print("FIXTURE_REVIEW", STATE["proposals"][0]["state"], flush=True)
            return self.reply({"ok": True})
        return self.reply({"error": "Fixture cannot submit orders"}, 400)


if __name__ == "__main__":
    print("UI fixture only at http://127.0.0.1:8788 — NO BROKER", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8788), FixtureHandler).serve_forever()
