"""Loopback/private-network console with separate proposer and operator authority.

Serve behind a private TLS reverse proxy such as Tailscale Serve. No cookies,
credential URLs, public CORS, arbitrary filesystem paths, or broker keys in UI.
"""
from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .contracts import ContractError
from .paper_trading import book_status
from .trade_control import TradeControl


def handler_factory(control, *, static_root, paper_db, origin):
    root = Path(static_root).resolve()
    token = os.environ.get("HAL_CONSOLE_TOKEN", "")
    proposer = os.environ.get("HAL_PROPOSAL_TOKEN", "")
    if len(token) < 32 or proposer and (len(proposer) < 32 or proposer == token):
        raise ContractError("configure distinct private tokens of at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def log_message(self, *_):
            pass  # No authorization or query strings in ordinary access logs.

        def reply(self, code, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self, proposal_only=False):
            supplied = self.headers.get("Authorization", "")
            operator = hmac.compare_digest(supplied, "Bearer " + token)
            agent = proposal_only and proposer and hmac.compare_digest(supplied, "Bearer " + proposer)
            if not (operator or agent):
                self.reply(401, {"error": "Operator authentication required"})
                return False
            if self.headers.get("Origin") not in {None, origin}:
                self.reply(403, {"error": "Cross-origin requests are not allowed"})
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path)
            if path.path.startswith("/api/"):
                if not self.authorized():
                    return
                try:
                    if path.path == "/api/status":
                        return self.reply(200, control.status())
                    if path.path == "/api/paper-status":
                        book = parse_qs(path.query).get("book", [""])[0]
                        return self.reply(200, book_status(paper_db, book))
                    return self.reply(404, {"error": "Unknown endpoint"})
                except (ContractError, ValueError):
                    return self.reply(400, {"error": "Book not available or invalid request"})
                except Exception:  # noqa: BLE001 -- redact storage failures at the HTTP boundary
                    return self.reply(500, {"error": "Unable to read the private ledger"})
            target = (root / (path.path.lstrip("/") or "index.html")).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                return self.reply(404, {"error": "Build the web console before serving it"})
            types = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".json": "application/json"}
            return self.reply(200, target.read_bytes(), types.get(target.suffix, "application/octet-stream"))

        def do_POST(self):
            path = urlsplit(self.path).path
            if not self.authorized(proposal_only=path == "/api/proposals"):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ContractError("A bounded JSON request is required")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ContractError("Request must be an object")
                if path == "/api/proposals":
                    result = {"id": control.propose(request)}
                elif path == "/api/books":
                    result = {"revision": control.configure_book(request["id"], request["policy"])}
                elif path == "/api/halt":
                    control.halt(request["halted"])
                    result = {"ok": True}
                elif path == "/api/review":
                    control.review(request["id"], approve=request["approve"])
                    result = {"ok": True}
                elif path == "/api/dispatch":
                    result = control.dispatch(request["id"])
                else:
                    return self.reply(404, {"error": "Unknown endpoint"})
                return self.reply(200, result)
            except ContractError as exc:
                return self.reply(400, {"error": str(exc)})
            except (ValueError, KeyError, TypeError):
                return self.reply(400, {"error": "Invalid request fields"})
            except Exception:  # noqa: BLE001 -- redact unexpected HTTP-boundary failures
                return self.reply(500, {"error": "Operation failed; inspect journal before retrying"})

    return Handler


def serve_console(*, state, paper_db, static_root, port=8787, origin=None):
    origin = origin or f"http://127.0.0.1:{port}"
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
        raise ContractError("origin must be an exact HTTP(S) origin")
    handler = handler_factory(TradeControl(state), static_root=static_root, paper_db=paper_db, origin=origin)
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print(f"Private console listening on http://127.0.0.1:{port}", flush=True)
        server.serve_forever()
