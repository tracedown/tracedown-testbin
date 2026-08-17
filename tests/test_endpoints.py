"""Smoke coverage over every route (chaos/TLS listeners are covered by a
raw-socket test and a live-run check; the ASGI TestClient covers the rest)."""

import asyncio
import os
import socket
import tempfile

# Isolate the sqlite state per test run — must be set before importing main.
os.environ["STATE_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="testbin-"), "state.db")

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


# ── echo ──

def test_anything_echoes_everything():
    r = client.post(
        "/anything/some/path?q=1",
        json={"a": 1},
        headers={"X-Workspace": "ws1"},
        cookies={"session": "abc"},
    )
    body = r.json()
    assert r.status_code == 200
    assert body["method"] == "POST"
    assert body["args"] == {"q": "1"}
    assert body["headers"]["x-workspace"] == "ws1"
    assert body["cookies"] == {"session": "abc"}
    assert body["json"] == {"a": 1}


def test_anything_form_body():
    r = client.post("/anything", data={"success": "true"})
    assert r.json()["form"] == {"success": "true"}


def test_get_compat():
    body = client.get("/get?x=y").json()
    assert body["args"] == {"x": "y"}
    assert "data" not in body


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


# ── status family ──

def test_status_codes():
    assert client.get("/status/503").status_code == 503
    assert client.get("/status/204").status_code == 204
    assert client.get("/status/999").status_code == 400


def test_flip():
    ok = client.post("/flip", json={"success": True})
    assert ok.status_code == 200 and ok.json() == {"success": True, "next": False}
    down = client.post("/flip", json={"success": False})
    assert down.status_code == 500 and down.json()["next"] is True
    # 0/1 and string forms, all channels
    assert client.post("/flip", json={"success": 0}).status_code == 500
    assert client.post("/flip", json={"success": 1}).status_code == 200
    assert client.post("/flip", json={"success": "0"}).status_code == 500
    assert client.post("/flip", json={"success": "TRUE"}).status_code == 200
    assert client.post("/flip", data={"success": "false"}).status_code == 500
    assert client.post("/flip", data={"success": "1"}).status_code == 200
    assert client.get("/flip?success=false").status_code == 500
    assert client.get("/flip?success=0").status_code == 500
    assert client.get("/flip").status_code == 200
    assert client.get("/flip?success=maybe").status_code == 400


def test_random_within_codes():
    for _ in range(10):
        assert client.get("/random?codes=200,500").status_code in (200, 500)
    assert client.get("/random?p=1&codes=201,500").status_code == 201


def test_flap_cycles():
    codes = [client.get("/flap/t-flap?codes=200,500").status_code for _ in range(4)]
    assert codes == [200, 500, 200, 500]


def test_sequence_holds_last():
    codes = [client.get("/sequence/t-seq?codes=200,500,204").status_code for _ in range(5)]
    assert codes == [200, 500, 204, 204, 204]


def test_fail_then_succeed():
    codes = [client.get("/fail-then-succeed/t-fts?fails=2").status_code for _ in range(4)]
    assert codes == [500, 500, 200, 200]


# ── timing ──

def test_delay_and_ttfb_and_drip():
    assert client.get("/delay/10").status_code == 200
    assert client.get("/delay/10?status=503&bytes=32").status_code == 503
    assert len(client.get("/ttfb/10?bytes=48").content) == 48
    assert len(client.get("/drip?duration_ms=50&bytes=100&chunks=5").content) == 100


def test_spike_transitions():
    fast = client.get("/spike/t-spike?after=2&fast_ms=1&slow_ms=1&grow_bytes=100")
    client.get("/spike/t-spike?after=2&fast_ms=1&slow_ms=1&grow_bytes=100")
    slow = client.get("/spike/t-spike?after=2&fast_ms=1&slow_ms=1&grow_bytes=100")
    assert len(slow.content) == len(fast.content) + 100


# ── bodies ──

def test_bytes_exact_and_deterministic():
    a, b = client.get("/bytes/1000"), client.get("/bytes/1000")
    assert len(a.content) == 1000 and a.content == b.content


def test_raw_exact():
    r = client.get("/raw?body=OK")
    assert r.text == "OK" and r.headers["content-type"].startswith("text/plain")


def test_json_variants():
    exact = client.get("/json/exact").json()
    extra = client.get("/json/extra").json()
    wrong = client.get("/json/wrong").json()
    assert exact["service"]["checks"] == 3
    assert "debug" in extra and "region" in extra["service"]
    assert isinstance(wrong["service"]["checks"], str)
    assert client.get("/json/nope").status_code == 404


def test_uuid_changes():
    assert client.get("/uuid").json()["uuid"] != client.get("/uuid").json()["uuid"]


def test_counter_increments():
    assert client.get("/counter/t-c").json()["count"] == 1
    assert client.get("/counter/t-c").json()["count"] == 2


def test_login():
    ok = client.post("/login", json={"username": "demo", "password": "s3cret"})
    assert ok.status_code == 200 and ok.json()["user"] == "demo" and ok.json()["token"]
    assert client.post("/login", json={"username": "demo", "password": "invalid"}).status_code == 401
    assert client.post("/login", json={"username": "demo"}).status_code == 401
    assert client.post("/login", data={"username": "demo", "password": "x"}).status_code == 200


def test_token_protected_chain():
    token = client.get("/token").json()["token"]
    assert client.get("/protected").status_code == 401
    ok = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert ok.json() == {"authorized": True, "token": token}


# ── cookies ──

def test_cookie_roundtrip():
    r = client.get("/cookies/set?a=1&b=2")
    assert r.json()["set"] == {"a": "1", "b": "2"}
    assert client.get("/cookies").json()["cookies"] == {"a": "1", "b": "2"}
    client.get("/cookies/delete?a=")
    assert client.get("/cookies").json()["cookies"] == {"b": "2"}


# ── redirects ──

def test_redirect_chain():
    r = client.get("/redirect/3", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/redirect/2")
    final = client.get("/redirect/2", follow_redirects=True)
    assert final.status_code == 200 and final.json()["path"] == "/anything"


def test_redirect_to_relative_only():
    assert client.get("/redirect-to?url=/anything", follow_redirects=False).status_code == 302
    assert client.get("/redirect-to?url=https://evil.example", follow_redirects=False).status_code == 400
    assert client.get("/redirect-to?url=//evil.example", follow_redirects=False).status_code == 400


def test_redirect_loop():
    r = client.get("/redirect-loop", follow_redirects=False)
    assert r.headers["location"] == "/redirect-loop"


# ── state admin ──

def test_state_key_ops():
    client.get("/counter/t-state")
    assert client.get("/state/counter:t-state").json()["count"] == 1
    assert client.delete("/state/counter:t-state").json()["deleted"] is True
    assert client.get("/state/counter:t-state").status_code == 404


def test_state_global_open_without_token():
    client.get("/counter/t-global")
    assert "counter:t-global" in client.get("/state").json()["keys"]
    assert client.delete("/state").json()["cleared"] >= 1


def test_chaos_info():
    modes = client.get("/chaos").json()["modes"]
    assert set(modes) == {"/rst", "/empty", "/partial"}


# ── chaos listener (raw socket) ──

@pytest.mark.parametrize("path,expect", [("/rst", "reset"), ("/empty", "empty"), ("/partial", "partial")])
def test_chaos_listener(path, expect):
    from chaos import start_chaos_server

    async def run():
        server = await start_chaos_server(0)
        port = server.sockets[0].getsockname()[1]

        def hit():
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            try:
                data = s.recv(4096)
            except ConnectionResetError:
                return "reset", b""
            finally:
                s.close()
            return "closed", data

        outcome, data = await asyncio.get_event_loop().run_in_executor(None, hit)
        server.close()
        await server.wait_closed()
        return outcome, data

    outcome, data = asyncio.new_event_loop().run_until_complete(run())
    if expect == "reset":
        assert outcome == "reset" or data == b""
    elif expect == "empty":
        assert data == b""
    else:
        assert data.startswith(b"HTTP/1.1 200") and len(data) < 200


# ── rate limiter ──

from ratelimit import RateLimiter


def _limiter(rps=5, allow=(), deny=()):
    rl = RateLimiter(rps=rps, ban_seconds=3600, max_tracked=1000)
    rl.set_rules(allow=list(allow), deny=list(deny))
    return rl


def test_ratelimit_bans_over_threshold():
    rl = _limiter(rps=5)
    ip = "203.0.113.7"
    for _ in range(5):
        allowed, _, _ = rl.check(ip)
        assert allowed
    allowed, reason, retry = rl.check(ip)  # 6th in the same second
    assert not allowed and reason == "banned" and retry == 3600
    # Stays banned on the next request
    allowed, reason, _ = rl.check(ip)
    assert not allowed and reason == "banned"


def test_ratelimit_whitelist_bypass():
    rl = _limiter(rps=2, allow=["203.0.113.0/24"])
    ip = "203.0.113.9"
    for _ in range(50):
        allowed, reason, _ = rl.check(ip)
        assert allowed and reason == "whitelisted"


def test_ratelimit_blacklist_blocks():
    rl = _limiter(deny=["198.51.100.4"])
    allowed, reason, _ = rl.check("198.51.100.4")
    assert not allowed and reason == "blacklisted"


def test_ratelimit_set_rules_hot_swap():
    rl = _limiter(rps=2)
    ip = "203.0.113.77"
    rl.set_rules(allow=["203.0.113.77"], deny=[])
    for _ in range(10):
        assert rl.check(ip)[0]
    rl.set_rules(allow=[], deny=[])  # remove exemption
    assert not all(rl.check(ip)[0] for _ in range(5))


def test_ratelimit_unban():
    rl = _limiter(rps=1)
    ip = "203.0.113.50"
    rl.check(ip)
    rl.check(ip)  # bans
    assert ip in rl.bans()
    assert rl.unban(ip) is True
    assert ip not in rl.bans()
    allowed, _, _ = rl.check(ip)
    assert allowed


def test_ratelimit_middleware_via_xff():
    # Distinct high-volume IP via X-Forwarded-For trips the app middleware.
    # Temporarily lower the ceiling so a handful of requests reliably exceed
    # it within one 1-second window (105 TestClient calls can otherwise spill
    # across a second boundary and reset the counter — timing-flaky).
    ip = "203.0.113.201"
    headers = {"x-forwarded-for": ip}
    assert client.get("/health").status_code == 200  # health is exempt
    limiter = app.state.limiter
    original_rps = limiter.rps
    limiter.rps = 3
    try:
        saw_429 = any(
            client.get("/get", headers=headers).status_code == 429
            for _ in range(10)
        )
    finally:
        limiter.rps = original_rps
    assert saw_429


def test_ratelimit_admin_crud_persists():
    # No ADMIN_TOKEN set in tests → open admin. Add a deny rule, see it listed,
    # verify it blocks over the wire, then remove it.
    ip = "198.51.100.99"
    cidr = "198.51.100.99/32"  # bare IPs normalize to /32
    r = client.post("/ratelimit/deny", json={"cidr": ip})
    assert r.status_code == 200 and r.json()["added"] is True and r.json()["cidr"] == cidr
    assert cidr in client.get("/ratelimit").json()["deny"]
    # The middleware now blocks that IP with 403.
    assert client.get("/get", headers={"x-forwarded-for": ip}).status_code == 403
    r = client.request("DELETE", f"/ratelimit/deny?cidr={ip}")
    assert r.status_code == 200 and r.json()["removed"] is True
    assert cidr not in client.get("/ratelimit").json()["deny"]


def test_ratelimit_admin_rejects_bad_cidr():
    assert client.post("/ratelimit/allow", json={"cidr": "not-an-ip"}).status_code == 400
    assert client.post("/ratelimit/bogus", json={"cidr": "1.2.3.4"}).status_code == 404
