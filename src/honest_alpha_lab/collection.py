"""Bounded public-document collection for exploratory agents.

This is a read-only HTTPS capability, not a browser, license approver, or generic
network proxy. Collected bytes are unapproved discovery artifacts. It uses a
fixed trusted child process; it does not sandbox arbitrary agent-written code.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import math
import multiprocessing
import re
import socket
import sqlite3
import ssl
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit
from uuid import uuid4

from .artifacts import LocalArtifactStore
from .contracts import ContractError, canonical_hash


@dataclass(frozen=True)
class CollectionPolicy:
    max_attempts: int = 20
    max_total_body_bytes: int = 20_000_000
    max_call_body_bytes: int = 1_000_000
    timeout_seconds: int = 30
    max_redirects: int = 3
    minimum_interval_seconds: float = 1.0
    user_agent: str = "HonestAlphaLabResearch/0.1"

    def __post_init__(self):
        for name in ("max_attempts", "max_total_body_bytes", "max_call_body_bytes", "timeout_seconds"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ContractError("collection budgets must be positive integers")
        if self.max_call_body_bytes > 10_000_000 or self.max_call_body_bytes > self.max_total_body_bytes:
            raise ContractError("call body limit must fit the total budget and be at most 10 MB")
        if self.timeout_seconds > 60:
            raise ContractError("collection calls must be bounded to 60 seconds")
        if type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 5:
            raise ContractError("redirect limit must be an integer from zero to five")
        if (type(self.minimum_interval_seconds) not in (float, int)
                or not math.isfinite(self.minimum_interval_seconds) or self.minimum_interval_seconds < 1):
            raise ContractError("public collection needs at least a one-second request interval")
        if not isinstance(self.user_agent, str) or not re.fullmatch(r"HonestAlphaLabResearch/[0-9.]+", self.user_agent):
            raise ContractError("use the identifiable research user agent; do not impersonate a browser")


def public_url(url):
    """Only explicit HTTPS documents; no credentials, fragments or opaque ports."""
    if (not isinstance(url, str) or len(url) > 4096 or not url.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url):
        raise ContractError("invalid public document URL")
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.port not in (None, 443)
                or parts.username is not None or parts.password is not None or parts.fragment):
            raise ContractError("only unauthenticated HTTPS URLs on port 443 are supported")
        host = parts.hostname.lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or ".." in host:
            raise ContractError("invalid public DNS hostname")
        if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local", ".internal")):
            raise ContractError("local/internal hosts are prohibited")
        for key, _ in parse_qsl(parts.query, keep_blank_values=True):
            if re.search(r"token|secret|password|api.?key|authorization|signature|credential", key, re.IGNORECASE):
                raise ContractError("credential-bearing URLs are not supported")
        path = parts.path or "/"
        if any(ord(c) < 32 or ord(c) == 127 for c in unquote(path + "?" + parts.query)):
            raise ContractError("encoded control characters are prohibited")
        return urlunsplit(("https", host, path, parts.query, ""))
    except ValueError as exc:
        raise ContractError("invalid HTTPS URL") from exc


def public_addresses(host):
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    if not addresses:
        raise ContractError("host has no addresses")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if (not address.is_global or address.is_multicast or address.is_reserved
                or isinstance(address, ipaddress.IPv6Address) and (address.ipv4_mapped or address.sixtofour or address.teredo)):
            raise ContractError("host resolves to a nonpublic or translated address")
    return addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the validated numeric IP, not a second DNS resolution. TLS
        # still authenticates the original DNS host (SNI and certificate name).
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class CollectionBudget:
    """Durable reserve-before-network budget. Crashed reservations remain spent.

    Local SQLite is a single-host service store, not an adversarial authorization
    boundary. Keep it and the artifact roots outside researcher write access.
    """

    def __init__(self, path, budget_id, policy: CollectionPolicy):
        self.path = str(Path(path).resolve())
        self.budget_id, self.policy = budget_id, policy
        if not isinstance(budget_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", budget_id):
            raise ContractError("invalid collection budget identifier")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS budgets (id TEXT PRIMARY KEY, policy_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, budget_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                    bytes_charged INTEGER NOT NULL, status TEXT NOT NULL, manifest_hash TEXT);
                CREATE TABLE IF NOT EXISTS host_schedule (host TEXT PRIMARY KEY, next_at REAL NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO budgets VALUES (?, ?)", (budget_id, canonical_hash(policy)))
            if db.execute("SELECT policy_hash FROM budgets WHERE id=?", (budget_id,)).fetchone()[0] != canonical_hash(policy):
                raise ContractError("collection budget policy is immutable")

    def reserve(self, request_hash):
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("BEGIN IMMEDIATE")
            count, used = db.execute("SELECT COUNT(*), COALESCE(SUM(bytes_charged),0) FROM attempts WHERE budget_id=?",
                                    (self.budget_id,)).fetchone()
            if count >= self.policy.max_attempts or used + self.policy.max_call_body_bytes > self.policy.max_total_body_bytes:
                raise PermissionError("collection budget exhausted")
            attempt_id = str(uuid4())
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, 'reserved', NULL)",
                       (attempt_id, self.budget_id, request_hash, self.policy.max_call_body_bytes))
            return attempt_id

    def finish(self, attempt_id, *, actual_bytes, status, manifest_hash):
        if type(actual_bytes) is not int or not 0 <= actual_bytes <= self.policy.max_call_body_bytes:
            raise ContractError("invalid measured collection byte usage")
        with sqlite3.connect(self.path) as db:
            changed = db.execute("UPDATE attempts SET bytes_charged=?, status=?, manifest_hash=? WHERE id=? AND budget_id=? AND status='reserved'",
                                 (actual_bytes, status, manifest_hash, attempt_id, self.budget_id)).rowcount
            if changed != 1:
                raise ContractError("attempt is missing or already settled")


def _pace(path, host, interval, deadline):
    with sqlite3.connect(path, timeout=2) as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT next_at FROM host_schedule WHERE host=?", (host,)).fetchone()
        now = time.time()
        delay = max(0, prior[0] - now) if prior else 0
        if time.monotonic() + delay >= deadline:
            raise ContractError("host pacing exceeds this call deadline; retry in a later budgeted call")
        db.execute("INSERT INTO host_schedule VALUES (?, ?) ON CONFLICT(host) DO UPDATE SET next_at=excluded.next_at",
                   (host, now + delay + interval))
    if delay:
        time.sleep(delay)


def _robots(text, url):
    """Conservative robots rules: merged best agent group, longest rule, Allow tie.

    Nonstandard pacing directives are deliberately refused for now, rather than
    silently ignored. Robots permission is not a data license or access grant.
    """
    groups, agents, rules = [], [], []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        name, value = (item.strip() for item in line.split(":", 1))
        name = name.lower()
        if name == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif agents and name in {"allow", "disallow", "crawl-delay", "request-rate"}:
            rules.append((name, value))
    groups.append((agents, rules))
    selected, specificity = [], -1
    for agents, rules in groups:
        scores = [0 if a == "*" else len(a) for a in agents if a == "*" or a in "honestalphalabresearch"]
        score = max(scores, default=-1)
        if score > specificity:
            selected, specificity = list(rules), score
        elif score == specificity and score >= 0:
            selected.extend(rules)
    target = urlsplit(url).path or "/"
    if urlsplit(url).query:
        target += "?" + urlsplit(url).query
    # Compare unreserved percent escapes consistently; preserve encoded reserved
    # delimiters. Non-ASCII rules become UTF-8 percent octets.
    from urllib.parse import quote

    def normalize(value):
        value = quote(value, safe="/%*?$&=:+,;@!~'()-._")
        return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m[1], 16))
                      if chr(int(m[1], 16)) in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
                      else "%" + m[1].upper(), value)

    matched = []
    for name, value in selected:
        if name in {"crawl-delay", "request-rate"}:
            raise ContractError("site has custom pacing directives requiring a reviewed provider adapter")
        if not value:
            continue
        value = normalize(value)
        anchored = value.endswith("$")
        pattern = re.escape(value[:-1] if anchored else value).replace(r"\*", ".*")
        if re.match("^" + pattern + ("$" if anchored else ""), normalize(target)):
            matched.append((len(value.replace("*", "").rstrip("$")), name == "allow"))
    return max(matched, default=(0, True))[1]


def _retrieve(url, policy, budget_path):
    """Runs only in the bounded worker. Returns bodies, never executable content."""
    deadline = time.monotonic() + policy.timeout_seconds
    remaining, responses, robots_checked = policy.max_call_body_bytes, [], {}

    def request(target):
        nonlocal remaining
        target = public_url(target)
        parts = urlsplit(target)
        addresses = public_addresses(parts.hostname)
        _pace(budget_path, parts.hostname, policy.minimum_interval_seconds, deadline)
        timeout = deadline - time.monotonic()
        if timeout <= 0 or remaining <= 0:
            raise ContractError("collection deadline/body budget exhausted")
        connection = _PinnedHTTPS(parts.hostname, addresses[0], timeout)
        try:
            connection.request("GET", urlunsplit(("", "", parts.path, parts.query, "")),
                               headers={"User-Agent": policy.user_agent, "Accept-Encoding": "identity", "Connection": "close"})
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()
                       if key.lower() in {"content-type", "content-length", "content-encoding", "location", "etag", "last-modified", "date"}}
            if headers.get("content-encoding", "identity").lower() != "identity":
                raise ContractError("compressed responses need a reviewed bounded decoding adapter")
            if "content-length" in headers and int(headers["content-length"]) > remaining:
                raise ContractError("response exceeds reserved body budget")
            body = response.read(remaining + 1)
            if len(body) > remaining:
                raise ContractError("response exceeds reserved body budget")
            remaining -= len(body)
            record = {"url": target, "status": response.status, "headers": headers, "body": body,
                      "retrieved_at": datetime.now(UTC).isoformat(), "peer_ip": addresses[0]}
            responses.append(record)
            return record
        finally:
            connection.close()

    current = public_url(url)
    for _ in range(policy.max_redirects + 1):
        origin = "https://" + urlsplit(current).hostname
        if origin not in robots_checked:
            robots = request(origin + "/robots.txt")
            # Refuse redirects or failures on the robots control document: do
            # not crawl speculatively when access policy could not be obtained.
            if robots["status"] == 404:
                robots_checked[origin] = ""
            elif robots["status"] == 200:
                robots_checked[origin] = robots["body"].decode("utf-8", errors="strict")
            else:
                raise ContractError("robots policy could not be established")
        if not _robots(robots_checked[origin], current):
            raise PermissionError("robots policy disallows collection")
        response = request(current)
        if response["status"] in {301, 302, 303, 307, 308}:
            if "location" not in response["headers"]:
                raise ContractError("redirect missing location")
            current = public_url(urljoin(current, response["headers"]["location"]))
            continue
        if response["status"] != 200:
            raise ContractError(f"public source returned HTTP {response['status']}; no bypass or automatic retry")
        media = response["headers"].get("content-type", "").split(";", 1)[0].strip().lower()
        if media not in {"text/plain", "text/html", "text/csv", "application/json", "application/xml", "text/xml", "application/pdf"}:
            raise ContractError("response type requires a reviewed provider adapter")
        return {"responses": responses, "final_url": current,
                "body_bytes": policy.max_call_body_bytes - remaining}
    raise ContractError("redirect limit exceeded")


def _collection_child(sender, url, policy, budget_path):
    try:
        sender.send({"ok": True, "result": _retrieve(url, policy, budget_path)})
    except Exception as exc:  # noqa: BLE001 -- redacted trusted-worker boundary
        sender.send({"ok": False, "error_type": type(exc).__name__})
    finally:
        sender.close()


class PublicDocumentCollectionTool:
    def __init__(self, budget: CollectionBudget, artifacts: LocalArtifactStore):
        self.budget, self.artifacts = budget, artifacts

    def execute(self, arguments):
        from .tools import ToolResult

        if not isinstance(arguments, dict) or set(arguments) != {"url"}:
            raise ContractError("collection accepts only a public URL; policy and paths are service-owned")
        url = public_url(arguments["url"])
        attempt_id = self.budget.reserve(canonical_hash({"url": url}))
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_collection_child, args=(sender, url, self.budget.policy, self.budget.path))
        manifest = {"attempt_id": attempt_id, "requested_url": url, "policy": asdict(self.budget.policy),
                    "scope": "unapproved_discovery", "independently_approved": False,
                    "financial_alpha_verified": False, "historical_availability_verified": False}
        actual_bytes = self.budget.policy.max_call_body_bytes
        try:
            process.start()
            sender.close()
            if not receiver.poll(self.budget.policy.timeout_seconds):
                manifest.update(status="failed", error_type="DeadlineExceeded")
            else:
                try:
                    message = receiver.recv()
                except EOFError:
                    message = {"ok": False, "error_type": "WorkerExited"}
                if message["ok"]:
                    result = message["result"]
                    actual_bytes = result["body_bytes"]
                    for response in result["responses"]:
                        body = response.pop("body")
                        response["body_hash"] = self.artifacts.put(body)
                        response["body_bytes"] = len(body)
                    manifest.update(status="collected", **result)
                    manifest["source_hash"] = result["responses"][-1]["body_hash"]
                else:
                    manifest.update(status="failed", error_type=message["error_type"])
        finally:
            sender.close()
            receiver.close()
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
        manifest_hash = self.artifacts.put(json.dumps(manifest, sort_keys=True, allow_nan=False).encode())
        # Failures retain the full reserved bytes, including crash/unknown usage.
        self.budget.finish(attempt_id, actual_bytes=actual_bytes, status=manifest["status"], manifest_hash=manifest_hash)
        return ToolResult({**manifest, "manifest_hash": manifest_hash})
