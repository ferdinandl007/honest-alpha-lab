"""Independent, durable trade approval boundary. No LLM receives operator authority."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .contracts import ContractError, canonical_hash
from .webull_paper import LIVE_ARMING, SandboxOrderJournal, WebullHKClient


def _now():
    return datetime.now(UTC).timestamp()


class TradeControl:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS control_books(id TEXT PRIMARY KEY, policy TEXT NOT NULL, revision TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trade_proposals(id TEXT PRIMARY KEY,book TEXT NOT NULL,revision TEXT NOT NULL,
              intent TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,
              reserved_day TEXT,notional REAL NOT NULL,result TEXT);
            CREATE TABLE IF NOT EXISTS control_events(seq INTEGER PRIMARY KEY,at REAL,kind TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS control_flags(id INTEGER PRIMARY KEY CHECK(id=1),halted INTEGER NOT NULL);
            INSERT OR IGNORE INTO control_flags VALUES(1,1);
            """)

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def _event(self, db, kind, payload):
        db.execute("INSERT INTO control_events(at,kind,payload) VALUES(?,?,?)", (_now(), kind, json.dumps(payload)))

    def configure_book(self, book_id, policy):
        required = {"mode", "approval_required", "account_id", "symbols", "max_order_notional", "max_daily_notional"}
        if set(policy) != required or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", book_id):
            raise ContractError("book needs its exact mode, approval, account, symbol and risk policy")
        if policy["mode"] not in {"shadow", "paper", "live"} or type(policy["approval_required"]) is not bool:
            raise ContractError("invalid execution mode")
        if (not isinstance(policy["account_id"], str) or not policy["account_id"]
                or not isinstance(policy["symbols"], list) or not policy["symbols"]
                or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,19}", s) for s in policy["symbols"])):
            raise ContractError("account and allowed US equity symbols must be explicit")
        for key in ("max_order_notional", "max_daily_notional"):
            if type(policy[key]) not in {int, float} or not math.isfinite(policy[key]) or policy[key] <= 0:
                raise ContractError("notional limits must be finite positive amounts")
        if policy["max_order_notional"] > policy["max_daily_notional"]:
            raise ContractError("order cap cannot exceed daily cap")
        self._armed(policy)
        encoded = json.dumps(policy, sort_keys=True)
        revision = canonical_hash({"policy": policy, "revision_nonce": uuid4().hex})
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO control_books VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET policy=excluded.policy,revision=excluded.revision",
                       (book_id, encoded, revision))
            self._event(db, "book_configured", {"book": book_id, "revision": revision})
        return revision

    @staticmethod
    def _armed(policy):
        if policy["mode"] == "live":
            if os.environ.get("HAL_ENABLE_LIVE_TRADING") != LIVE_ARMING:
                raise ContractError("live mode requires deployment-level arming")
            if not policy["approval_required"] and os.environ.get("HAL_ENABLE_AUTO_LIVE") != "YES_I_ACCEPT_UNATTENDED_ORDERS":
                raise ContractError("unattended live mode requires separate deployment arming")

    def halt(self, halted):
        if type(halted) is not bool:
            raise ContractError("halt must be boolean")
        with self._db() as db:
            db.execute("UPDATE control_flags SET halted=? WHERE id=1", (int(halted),))
            self._event(db, "halt_changed", {"halted": halted})

    def propose(self, proposal):
        fields = {"id", "book", "symbol", "side", "quantity", "limit_price", "reference_price", "quote_at", "rationale"}
        if set(proposal) != fields or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", proposal["id"]):
            raise ContractError("proposal needs exact typed order, quote and rationale fields")
        if proposal["side"] not in {"BUY", "SELL"} or not isinstance(proposal["rationale"], str) or len(proposal["rationale"]) > 4000:
            raise ContractError("invalid proposal side or rationale")
        for key in ("quantity", "limit_price", "reference_price"):
            if type(proposal[key]) not in {int, float} or not math.isfinite(proposal[key]) or proposal[key] <= 0:
                raise ContractError("order values must be finite and positive")
        if int(proposal["quantity"]) != proposal["quantity"]:
            raise ContractError("whole-share orders only")
        stamp = datetime.fromisoformat(proposal["quote_at"])
        if stamp.utcoffset() is None or not 0 <= _now() - stamp.timestamp() <= 60:
            raise ContractError("proposal reference quote must be fresh within 60 seconds")
        if abs(proposal["limit_price"] / proposal["reference_price"] - 1) > .02:
            raise ContractError("limit price exceeds the 2 percent reference-price collar")
        notional = proposal["quantity"] * proposal["limit_price"]
        encoded, digest = json.dumps(proposal, sort_keys=True), canonical_hash(proposal)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT digest FROM trade_proposals WHERE id=?", (proposal["id"],)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise ContractError("proposal ID cannot change its intent")
                return proposal["id"]
            row = db.execute("SELECT policy,revision FROM control_books WHERE id=?", (proposal["book"],)).fetchone()
            if row is None:
                raise ContractError("unknown book")
            policy = json.loads(row[0])
            if proposal["symbol"] not in policy["symbols"] or notional > policy["max_order_notional"]:
                raise ContractError("proposal violates book symbol or order risk limit")
            db.execute("INSERT INTO trade_proposals VALUES(?,?,?,?,?,?,?,?,NULL,?,NULL)",
                       (proposal["id"], proposal["book"], row[1], encoded, digest, "pending", _now(), _now()+300, notional))
            self._event(db, "proposed", {"id": proposal["id"], "digest": digest})
        return proposal["id"]

    def review(self, proposal_id, *, approve):
        if type(approve) is not bool:
            raise ContractError("approval must be explicit boolean")
        with self._db() as db:
            cursor = db.execute("UPDATE trade_proposals SET state=? WHERE id=? AND state='pending' AND expires>?",
                                ("approved" if approve else "rejected", proposal_id, _now()))
            if cursor.rowcount != 1:
                raise ContractError("proposal is missing, expired or already reviewed")
            self._event(db, "approved" if approve else "rejected", {"id": proposal_id})

    def dispatch(self, proposal_id, *, client_factory=WebullHKClient):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT halted FROM control_flags WHERE id=1").fetchone()[0]:
                raise ContractError("trading is halted")
            row = db.execute("SELECT book,revision,intent,state,expires,notional FROM trade_proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise ContractError("unknown proposal")
            book, revision, encoded, state, expires, notional = row
            policy_json, current_revision = db.execute("SELECT policy,revision FROM control_books WHERE id=?", (book,)).fetchone()
            policy, intent = json.loads(policy_json), json.loads(encoded)
            self._armed(policy)
            if revision != current_revision or expires <= _now():
                raise ContractError("proposal expired or book policy changed; propose again")
            if state != "approved" and not (state == "pending" and not policy["approval_required"]):
                raise ContractError("proposal needs approval or was already dispatched")
            if _now() - datetime.fromisoformat(intent["quote_at"]).timestamp() > 60:
                raise ContractError("quote is stale; create a fresh proposal for review")
            day = datetime.now(UTC).date().isoformat()
            spent = db.execute("SELECT COALESCE(SUM(notional),0) FROM trade_proposals WHERE book=? AND reserved_day=?", (book, day)).fetchone()[0]
            if spent + notional > policy["max_daily_notional"]:
                raise ContractError("daily gross order notional budget exhausted")
            db.execute("UPDATE trade_proposals SET state='unknown',reserved_day=? WHERE id=?", (day, proposal_id))
            self._event(db, "dispatch_reserved", {"id": proposal_id, "mode": policy["mode"]})
        if policy["mode"] == "shadow":
            result = {"state": "shadow_recorded", "note": "Intent only; fills require the separate paper book."}
        else:
            mode = "sandbox" if policy["mode"] == "paper" else "live"
            client = client_factory(mode=mode)
            if mode == "live":
                quotes = client.snapshot(intent["symbol"])
                matching = [q for q in quotes if isinstance(q, dict) and q.get("symbol") == intent["symbol"]] if isinstance(quotes, list) else []
                if len(matching) != 1:
                    raise ContractError("live execution requires a fresh broker market quote")
                try:
                    price = float(matching[0]["price"])
                    quote_time = float(matching[0]["last_trade_time"]) / 1000
                    if (not math.isfinite(price) or price <= 0 or not 0 <= _now()-quote_time <= 60
                            or abs(intent["limit_price"]/price-1) > .02):
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    raise ContractError("broker quote failed freshness or limit-price collar") from None
            journal = SandboxOrderJournal(str(self.path) + ".orders.sqlite")
            result = journal.submit(client, account_id=policy["account_id"],
                client_order_id=proposal_id, symbol=intent["symbol"], side=intent["side"],
                quantity=intent["quantity"], limit_price=intent["limit_price"],
                max_notional=policy["max_order_notional"], enable_sandbox_orders=mode == "sandbox", enable_live_orders=mode == "live")
        with self._db() as db:
            db.execute("UPDATE trade_proposals SET state=?,result=? WHERE id=?", (result["state"], json.dumps(result), proposal_id))
            self._event(db, "dispatch_response", {"id": proposal_id, "state": result["state"]})
        return result

    def dispatch_ready(self):
        """Trusted executor only; proposers cannot invoke this through their token."""
        snapshot = self.status()
        policies = {book["id"]: book for book in snapshot["books"]}
        results = []
        for proposal in reversed(snapshot["proposals"]):
            policy = policies[proposal["book"]]
            if proposal["state"] == "approved" or proposal["state"] == "pending" and not policy["approval_required"]:
                try:
                    result = self.dispatch(proposal["id"])
                except ContractError as exc:
                    result = {"error": str(exc)}
                results.append({"id": proposal["id"], "result": result})
        return results

    def status(self):
        with self._db() as db:
            books = [{"id": row[0], **json.loads(row[1]), "revision": row[2]} for row in db.execute("SELECT * FROM control_books ORDER BY id")]
            proposals = [{**json.loads(row[0]), "state": row[1], "expires_at": row[2]} for row in db.execute("SELECT intent,state,expires FROM trade_proposals ORDER BY created DESC LIMIT 200")]
            return {"halted": bool(db.execute("SELECT halted FROM control_flags WHERE id=1").fetchone()[0]),
                    "live_armed": os.environ.get("HAL_ENABLE_LIVE_TRADING") == LIVE_ARMING,
                    "books": books, "proposals": proposals}
