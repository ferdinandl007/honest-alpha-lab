"""Collection safety/cost correctness, not financial benchmark results."""
import json
import socket
import sqlite3
import time
from dataclasses import replace

import pytest

from honest_alpha_lab import collection as c
from honest_alpha_lab.artifacts import LocalArtifactStore
from honest_alpha_lab.contracts import ContractError


@pytest.mark.parametrize("url", [
    "http://example.org/", "file:///etc/passwd", "https://localhost/", "https://x.local/a",
    "https://metadata.google.internal/", "https://example.org:444/a", "https://user:pass@example.org/",
    "https://example.org/a#frag", "https://example.org/a\n", "https://example.org/%0D%0A",
    "https://example.org/?api_key=hidden", "https://example.org/?%74oken=secret",
    "https://example.org\\@127.0.0.1/", "https://example..org/a",
])
def test_disallowed_url_forms(url):
    with pytest.raises(ContractError):
        c.public_url(url)


def test_public_url_normalized():
    assert c.public_url("https://Example.ORG:443?q=shipping") == "https://example.org/?q=shipping"


@pytest.mark.parametrize("address", ["127.0.0.1", "10.2.3.4", "169.254.169.254", "192.168.1.1", "100.64.0.1", "::1", "fc00::1", "::ffff:8.8.8.8", "2002:0808:0808::1"])
def test_reject_nonpublic_dns_even_mixed_with_public(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)),
    ])
    with pytest.raises(ContractError):
        c.public_addresses("new-source.org")


def test_accept_public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    assert c.public_addresses("new-source.org") == ["8.8.8.8"]


@pytest.mark.parametrize(("rules", "path", "allowed"), [
    ("User-agent: *\nDisallow: /private", "/private/x", False),
    ("User-agent: *\nDisallow: /\nAllow: /public", "/public/x", True),
    ("User-agent: *\nDisallow: /x\nAllow: /x", "/x", True),
    ("User-agent: *\nDisallow: /*.csv$", "/data/x.csv", False),
    ("User-agent: *\nDisallow: /*.csv$", "/data/x.csv?date=1", True),
    ("User-agent: *\nDisallow: /\nUser-agent: HonestAlphaLabResearch\nAllow: /", "/a", True),
    ("User-agent: UnrelatedBot\nDisallow: /", "/a", True),
    ("User-agent: *\nDisallow: /a%62", "/ab", False),
    ("User-agent: *\nDisallow: /a%2Fb", "/a/b", True),
    ("User-agent: *\nDisallow: /", "/", False),
])
def test_robots_policy(rules, path, allowed):
    assert c._robots(rules, "https://example.org" + path) is allowed


def test_custom_robots_rate_not_silently_ignored():
    with pytest.raises(ContractError, match="pacing"):
        c._robots("User-agent: *\nCrawl-delay: 12", "https://example.org/")


def test_persistent_reservation_and_immutable_policy(tmp_path):
    path = tmp_path / "budget.sqlite"
    policy = c.CollectionPolicy(max_attempts=2, max_call_body_bytes=100, max_total_body_bytes=200)
    budget = c.CollectionBudget(path, "run", policy)
    first = budget.reserve("hash")
    # Simulated worker crash: no settlement and the full reservation stays spent.
    reopened = c.CollectionBudget(path, "run", policy)
    second = reopened.reserve("hash-2")
    assert first != second
    with pytest.raises(PermissionError):
        reopened.reserve("hash-3")
    with pytest.raises(ContractError, match="immutable"):
        c.CollectionBudget(path, "run", replace(policy, max_attempts=3))


def test_measured_success_refunds_unused_bytes_once(tmp_path):
    policy = c.CollectionPolicy(max_call_body_bytes=100, max_total_body_bytes=110)
    budget = c.CollectionBudget(tmp_path / "budget.sqlite", "run", policy)
    attempt = budget.reserve("hash")
    budget.finish(attempt, actual_bytes=10, status="collected", manifest_hash="manifest")
    with pytest.raises(ContractError):
        budget.finish(attempt, actual_bytes=0, status="collected", manifest_hash="other")
    budget.reserve("second")
    with pytest.raises(PermissionError):
        budget.reserve("third")


