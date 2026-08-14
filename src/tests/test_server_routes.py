"""Contract: server.py HTTP API (CONTRACT.md "HTTP API" + "SSE event kinds").

Verifies: default-DENY auth middleware with exact exempt paths, /login
cookie issuance (session_value, HttpOnly, SameSite=Lax) and 503 when locked,
NO query-param auth path, /api/session boot probe, /health v0.4 compat shape,
/api/vision passthrough, POST /api/send validation, and the /events/stream
SSE hello frame + wrong-epoch reset.

Clients come from conftest: env set BEFORE importing server (config is read
at import), lifespan not started (no broker — routes must stay honest).
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("server")
auth = pytest.importorskip("hub.auth")

TOKEN = "testtoken"


# ---------------------------------------------------------------------------
# Exempt endpoints
# ---------------------------------------------------------------------------

class TestHealthz:
    def test_configured(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["version"] == "0.6.0"
        assert body["auth_configured"] is True
        assert isinstance(body["nats"], bool)

    def test_unconfigured_still_200(self, unconfigured_client):
        r = unconfigured_client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["auth_configured"] is False


class TestHealthV04Compat:
    def test_shape(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "version": "0.6.0", "wayfinder": False}

    def test_unconfigured(self, unconfigured_client):
        assert unconfigured_client.get("/health").status_code == 200


class TestSession:
    def test_configured_unauthenticated(self, client):
        r = client.get("/api/session")
        assert r.status_code == 200  # exempt boot probe, never errors
        assert r.json() == {"auth_configured": True, "authenticated": False}

    def test_configured_authenticated(self, client):
        client.post("/login", json={"token": TOKEN})
        r = client.get("/api/session")
        assert r.json() == {"auth_configured": True, "authenticated": True}

    def test_unconfigured(self, unconfigured_client):
        r = unconfigured_client.get("/api/session")
        assert r.status_code == 200
        assert r.json() == {"auth_configured": False, "authenticated": False}


# ---------------------------------------------------------------------------
# Auth middleware: default-DENY
# ---------------------------------------------------------------------------

class TestAuthGate:
    def test_unauthenticated_api_401_when_configured(self, client):
        r = client.get("/api/roster")
        assert r.status_code == 401
        assert r.json()["detail"] == "unauthorized"

    def test_unauthenticated_api_403_when_unconfigured(self, unconfigured_client):
        r = unconfigured_client.get("/api/roster")
        assert r.status_code == 403

    def test_bearer_header_authenticates(self, client):
        r = client.get("/api/roster", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200

    def test_query_param_token_must_not_authenticate(self, client):
        # NO query-param auth path exists anywhere in v0.6.
        r = client.get(f"/api/roster?token={TOKEN}")
        assert r.status_code == 401

    def test_events_stream_gated(self, client):
        r = client.get("/events/stream")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# /login and /logout
# ---------------------------------------------------------------------------

class TestLogin:
    def test_wrong_token_401(self, client):
        r = client.post("/login", json={"token": "wrong"})
        assert r.status_code == 401

    def test_right_token_sets_session_cookie(self, client):
        r = client.post("/login", json={"token": TOKEN})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert client.cookies.get("fleet_session") == auth.session_value(TOKEN)
        set_cookie = r.headers.get("set-cookie", "")
        assert "httponly" in set_cookie.lower()
        assert "samesite=lax" in set_cookie.lower()

    def test_cookie_then_api_roster_200(self, client, roster):
        client.post("/login", json={"token": TOKEN})
        r = client.get("/api/roster")
        assert r.status_code == 200
        body = r.json()
        # roster fixture: 2 active seats, 1 archived
        assert body["count"] == 2
        assert body["archived_count"] == 1
        assert all(row["seat"] == "active" for row in body["agents"])

    def test_include_archived(self, client):
        client.post("/login", json={"token": TOKEN})
        r = client.get("/api/roster", params={"include": "archived"})
        assert r.status_code == 200
        assert len(r.json()["agents"]) == 3

    def test_unconfigured_login_503(self, unconfigured_client):
        r = unconfigured_client.post("/login", json={"token": "anything"})
        assert r.status_code == 503

    def test_logout_expires_cookie(self, client):
        client.post("/login", json={"token": TOKEN})
        r = client.post("/logout")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert client.get("/api/roster").status_code == 401


# ---------------------------------------------------------------------------
# Authed content routes
# ---------------------------------------------------------------------------

@pytest.fixture
def authed(client):
    client.post("/login", json={"token": TOKEN})
    return client


class TestContentRoutes:
    def test_vision_returns_json(self, authed):
        r = authed.get("/api/vision")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)
        # conftest writes a fleet_vision.v1 file; a missing file must still
        # yield the safe fallback shape — either way schema+ventures exist.
        assert body.get("schema") == "fleet_vision.v1"
        assert isinstance(body.get("ventures"), list)

    def test_agent_row_and_404(self, authed):
        r = authed.get("/api/agent/agni-hermes")
        assert r.status_code == 200
        assert r.json()["uid"] == "agni-hermes"
        assert authed.get("/api/agent/no-such-uid").status_code == 404

    def test_chat_empty_honest(self, authed):
        r = authed.get("/api/chat")
        assert r.status_code == 200
        assert r.json()["messages"] == []

    def test_presence_shape(self, authed):
        r = authed.get("/api/presence")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["agents"], list)
        assert body["live_window_s"] == 300
        assert body["recent_window_s"] == 7200

    def test_send_empty_text_400(self, authed):
        r = authed.post("/api/send", json={"text": "", "to": None})
        assert r.status_code == 400

    def test_send_no_broker_honest_error(self, authed):
        # lifespan not started -> nc is None -> hub.natsio.send nats-down path
        r = authed.post("/api/send", json={"text": "hello", "to": None})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False


# ---------------------------------------------------------------------------
# SSE
#
# TestClient buffers whole responses, so an endless SSE stream would hang it.
# Drive the ASGI app directly: collect body chunks, and once the expected
# frame appears send http.disconnect so the stream generator exits. A hard
# asyncio timeout keeps each test under ~3s even if the frame never comes.
# ---------------------------------------------------------------------------

async def _sse_request(app, headers: dict[str, str], stop: bytes, timeout: float = 3.0):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/events/stream",
        "raw_path": b"/events/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    chunks: list[bytes] = []
    status: dict = {}
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
            status["headers"] = {
                k.decode().lower(): v.decode() for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if stop in b"".join(chunks):
                disconnect.set()

    try:
        await asyncio.wait_for(app(scope, receive, send), timeout)
    except asyncio.TimeoutError:
        pass  # assertions below report on whatever was collected
    return status, b"".join(chunks)


class TestEventsStream:
    @pytest.mark.asyncio
    async def test_hello_frame_first(self, configured):
        _, server = configured
        status, body = await _sse_request(
            server.app, {"Authorization": f"Bearer {TOKEN}"}, stop=b"\n\n"
        )
        assert status["code"] == 200
        assert status["headers"]["content-type"].startswith("text/event-stream")
        text = body.decode()
        assert "event: hello" in text
        data_line = next(
            l for l in text.splitlines() if l.startswith("data:") and "epoch" in l
        )
        assert "0.6.0" in data_line

    @pytest.mark.asyncio
    async def test_wrong_epoch_last_event_id_gets_reset(self, configured):
        _, server = configured
        _, body = await _sse_request(
            server.app,
            {"Authorization": f"Bearer {TOKEN}", "Last-Event-ID": "1-1"},
            stop=b"event: reset",
        )
        assert "event: reset" in body.decode()
