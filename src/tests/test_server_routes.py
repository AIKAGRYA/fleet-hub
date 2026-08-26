"""Contract: server.py HTTP API (CONTRACT.md "HTTP API" + "SSE event kinds").

Verifies: default-DENY auth, random/revocable cookie sessions, strict ambient
mutation checks, stable public errors/security headers, compatibility reads,
and honest process-local SSE reset semantics.

Clients come from conftest: env set BEFORE importing server (config is read
at import), lifespan not started (no broker — routes must stay honest).
"""
from __future__ import annotations

import asyncio

import pytest

from hub import auth

TOKEN = "testtoken"


def login_headers(client) -> dict[str, str]:
    response = client.post("/login", json={"token": TOKEN})
    assert response.status_code == 200
    return {
        "X-CSRF-Token": response.json()["csrf_token"],
        "Origin": "http://testserver",
        "Sec-Fetch-Site": "same-origin",
    }


# ---------------------------------------------------------------------------
# Exempt endpoints
# ---------------------------------------------------------------------------

class TestHealthz:
    def test_configured(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["version"] == "1.0.0-dev"
        assert body["build_status"] == "candidate-unqualified"
        assert body["qualified"] is False
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
        assert r.json() == {
            "status": "ok",
            "version": "1.0.0-dev",
            "wayfinder": False,
            "build_status": "candidate-unqualified",
        }

    def test_unconfigured(self, unconfigured_client):
        assert unconfigured_client.get("/health").status_code == 200


class TestPublicShellAssets:
    def test_service_worker_has_top_level_scope_and_no_cache(self, client):
        response = client.get("/sw.js")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/javascript")
        assert response.headers["service-worker-allowed"] == "/fleet/"
        assert "no-store" in response.headers["cache-control"]


class TestSession:
    def test_configured_unauthenticated(self, client):
        r = client.get("/api/session")
        assert r.status_code == 200  # exempt boot probe, never errors
        assert r.json() == {
            "auth_configured": True,
            "authenticated": False,
            "auth_mode": None,
            "csrf_token": None,
            "expires_at": None,
        }

    def test_configured_authenticated(self, client):
        client.post("/login", json={"token": TOKEN})
        r = client.get("/api/session")
        body = r.json()
        assert body["auth_configured"] is True
        assert body["authenticated"] is True
        assert body["auth_mode"] == "session"
        assert isinstance(body["csrf_token"], str)
        assert body["expires_at"].endswith("Z")

    def test_unconfigured(self, unconfigured_client):
        r = unconfigured_client.get("/api/session")
        assert r.status_code == 200
        assert r.json()["auth_configured"] is False
        assert r.json()["authenticated"] is False
        assert r.json()["csrf_token"] is None


# ---------------------------------------------------------------------------
# Auth middleware: default-DENY
# ---------------------------------------------------------------------------

class TestAuthGate:
    def test_unauthenticated_api_401_when_configured(self, client):
        r = client.get("/api/roster")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"
        assert r.json()["error"]["request_id"] == r.headers["x-request-id"]

    def test_unauthenticated_api_403_when_unconfigured(self, unconfigured_client):
        r = unconfigured_client.get("/api/roster")
        assert r.status_code == 403

    def test_bearer_header_authenticates(self, client):
        r = client.get("/api/roster", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200

    def test_invalid_explicit_bearer_does_not_fall_back_to_cookie(self, client):
        login_headers(client)
        r = client.get(
            "/api/roster", headers={"Authorization": "Bearer definitely-wrong"}
        )
        assert r.status_code == 401

    def test_query_param_token_must_not_authenticate(self, client):
        # NO query-param auth path exists anywhere in v0.6.
        r = client.get(f"/api/roster?token={TOKEN}")
        assert r.status_code == 401

    def test_events_stream_gated(self, client):
        r = client.get("/events/stream")
        assert r.status_code == 401

    def test_security_headers_cover_public_and_error_responses(self, client):
        for response in (client.get("/healthz"), client.get("/api/roster")):
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert response.headers["referrer-policy"] == "no-referrer"


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
        cookie = client.cookies.get(auth.COOKIE_NAME)
        assert cookie
        assert cookie != TOKEN
        assert r.json()["csrf_token"]
        assert r.json()["csrf_token"] != cookie
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
        headers = login_headers(client)
        old_cookie = client.cookies.get(auth.COOKIE_NAME)
        r = client.post("/logout", headers=headers)
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert client.get("/api/roster").status_code == 401
        client.cookies.set(auth.COOKIE_NAME, old_cookie)
        assert client.get("/api/roster").status_code == 401

    def test_cookie_mutation_requires_csrf_origin_and_fetch_metadata(self, client):
        login = client.post("/login", json={"token": TOKEN})
        csrf = login.json()["csrf_token"]
        no_csrf = client.post("/api/send", json={"text": "hello"})
        assert no_csrf.status_code == 403
        assert no_csrf.json()["error"]["code"] == "csrf_rejected"

        no_origin = client.post(
            "/api/send", json={"text": "hello"}, headers={"X-CSRF-Token": csrf}
        )
        assert no_origin.json()["error"]["code"] == "origin_rejected"

        wrong_origin = client.post(
            "/api/send",
            json={"text": "hello"},
            headers={
                "X-CSRF-Token": csrf,
                "Origin": "https://attacker.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert wrong_origin.json()["error"]["code"] == "origin_rejected"

        no_fetch_metadata = client.post(
            "/api/send",
            json={"text": "hello"},
            headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"},
        )
        assert no_fetch_metadata.json()["error"]["code"] == "fetch_metadata_rejected"

    def test_bearer_mutation_is_non_ambient(self, client):
        response = client.post(
            "/api/send",
            json={"text": "hello"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 200
        assert response.json()["error"] == "nats_unavailable"


class TestRequestBodyLimit:
    def test_login_content_length_is_rejected_before_validation(self, configured):
        client, server = configured
        marker = "secret-marker-that-must-not-echo"
        response = client.post(
            "/login",
            content=(marker + "x" * server.MAX_REQUEST_BODY_BYTES).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["error_code"] == "request_body_too_large"
        assert marker not in response.text

    def test_authenticated_body_uses_same_limit(self, configured):
        client, server = configured
        client.headers.update(login_headers(client))
        response = client.post(
            "/api/send",
            content=b"x" * (server.MAX_REQUEST_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["error_code"] == "request_body_too_large"


# ---------------------------------------------------------------------------
# Authed content routes
# ---------------------------------------------------------------------------

@pytest.fixture
def authed(client):
    client.headers.update(login_headers(client))
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
        r = authed.post("/api/send", json={"text": " ", "to": None})
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


async def _chunked_post_without_content_length(app, path: str, chunks: list[bytes]):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    pending = list(chunks)
    messages: list[dict] = []

    async def receive():
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(app(scope, receive, send), timeout=3)
    status = next(item["status"] for item in messages if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"")
        for item in messages
        if item["type"] == "http.response.body"
    )
    return status, body


class TestEventsStream:
    @pytest.mark.asyncio
    async def test_chunked_body_without_content_length_is_bounded(self, configured):
        _, server = configured
        half = server.MAX_REQUEST_BODY_BYTES // 2 + 1
        status, body = await _chunked_post_without_content_length(
            server.app, "/login", [b"x" * half, b"y" * half]
        )
        assert status == 413
        assert b"request_body_too_large" in body

    @pytest.mark.asyncio
    async def test_valid_json_prefix_cannot_mutate_before_chunked_413(self, configured):
        _, server = configured
        valid = b'{"token":"testtoken"}'
        first = valid + b" " * (server.MAX_REQUEST_BODY_BYTES - len(valid))
        before = len(server.SESSIONS)

        status, body = await _chunked_post_without_content_length(
            server.app, "/login", [first, b"x"]
        )

        assert status == 413
        assert b"request_body_too_large" in body
        assert len(server.SESSIONS) == before

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
        assert "1.0.0-dev" in data_line
        assert '"resume_scope": "process_local"' in data_line
        assert '"durable_resume": false' in data_line

    @pytest.mark.asyncio
    async def test_wrong_epoch_last_event_id_gets_reset(self, configured):
        _, server = configured
        _, body = await _sse_request(
            server.app,
            {"Authorization": f"Bearer {TOKEN}", "Last-Event-ID": "1-1"},
            stop=b"event: reset",
        )
        assert "event: reset_required" in body.decode()
        assert '"resume_scope": "process_local"' in body.decode()

    def test_session_eventsource_requires_same_origin_fetch_metadata(self, configured):
        client, _ = configured
        client.post("/login", json={"token": TOKEN})

        missing = client.get("/events/stream")
        assert missing.status_code == 403
        assert missing.json()["error_code"] == "sse_fetch_metadata_rejected"

        cross_site = client.get(
            "/events/stream", headers={"Sec-Fetch-Site": "cross-site"}
        )
        assert cross_site.status_code == 403
        assert cross_site.json()["error_code"] == "sse_fetch_metadata_rejected"

        hostile_referer = client.get(
            "/events/stream",
            headers={
                "Sec-Fetch-Site": "same-origin",
                "Referer": "https://attacker.example/steal",
            },
        )
        assert hostile_referer.status_code == 403
        assert hostile_referer.json()["error_code"] == "sse_origin_rejected"

    @pytest.mark.asyncio
    async def test_same_origin_session_eventsource_and_quota_release(self, configured):
        client, server = configured
        client.post("/login", json={"token": TOKEN})
        cookie = client.cookies.get(auth.COOKIE_NAME)
        status, body = await _sse_request(
            server.app,
            {
                "Cookie": f"{auth.COOKIE_NAME}={cookie}",
                "Host": "testserver",
                "Sec-Fetch-Site": "same-origin",
            },
            stop=b"\n\n",
        )
        assert status["code"] == 200
        assert b"event: hello" in body
        assert server.SSE_BY_PRINCIPAL == {}
        assert server.SSE_BY_IP == {}

    def test_eventsource_principal_and_ip_quotas_are_independent(self, configured):
        client, server = configured
        client.post("/login", json={"token": TOKEN})
        cookie = client.cookies.get(auth.COOKIE_NAME)
        context = auth.authenticate(
            TOKEN, bearer=None, cookie=cookie, sessions=server.SESSIONS
        )
        assert context is not None
        headers = {"Sec-Fetch-Site": "same-origin"}

        server.SSE_BY_PRINCIPAL[context.principal_key] = server.SSE_MAX_PER_PRINCIPAL
        principal = client.get("/events/stream", headers=headers)
        assert principal.status_code == 429
        assert principal.json()["error_code"] == "sse_principal_quota_reached"
        server.SSE_BY_PRINCIPAL.clear()

        server.SSE_BY_IP["testclient"] = server.SSE_MAX_PER_IP
        ip = client.get("/events/stream", headers=headers)
        assert ip.status_code == 429
        assert ip.json()["error_code"] == "sse_ip_quota_reached"
        server.SSE_BY_IP.clear()