class Reply:
    def __init__(self, status=200, body=b"data", headers=None):
        self.status, self.body = status, body
        self.headers = headers or {"Content-Type": "text/plain"}

    def getheaders(self):
        return list(self.headers.items())

    def read(self, count):
        return self.body[:count]


def transport(monkeypatch, replies):
    requests, addresses = [], []
    monkeypatch.setattr(c, "public_addresses", lambda host: ["8.8.8.8"])
    monkeypatch.setattr(c, "_pace", lambda *args: None)

    class Connection:
        def __init__(self, host, address, timeout):
            addresses.append((host, address))

        def request(self, method, target, headers):
            requests.append((method, target, headers))

        def getresponse(self):
            return replies.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(c, "_PinnedHTTPS", Connection)
    return requests, addresses


def test_fetch_new_domain_captures_exact_bytes_no_credentials(monkeypatch):
    requests, addresses = transport(monkeypatch, [Reply(404, b""), Reply(body=b"measurement,12\n")])
    result = c._retrieve("https://new-source.org/data.csv", c.CollectionPolicy(), "unused")
    assert result["responses"][-1]["body"] == b"measurement,12\n"
    assert addresses == [("new-source.org", "8.8.8.8")] * 2
    assert all(set(headers) == {"User-Agent", "Accept-Encoding", "Connection"} for _, _, headers in requests)
    assert result["body_bytes"] == len(b"measurement,12\n")


def test_redirect_private_target_rechecked_before_connection(monkeypatch):
    requests, _ = transport(monkeypatch, [Reply(404, b""), Reply(302, b"", {"Location": "https://internal-source.org/"})])

    def addresses(host):
        if host == "internal-source.org":
            raise ContractError("nonpublic")
        return ["8.8.8.8"]

    monkeypatch.setattr(c, "public_addresses", addresses)
    with pytest.raises(ContractError, match="nonpublic"):
        c._retrieve("https://new-source.org/", c.CollectionPolicy(), "unused")
    assert len(requests) == 2


@pytest.mark.parametrize("reply", [Reply(403), Reply(200, b"x" * 101), Reply(200, b"zip", {"Content-Encoding": "gzip"}), Reply(200, b"x", {"Content-Type": "application/octet-stream"})])
def test_failures_and_oversized_bodies_do_not_bypass(monkeypatch, reply):
    transport(monkeypatch, [Reply(404, b""), reply])
    with pytest.raises(ContractError):
        c._retrieve("https://example.org/data", c.CollectionPolicy(max_call_body_bytes=100), "unused")


def test_robots_refusal_stops_before_document(monkeypatch):
    requests, _ = transport(monkeypatch, [Reply(body=b"User-agent: *\nDisallow: /")])
    with pytest.raises(PermissionError):
        c._retrieve("https://example.org/data", c.CollectionPolicy(), "unused")
    assert len(requests) == 1


def slow_child(sender, url, policy, budget_path):
    time.sleep(10)


def result_child(sender, url, policy, budget_path):
    sender.send({"ok": True, "result": {"final_url": url, "body_bytes": 4,
                 "responses": [{"url": url, "status": 200, "headers": {}, "body": b"data"}]}})
    sender.close()


@pytest.mark.parametrize("slow", [False, True])
def test_real_child_boundary_and_budgeted_timeout(tmp_path, monkeypatch, slow):
    monkeypatch.setattr(c, "_collection_child", slow_child if slow else result_child)
    policy = c.CollectionPolicy(timeout_seconds=1 if slow else 10, max_call_body_bytes=100)
    budget = c.CollectionBudget(tmp_path / "budget.sqlite", "run", policy)
    store = LocalArtifactStore(tmp_path / "artifacts")
    started = time.monotonic()
    result = c.PublicDocumentCollectionTool(budget, store).execute({"url": "https://example.org/data"}).output
    assert time.monotonic() - started < 8
    assert result["status"] == ("failed" if slow else "collected")
    assert result["independently_approved"] is False
    assert json.loads(store.get(result["manifest_hash"]))["scope"] == "unapproved_discovery"
    with sqlite3.connect(budget.path) as db:
        assert db.execute("SELECT bytes_charged FROM attempts").fetchone()[0] == (100 if slow else 4)
    if not slow:
        assert store.get(result["source_hash"]) == b"data"
