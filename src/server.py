#!/usr/bin/env python3
"""Dharma Fleet Hub v1 candidate backend.

Fleet Hub is a bounded phone projection, not a work-state owner. This module
keeps the v0.6 HTTP surface while adding authenticated v1 read contracts,
canonical chat intents, and a read-only Mission Control provider boundary.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hub import BUILD_STATUS, VERSION, auth, monitor, natsio, presence
from hub.mission_provider import (
    DEFAULT_MISSION_PROVIDER,
    MAX_OWNER_RESPONSE_BYTES,
    MissionCatalog,
    MissionProvider,
    MissionSnapshotProjection,
    mission_provider_from_settings,
)
from hub.needs_john import derive_needs_john
from hub.state import (
    HubState,
    IdempotencyConflict,
    IdempotencySaturated,
    TooManySubscribers,
)


# --- Configuration ----------------------------------------------------------


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _base_path(value: str) -> str:
    clean = value.strip() or "/fleet/"
    if not clean.startswith("/") or any(ch in clean for ch in "\r\n?#"):
        return "/fleet/"
    return clean.rstrip("/") + "/"


ROOT = Path(os.environ.get("FLEET_HUB_ROOT", Path(__file__).resolve().parent))
STATIC = ROOT / "static"
ROSTER_PATH = Path(os.environ.get("FLEET_HUB_ROSTER", ROOT / "roster.json"))
VISION_PATH = Path(os.environ.get("FLEET_HUB_VISION", ROOT / "vision.json"))
TOKEN = (os.environ.get("FLEET_HUB_TOKEN") or "").strip()
INSECURE_COOKIE = os.environ.get("FLEET_HUB_INSECURE_COOKIE") == "1"
BASE_PATH = _base_path(os.environ.get("FLEET_HUB_BASE_PATH", "/fleet/"))
SESSION_TTL_S = _env_int(
    "FLEET_HUB_SESSION_TTL_S", 12 * 60 * 60, minimum=60, maximum=30 * 24 * 60 * 60
)
MAX_SESSIONS = _env_int("FLEET_HUB_MAX_SESSIONS", 256, minimum=1, maximum=4096)
MAX_REQUEST_BODY_BYTES = _env_int(
    "FLEET_HUB_MAX_REQUEST_BODY_BYTES",
    64 * 1024,
    minimum=8 * 1024,
    maximum=1024 * 1024,
)
MISSION_PROVIDER_TIMEOUT_S = _env_int(
    "FLEET_HUB_MISSION_PROVIDER_TIMEOUT_MS", 2_000, minimum=100, maximum=30_000
) / 1000
MISSION_CONTROL_URL = (
    os.environ.get("FLEET_HUB_MISSION_CONTROL_URL") or ""
).strip()
MISSION_CONTROL_TOKEN = (
    os.environ.get("FLEET_HUB_MISSION_CONTROL_TOKEN") or ""
).strip()
MISSION_CONTROL_MAX_RESPONSE_BYTES = _env_int(
    "FLEET_HUB_MISSION_CONTROL_MAX_RESPONSE_BYTES",
    MAX_OWNER_RESPONSE_BYTES,
    minimum=16 * 1024,
    maximum=8 * 1024 * 1024,
)
BROKER_TIMEOUT_S = _env_int(
    "FLEET_HUB_BROKER_TIMEOUT_MS", 2_500, minimum=100, maximum=30_000
) / 1000
SSE_MAX_PER_PRINCIPAL = _env_int(
    "FLEET_HUB_SSE_MAX_PER_PRINCIPAL", 3, minimum=1, maximum=32
)
SSE_MAX_PER_IP = _env_int(
    "FLEET_HUB_SSE_MAX_PER_IP", 8, minimum=1, maximum=128
)
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
NATS_USER = os.environ.get("NATS_USER") or None
NATS_PASS = os.environ.get("NATS_PASS") or os.environ.get("NATS_PASSWORD") or None
STREAM = os.environ.get("NATS_STREAM", "DHARMA_A2A")
CHAT_SUBJECT = os.environ.get("NATS_CHAT_SUBJECT", "dharma.fleet.chat")
FLEET_SENDER_UID = os.environ.get("FLEET_HUB_AGENT_UID", "operator").strip()
_agent_observation_subject = os.environ.get(
    "FLEET_HUB_NATS_AGENT_OBSERVATION_SUBJECT", "dharma.agent.>"
).strip()
if _agent_observation_subject not in {"", "dharma.agent.>"}:
    _agent_observation_subject = "dharma.agent.>"
NATS_AGENT_OBSERVATION_SUBJECT = _agent_observation_subject or None
_transport_principal = os.environ.get(
    "FLEET_HUB_NATS_TRANSPORT_PRINCIPAL", "unspecified"
).strip()
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", _transport_principal):
    _transport_principal = "unspecified"
NATS_TRANSPORT_PRINCIPAL = _transport_principal
_transport_authority = os.environ.get(
    "FLEET_HUB_NATS_TRANSPORT_AUTHORITY", "credential_owner_unspecified"
).strip()
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", _transport_authority):
    _transport_authority = "credential_owner_unspecified"
NATS_TRANSPORT_AUTHORITY = _transport_authority
MONITOR_URL = os.environ.get("NATS_MONITOR_URL", "http://127.0.0.1:8222")
LIVE_WINDOW_S = _env_int("FLEET_LIVE_WINDOW_S", 300, minimum=10, maximum=86_400)
RECENT_WINDOW_S = _env_int(
    "FLEET_RECENT_WINDOW_S", 7200, minimum=LIVE_WINDOW_S, maximum=7 * 86_400
)
REPLAY_HOURS = _env_int("FLEET_REPLAY_HOURS", 48, minimum=1, maximum=168)
REPLAY_STREAMS = tuple(
    stream.strip()
    for stream in os.environ.get("FLEET_REPLAY_STREAMS", "DHARMA_A2A").split(",")
    if stream.strip()
)[:16]
MISSION_IDS = tuple(
    mission_id.strip()
    for mission_id in os.environ.get("FLEET_HUB_MISSION_IDS", "").split(",")
    if mission_id.strip()
)
CONFIGURED_ORIGINS = frozenset(
    origin.strip().rstrip("/").lower()
    for origin in os.environ.get("FLEET_HUB_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
)
_deployment_namespace = os.environ.get(
    "FLEET_HUB_DEPLOYMENT_NAMESPACE", "agni-candidate"
).strip()
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", _deployment_namespace):
    _deployment_namespace = "agni-candidate"
DEDUPE_NAMESPACE = f"fleet-hub-v1:{_deployment_namespace}"
_EVIDENCE_MODES = frozenset(
    {"fixture", "local_integration", "live_read", "live_canary", "owner"}
)
EVIDENCE_MODE = os.environ.get(
    "FLEET_HUB_EVIDENCE_MODE", "local_integration"
).strip()
if EVIDENCE_MODE not in _EVIDENCE_MODES:
    EVIDENCE_MODE = "local_integration"
SOURCE_INSTANCE = os.environ.get(
    "FLEET_HUB_SOURCE_INSTANCE", "fleet-hub-candidate"
).strip()[:128]
if not SOURCE_INSTANCE or any(ord(char) < 0x20 for char in SOURCE_INSTANCE):
    SOURCE_INSTANCE = "fleet-hub-candidate"
GENERATED_BY_FIXTURE = os.environ.get(
    "FLEET_HUB_GENERATED_BY_FIXTURE", ""
).strip().casefold() in {"1", "true", "yes", "on"}

CFG = SimpleNamespace(
    url=NATS_URL,
    user=NATS_USER,
    password=NATS_PASS,
    stream=STREAM,
    chat_subject=CHAT_SUBJECT,
    agent_observation_subject=NATS_AGENT_OBSERVATION_SUBJECT,
    fleet_sender_uid=FLEET_SENDER_UID,
    monitor_url=MONITOR_URL,
    live_window_s=LIVE_WINDOW_S,
    recent_window_s=RECENT_WINDOW_S,
    replay_hours=REPLAY_HOURS,
    replay_streams=REPLAY_STREAMS,
    dedupe_namespace=DEDUPE_NAMESPACE,
)

NODES = [
    {
        "id": "agni",
        "label": "AGNI",
        "role": "NATS hub / Fleet Hub",
        "public": "157.245.193.15",
        "tailscale": "100.79.111.89",
        "host": "agni-openclaw",
    },
    {
        "id": "meghadharma",
        "label": "Meghadharma",
        "role": "semantic bridge / 雷影",
        "public": "178.128.87.170",
        "tailscale": "100.103.106.70",
        "host": "meghadharma-cloud",
    },
    {
        "id": "rushabdev",
        "label": "Rushabdev",
        "role": "operator proxy / revenue",
        "public": "167.172.95.184",
        "tailscale": "100.113.248.117",
        "host": "openclaw23",
    },
]


def load_roster() -> dict[str, Any]:
    """Load the configured roster; absent/invalid data stays explicitly empty."""

    try:
        data = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"schema": "fleet_roster.unavailable", "agents": {}, "count": 0}
    agents = data.get("agents")
    if not isinstance(agents, dict):
        return {"schema": "fleet_roster.invalid", "agents": {}, "count": 0}
    for info in agents.values():
        if isinstance(info, dict) and not info.get("seat"):
            info["seat"] = "active"
    return data


STATE = HubState()
SESSIONS = auth.SessionStore(ttl_s=SESSION_TTL_S, max_sessions=MAX_SESSIONS)
ROSTER = load_roster()
MONITOR_CACHE: dict[str, Any] = {}
LOGIN_FAILURES: dict[str, list[float]] = {}
SSE_BY_PRINCIPAL: dict[str, int] = {}
SSE_BY_IP: dict[str, int] = {}


def utc_now() -> str:
    value = presence.iso(time.time())
    assert value is not None
    return value


def decorated_rows() -> list[dict[str, Any]]:
    return presence.decorate(
        ROSTER.get("agents") or {},
        STATE.presence,
        time.time(),
        LIVE_WINDOW_S,
        RECENT_WINDOW_S,
    )


def _safe_endpoint(value: str) -> str:
    """Strip any userinfo/query from a configured network endpoint."""

    try:
        parts = urlsplit(value)
        host = parts.hostname or "configured"
        port = f":{parts.port}" if parts.port is not None else ""
        return f"{parts.scheme or 'nats'}://{host}{port}"
    except (TypeError, ValueError):
        return "configured"


# --- Runtime ----------------------------------------------------------------


async def _health_snapshot() -> dict[str, Any]:
    mon = await monitor.varz(MONITOR_URL, MONITOR_CACHE)
    return {
        "connected": STATE.connected,
        "last_seq": STATE.last_seq,
        "monitor_ok": bool(mon.get("ok")),
    }


async def health_emit_loop() -> None:
    """Emit bounded, low-rate health invalidations."""

    last: dict[str, Any] | None = None
    tick = 0
    while True:
        try:
            if tick % 15 == 0:
                snap = await _health_snapshot()
            else:
                cached = MONITOR_CACHE.get("val") or {}
                snap = {
                    "connected": STATE.connected,
                    "last_seq": STATE.last_seq,
                    "monitor_ok": bool(cached.get("ok")),
                }
            if snap != last or tick % 15 == 0:
                STATE.bus.publish("health", snap)
                last = snap
        except Exception:
            # The next health read exposes a stable unavailable state; this
            # fan-out helper must not terminate the process.
            pass
        tick += 1
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(natsio.nats_loop(STATE, CFG, ROSTER)),
        asyncio.create_task(health_emit_loop()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if STATE.nc is not None:
            try:
                await STATE.nc.close()
            except Exception:
                pass
        for queue in list(STATE.bus._queues):
            STATE.bus.detach(queue)


app = FastAPI(title="Dharma Fleet Hub", version=VERSION, lifespan=lifespan)
app.state.mission_provider = mission_provider_from_settings(
    owner_base_url=MISSION_CONTROL_URL,
    bearer_token=MISSION_CONTROL_TOKEN,
    mission_ids=MISSION_IDS,
    timeout_s=MISSION_PROVIDER_TIMEOUT_S,
    max_response_bytes=MISSION_CONTROL_MAX_RESPONSE_BYTES,
)
app.state.sessions = SESSIONS
# Provenance is explicit so a fixture-backed browser build cannot be mistaken
# for production evidence.  A production operator may replace these values in
# its separately authorized adapter/bootstrap wiring; repository-local runs are
# local integration by default.
app.state.evidence_mode = EVIDENCE_MODE
app.state.source_instance = SOURCE_INSTANCE
app.state.generated_by_fixture = GENERATED_BY_FIXTURE


# --- HTTP safety boundary ---------------------------------------------------


PUBLIC_PATHS = {
    "/",
    "/index.html",
    "/sw.js",
    "/health",
    "/healthz",
    "/login",
    "/api/session",
}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unavailable")


def _error(
    request: Request,
    *,
    status: int,
    code: str,
    message: str,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "detail": message,
        "error_code": code,
        "error": {
            "code": code,
            "message": message,
            "request_id": _request_id(request),
        },
    }
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status)


def _secure_response(request: Request, response):
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    response.headers.setdefault("X-Request-ID", _request_id(request))
    if (
        request.url.path.startswith("/api/")
        or request.url.path in {"/login", "/logout"}
    ):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _normalized_origin(value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    if parts.path not in {"", "/"}:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _request_origin(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    return f"{proto.split(',')[0].strip().lower()}://{host.split(',')[0].strip().lower()}"


def _reference_origin(value: str) -> str | None:
    """Extract an origin from an Origin or Referer value without userinfo."""

    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    if parts.username or parts.password:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _session_sse_error(
    request: Request, context: auth.AuthContext
) -> JSONResponse | None:
    """Reject ambient-cookie EventSource requests from another site.

    Native EventSource normally omits ``Origin`` on a same-origin GET, so
    Fetch Metadata is the required signal.  Origin/Referer, when supplied, are
    checked as additional evidence rather than required headers.
    """

    if request.url.path != "/events/stream" or context.mode != "session":
        return None
    if request.headers.get("sec-fetch-site", "").lower() != "same-origin":
        return _error(
            request,
            status=403,
            code="sse_fetch_metadata_rejected",
            message="Event stream request is not same-origin",
        )
    allowed = CONFIGURED_ORIGINS or frozenset({_request_origin(request)})
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value and _reference_origin(value) not in allowed:
            return _error(
                request,
                status=403,
                code="sse_origin_rejected",
                message="Event stream origin is not allowed",
            )
    return None


class RequestBodyLimitMiddleware:
    """Buffer a bounded body before dispatching to FastAPI.

    Dispatch must not begin until the final ``http.request`` frame is known to
    fit. Truncating an over-limit stream and replacing the response afterward
    is unsafe: a valid JSON prefix could execute a mutation before the client
    receives the synthetic 413.
    """

    def __init__(self, application, *, max_bytes: int) -> None:
        self.application = application
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.application(scope, receive, send)
            return

        # Do not eagerly consume a safe request's receive channel. Streaming
        # responses such as SSE may not receive any inbound ASGI frame until
        # the client disconnects, so buffering a GET would deadlock before the
        # endpoint can send response headers. Fleet Hub only parses bodies on
        # mutating methods; those are the requests that need this pre-dispatch
        # boundary.
        if str(scope.get("method", "GET")).upper() not in MUTATING_METHODS:
            await self.application(scope, receive, send)
            return

        request_id = uuid.uuid4().hex

        async def reject() -> None:
            response = JSONResponse(
                {
                    "detail": "Request body exceeds the configured limit",
                    "error_code": "request_body_too_large",
                    "error": {
                        "code": "request_body_too_large",
                        "message": "Request body exceeds the configured limit",
                        "request_id": request_id,
                    },
                },
                status_code=413,
            )
            for name, value in _SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Request-ID"] = request_id

            async def disconnected_receive():
                return {"type": "http.disconnect"}

            await response(scope, disconnected_receive, send)

        headers = {
            key.lower(): value
            for key, value in scope.get("headers", ())
        }
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await reject()
                    return
            except ValueError:
                # The inner safety middleware emits the stable 400 contract.
                pass

        buffered: list[dict[str, Any]] = []
        consumed = 0
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                # No endpoint has run, so a disconnected upload has no effect.
                return
            if message_type != "http.request":
                buffered.append(message)
                break
            body = message.get("body", b"")
            consumed += len(body)
            if consumed > self.max_bytes:
                await reject()
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break

        async def replay_receive():
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.application(scope, replay_receive, send)


def _cookie_mutation_error(request: Request, context: auth.AuthContext) -> JSONResponse | None:
    if context.mode != "session" or request.method not in MUTATING_METHODS:
        return None
    csrf = request.headers.get(auth.CSRF_HEADER)
    cookie = request.cookies.get(auth.COOKIE_NAME)
    if not SESSIONS.check_csrf(cookie, csrf):
        return _error(
            request,
            status=403,
            code="csrf_rejected",
            message="CSRF validation failed",
        )
    origin = _normalized_origin(request.headers.get("origin", ""))
    allowed = CONFIGURED_ORIGINS or frozenset({_request_origin(request)})
    if origin is None or origin not in allowed:
        return _error(
            request,
            status=403,
            code="origin_rejected",
            message="Request origin is not allowed",
        )
    if request.headers.get("sec-fetch-site", "").lower() != "same-origin":
        return _error(
            request,
            status=403,
            code="fetch_metadata_rejected",
            message="Request fetch metadata is not same-origin",
        )
    return None


@app.middleware("http")
async def security_and_auth_middleware(request: Request, call_next):
    request.state.request_id = uuid.uuid4().hex
    path = request.url.path
    public = path in PUBLIC_PATHS or path.startswith("/static/")
    context: auth.AuthContext | None = None

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            return _secure_response(
                request,
                _error(
                    request,
                    status=400,
                    code="invalid_content_length",
                    message="Content-Length is invalid",
                ),
            )
        if declared_length < 0:
            return _secure_response(
                request,
                _error(
                    request,
                    status=400,
                    code="invalid_content_length",
                    message="Content-Length is invalid",
                ),
            )
        if declared_length > MAX_REQUEST_BODY_BYTES:
            return _secure_response(
                request,
                _error(
                    request,
                    status=413,
                    code="request_body_too_large",
                    message="Request body exceeds the configured limit",
                ),
            )

    if not public:
        if not TOKEN:
            return _secure_response(
                request,
                _error(
                    request,
                    status=403,
                    code="server_locked",
                    message="Server authentication is not configured",
                ),
            )
        authorization = request.headers.get("authorization")
        bearer = auth.parse_bearer(authorization)
        if authorization is not None and bearer is None:
            return _secure_response(
                request,
                _error(
                    request,
                    status=401,
                    code="unauthorized",
                    message="Authentication required",
                ),
            )
        context = auth.authenticate(
            TOKEN,
            bearer=bearer,
            cookie=request.cookies.get(auth.COOKIE_NAME),
            sessions=SESSIONS,
        )
        if context is None:
            return _secure_response(
                request,
                _error(
                    request,
                    status=401,
                    code="unauthorized",
                    message="Authentication required",
                ),
            )
        request.state.auth = context
        sse_error = _session_sse_error(request, context)
        if sse_error is not None:
            return _secure_response(request, sse_error)
        mutation_error = _cookie_mutation_error(request, context)
        if mutation_error is not None:
            return _secure_response(request, mutation_error)

    try:
        response = await call_next(request)
    except Exception:
        response = _error(
            request,
            status=500,
            code="internal_error",
            message="The request could not be completed",
        )
    return _secure_response(request, response)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    del exc
    return _error(
        request,
        status=422,
        code="invalid_request",
        message="Request validation failed",
    )


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    defaults = {
        400: ("invalid_request", "Request is invalid"),
        401: ("unauthorized", "Authentication required"),
        403: ("forbidden", "Request is forbidden"),
        404: ("not_found", "Resource not found"),
        409: ("conflict", "Request conflicts with current state"),
        413: ("request_body_too_large", "Request body exceeds the configured limit"),
        429: ("rate_limited", "Too many requests"),
        503: ("unavailable", "Capability is unavailable"),
    }
    code, message = defaults.get(
        exc.status_code, ("request_failed", "The request could not be completed")
    )
    return _error(request, status=exc.status_code, code=code, message=message)


# --- Models and API helpers -------------------------------------------------


_CALLER_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def reject_non_scalar_unicode(cls, value):
        """Keep lone UTF-16 surrogates out of JSON, logs, and NATS frames."""

        if isinstance(value, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("string contains a non-scalar Unicode code point")
        return value


class LoginRequest(WireModel):
    token: str = Field(min_length=1, max_length=4096)


class SendRequest(WireModel):
    text: str = Field(min_length=1, max_length=8000)
    to: str | None = Field(default=None, max_length=200)
    msg_id: str | None = Field(default=None, pattern=_CALLER_ID)


class ChatIntent(WireModel):
    text: str = Field(min_length=1, max_length=8000)
    to: str | None = Field(default=None, min_length=1, max_length=200)
    msg_id: str | None = Field(default=None, pattern=_CALLER_ID)
    correlation_id: str | None = Field(default=None, pattern=_CALLER_ID)
    causation_id: str | None = Field(default=None, pattern=_CALLER_ID)
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


def _v1_meta(*, source: str, observed_at: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "fleet-hub.api.v1",
        "observed_at": observed_at or utc_now(),
        "source": source,
        "build_status": BUILD_STATUS,
    }


def _provider(request: Request) -> MissionProvider:
    provider = getattr(request.app.state, "mission_provider", None)
    if not isinstance(provider, MissionProvider):
        return DEFAULT_MISSION_PROVIDER
    return provider


async def _mission_catalog(request: Request) -> MissionCatalog | None:
    try:
        value = await asyncio.wait_for(
            _provider(request).list_missions(), timeout=MISSION_PROVIDER_TIMEOUT_S
        )
        return MissionCatalog.model_validate(value)
    except Exception:
        return None


async def _mission_snapshot(
    request: Request, mission_id: str
) -> MissionSnapshotProjection | None:
    try:
        value = await asyncio.wait_for(
            _provider(request).get_snapshot(mission_id),
            timeout=MISSION_PROVIDER_TIMEOUT_S,
        )
        return MissionSnapshotProjection.model_validate(value)
    except Exception:
        return None


def _projection_matches_mission(
    projection: MissionSnapshotProjection, mission_id: str
) -> bool:
    return bool(
        projection.mission_id == mission_id
        and projection.snapshot is not None
        and projection.snapshot.mission.mission_id == mission_id
    )


def _catalog_version(catalog: MissionCatalog) -> str:
    encoded = json.dumps(
        catalog.model_dump(mode="json"), separators=(",", ":"), sort_keys=True
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class CursorError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _encode_cursor(*, kind: str, offset: int, version: str) -> str:
    body = json.dumps(
        {"kind": kind, "offset": offset, "version": version},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(
        TOKEN.encode(), b"fleet-hub-cursor-v1\x00" + body, hashlib.sha256
    ).digest()[:16]
    return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")


def _decode_cursor(cursor: str, *, kind: str, version: str) -> int:
    if len(cursor) > 512:
        raise CursorError("invalid_cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        if len(raw) <= 16:
            raise ValueError
        body, supplied = raw[:-16], raw[-16:]
        expected = hmac.new(
            TOKEN.encode(), b"fleet-hub-cursor-v1\x00" + body, hashlib.sha256
        ).digest()[:16]
        if not hmac.compare_digest(supplied, expected):
            raise ValueError
        payload = json.loads(body)
        if payload.get("kind") != kind:
            raise ValueError
        if payload.get("version") != version:
            raise CursorError("stale_cursor")
        offset = payload.get("offset")
        if not isinstance(offset, int) or offset < 0 or offset > 100_000:
            raise ValueError
        return offset
    except CursorError:
        raise
    except Exception as exc:
        raise CursorError("invalid_cursor") from exc


def _page(
    items: list[Any],
    *,
    cursor: str | None,
    limit: int,
    kind: str,
    version: str,
) -> tuple[list[Any], str | None]:
    offset = _decode_cursor(cursor, kind=kind, version=version) if cursor else 0
    if offset > len(items):
        raise CursorError("invalid_cursor")
    page = items[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = (
        _encode_cursor(kind=kind, offset=next_offset, version=version)
        if next_offset < len(items)
        else None
    )
    return page, next_cursor


def _cursor_error_response(request: Request, exc: CursorError) -> JSONResponse:
    status = 409 if exc.code == "stale_cursor" else 400
    message = "Cursor no longer matches the source" if status == 409 else "Cursor is invalid"
    return _error(request, status=status, code=exc.code, message=message)


def _throttled(ip: str, now: float) -> bool:
    recent = [seen for seen in LOGIN_FAILURES.get(ip, []) if now - seen < 60]
    if recent:
        LOGIN_FAILURES[ip] = recent
    else:
        LOGIN_FAILURES.pop(ip, None)
    for stale_ip in [
        key
        for key, values in LOGIN_FAILURES.items()
        if not any(now - seen < 60 for seen in values)
    ]:
        LOGIN_FAILURES.pop(stale_ip, None)
    return len(recent) >= 10


# --- Authentication routes -------------------------------------------------


@app.post("/login")
async def login(body: LoginRequest, request: Request) -> JSONResponse:
    if not TOKEN:
        return _error(
            request,
            status=503,
            code="server_locked",
            message="Server authentication is not configured",
        )
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    if _throttled(ip, now):
        return _error(
            request,
            status=429,
            code="login_rate_limited",
            message="Too many login attempts",
        )
    if not auth.check_token(TOKEN, body.token):
        if len(LOGIN_FAILURES) >= 1024 and ip not in LOGIN_FAILURES:
            LOGIN_FAILURES.pop(next(iter(LOGIN_FAILURES)))
        LOGIN_FAILURES.setdefault(ip, []).append(now)
        return _error(
            request,
            status=401,
            code="invalid_token",
            message="Invalid login token",
        )
    LOGIN_FAILURES.pop(ip, None)
    session = SESSIONS.issue(now=now)
    response = JSONResponse(
        {
            "ok": True,
            "csrf_token": session.csrf_token,
            "expires_at": presence.iso(session.expires_at),
        }
    )
    response.set_cookie(
        auth.COOKIE_NAME,
        session.session_id,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=SESSION_TTL_S,
        secure=not INSECURE_COOKIE,
    )
    return response


def _logout_response(request: Request, *, v1: bool = False) -> JSONResponse:
    context: auth.AuthContext = request.state.auth
    cookie_session_id = request.cookies.get(auth.COOKIE_NAME)
    revoked = SESSIONS.revoke(cookie_session_id)
    if context.mode == "session" and context.session is not None:
        revoked = SESSIONS.revoke(context.session.session_id) or revoked
    body: dict[str, Any] = {"ok": True, "revoked": revoked}
    if v1:
        body.update(_v1_meta(source="FleetHub.SessionStore"))
    response = JSONResponse(body)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@app.post("/logout")
async def logout(request: Request) -> JSONResponse:
    return _logout_response(request)


@app.post("/api/v1/session/logout")
async def logout_v1(request: Request) -> JSONResponse:
    return _logout_response(request, v1=True)


@app.get("/api/session")
async def api_session(request: Request) -> dict[str, Any]:
    authorization = request.headers.get("authorization")
    bearer = auth.parse_bearer(authorization)
    context = None
    if authorization is None or bearer is not None:
        context = auth.authenticate(
            TOKEN,
            bearer=bearer,
            cookie=request.cookies.get(auth.COOKIE_NAME),
            sessions=SESSIONS,
        )
    return {
        "auth_configured": bool(TOKEN),
        "authenticated": context is not None,
        "auth_mode": context.mode if context else None,
        "csrf_token": (
            context.session.csrf_token
            if context is not None and context.session is not None
            else None
        ),
        "expires_at": (
            presence.iso(context.session.expires_at)
            if context is not None and context.session is not None
            else None
        ),
    }


# --- Health and compatibility reads ----------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "nats": STATE.connected,
        "version": VERSION,
        "build_status": BUILD_STATUS,
        "qualified": False,
        "auth_configured": bool(TOKEN),
    }


@app.get("/health")
async def health_v04() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": VERSION,
        "wayfinder": False,
        "build_status": BUILD_STATUS,
    }


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "nats": {
            "connected": STATE.connected,
            "stream": STREAM,
            "chat_subject": CHAT_SUBJECT,
            "agent_observation_subject": NATS_AGENT_OBSERVATION_SUBJECT,
            "transport_principal": NATS_TRANSPORT_PRINCIPAL,
            "transport_authority": NATS_TRANSPORT_AUTHORITY,
            "messages": STATE.messages,
            "last_seq": STATE.last_seq,
        },
        "sse_clients": STATE.bus.clients(),
        "event_resume_scope": STATE.bus.resume_scope,
        "event_resume_durable": False,
        "version": VERSION,
        "build_status": BUILD_STATUS,
    }


async def _stream_info() -> dict[str, Any]:
    if STATE.js is None:
        return {"error": STATE.last_error or "stream_unavailable"}
    try:
        info = await asyncio.wait_for(
            STATE.js.stream_info(STREAM), timeout=BROKER_TIMEOUT_S
        )
        stream_state = info.state
        return {
            "messages": stream_state.messages,
            "first_seq": stream_state.first_seq,
            "last_seq": stream_state.last_seq,
            "consumer_count": stream_state.consumer_count,
            "bytes": stream_state.bytes,
            "error": None,
        }
    except Exception as exc:
        return {"error": f"stream_info_unavailable:{type(exc).__name__}"}


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    stream = await _stream_info()
    mon = await monitor.varz(MONITOR_URL, MONITOR_CACHE)
    rows = decorated_rows()
    active = [row for row in rows if row["seat"] == "active"]
    summary = {
        "fresh": sum(1 for row in active if row["freshness"] == "fresh"),
        "recent": sum(1 for row in active if row["freshness"] == "recent"),
        "stale": sum(1 for row in active if row["freshness"] == "stale"),
        "never": sum(1 for row in active if row["freshness"] == "never"),
        "archived_seats": len(rows) - len(active),
    }
    return {
        "ok": bool(STATE.connected and TOKEN),
        "version": VERSION,
        "build_status": BUILD_STATUS,
        "auth_configured": bool(TOKEN),
        "broker": {
            "connected": STATE.connected,
            "endpoint": _safe_endpoint(NATS_URL),
            "stream": STREAM,
            "chat_subject": CHAT_SUBJECT,
            "agent_observation_subject": NATS_AGENT_OBSERVATION_SUBJECT,
            "transport_principal": NATS_TRANSPORT_PRINCIPAL,
            "transport_authority": NATS_TRANSPORT_AUTHORITY,
            "messages": stream.get("messages", STATE.messages),
            "first_seq": stream.get("first_seq"),
            "last_seq": stream.get("last_seq", STATE.last_seq),
            "consumer_count": stream.get("consumer_count"),
            "bytes": stream.get("bytes"),
            "error": stream.get("error") or STATE.last_error,
        },
        "monitor": mon,
        "startup_backfill": dict(STATE.replay),
        "presence_summary": summary,
        "nodes": NODES,
        "timestamp": utc_now(),
    }


@app.get("/api/broker")
async def api_broker() -> dict[str, Any]:
    rtt_ms = None
    if STATE.nc is not None:
        try:
            rtt_ms = round(await STATE.nc.rtt() * 1000, 2)
        except Exception:
            rtt_ms = None
    return {
        "connected": STATE.connected,
        "rtt_ms": rtt_ms,
        "monitor": await monitor.varz(MONITOR_URL, MONITOR_CACHE),
        "stream": await _stream_info(),
    }


@app.get("/api/presence")
async def api_presence() -> dict[str, Any]:
    return {
        "agents": decorated_rows(),
        "live_window_s": LIVE_WINDOW_S,
        "recent_window_s": RECENT_WINDOW_S,
        "identity_claim_policy": "payload senders are reported_unverified",
    }


@app.get("/api/roster")
async def api_roster(include: str | None = None) -> dict[str, Any]:
    rows = decorated_rows()
    active = [row for row in rows if row["seat"] == "active"]
    return {
        "agents": rows if include == "archived" else active,
        "count": len(active),
        "archived_count": len(rows) - len(active),
    }


@app.get("/api/agent/{uid}")
async def api_agent(uid: str) -> dict[str, Any]:
    if len(uid) > 256:
        raise HTTPException(status_code=404)
    for row in decorated_rows():
        if row["uid"] == uid:
            return row
    raise HTTPException(status_code=404)


@app.get("/api/nodes")
async def api_nodes() -> dict[str, Any]:
    return {"nodes": NODES}


@app.get("/api/chat")
async def api_chat() -> dict[str, Any]:
    return {"messages": list(STATE.chat)[-200:]}


@app.get("/api/dm/{uid}")
async def api_dm(uid: str) -> dict[str, Any]:
    if uid not in (ROSTER.get("agents") or {}):
        raise HTTPException(status_code=404)
    return {"messages": list(STATE.dms.get(uid, []))}


@app.get("/api/vision")
async def api_vision() -> dict[str, Any]:
    try:
        value = json.loads(VISION_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"schema": "fleet_vision.invalid"}
    except Exception:
        return {"schema": "fleet_vision.v1", "ventures": []}


@app.get("/api/kanban")
async def api_kanban(request: Request):
    """Compatibility route backed by the selected Mission Control snapshot."""

    catalog = await _mission_catalog(request)
    if catalog is None or not catalog.available:
        return _error(
            request,
            status=503,
            code="mission_provider_unavailable",
            message="Mission Control projection is unavailable",
            extra={"available": False},
        )
    if not catalog.configured_mission_ids:
        return _error(
            request,
            status=503,
            code="mission_not_configured",
            message="No Mission Control mission is configured",
            extra={"available": False},
        )
    selected_mission_id = catalog.configured_mission_ids[0]
    projection = await _mission_snapshot(request, selected_mission_id)
    if projection is None or not projection.available or projection.snapshot is None:
        return _error(
            request,
            status=503,
            code="mission_snapshot_unavailable",
            message="Mission Control snapshot is unavailable",
            extra={"available": False},
        )
    if not _projection_matches_mission(projection, selected_mission_id):
        return _error(
            request,
            status=503,
            code="mission_snapshot_identity_mismatch",
            message="Mission Control snapshot identity could not be verified",
            extra={"available": False},
        )
    return {
        "tasks": [task.model_dump(mode="json") for task in projection.snapshot.tasks],
        "available": True,
        "source": projection.authority,
        "observed_at": projection.snapshot.observed_at.isoformat(),
    }


@app.post("/api/send")
async def api_send(body: SendRequest, request: Request) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400)
    msg_id = body.msg_id or str(uuid.uuid4())
    context: auth.AuthContext = request.state.auth
    return await natsio.send(
        STATE,
        CFG,
        ROSTER,
        text,
        body.to,
        msg_id,
        principal_scope=context.principal_key,
    )


# --- v1 reads ---------------------------------------------------------------


def _mission_unavailable(
    request: Request,
    *,
    code: str,
    status: int = 503,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    return _error(
        request,
        status=status,
        code=code,
        message="Mission Control projection is unavailable",
        extra={
            "available": False,
            **_v1_meta(source="TaskBoard+RuntimeStateStore"),
            **(extra or {}),
        },
    )


@app.get("/api/v1/bootstrap")
async def api_v1_bootstrap(request: Request) -> dict[str, Any]:
    catalog = await _mission_catalog(request)
    if catalog is None:
        mission_data: dict[str, Any] = {
            "available": False,
            "missions": [],
            "discovery_complete": False,
            "commands_available": False,
            "commands": [],
            "error_code": "provider_unavailable",
        }
    else:
        mission_data = catalog.model_dump(mode="json")

    needs_data: dict[str, Any] = {
        "available": False,
        "items": [],
        "count": None,
        "commands_available": False,
        "commands": [],
        "error_code": "provider_unavailable"
        if catalog is None or not catalog.available
        else "no_selected_mission",
    }
    selected = None
    if catalog is not None and catalog.available and catalog.missions:
        selected = catalog.missions[0].model_dump(mode="json")
        selected_mission_id = catalog.missions[0].mission_id
        projection = await _mission_snapshot(request, selected_mission_id)
        if projection is not None:
            if (
                projection.available
                and projection.snapshot is not None
                and _projection_matches_mission(projection, selected_mission_id)
            ):
                derived = derive_needs_john(
                    projection.snapshot, source_version=projection.source_version
                )
                needs_data = {
                    "available": True,
                    **derived.model_dump(mode="json"),
                }
            else:
                needs_data["error_code"] = (
                    projection.error_code
                    or "mission_snapshot_identity_mismatch"
                )
        else:
            needs_data["error_code"] = "provider_unavailable"

    context: auth.AuthContext = request.state.auth
    evidence_mode = getattr(request.app.state, "evidence_mode", "local_integration")
    if evidence_mode not in {
        "fixture",
        "local_integration",
        "live_read",
        "live_canary",
        "owner",
    }:
        evidence_mode = "local_integration"
    source_instance = str(
        getattr(request.app.state, "source_instance", "fleet-hub-candidate")
    )[:128]
    generated_by_fixture = bool(
        getattr(request.app.state, "generated_by_fixture", False)
    ) or evidence_mode == "fixture"
    return {
        "available": True,
        **_v1_meta(source="FleetHub"),
        "version": VERSION,
        "qualified": False,
        "evidence_mode": evidence_mode,
        "source_instance": source_instance,
        "generated_by_fixture": generated_by_fixture,
        "process_local": True,
        "resume_scope": STATE.bus.resume_scope,
        "durable_event_resume": False,
        "session": {
            "authenticated": True,
            "auth_mode": context.mode,
        },
        "connections": {
            "hub": True,
            "nats": STATE.connected,
            "mission_control": bool(catalog and catalog.available),
            "observed_at": utc_now(),
        },
        "missions": mission_data,
        "selected_mission": selected,
        "needs_john": needs_data,
        "capabilities": {
            "mission_read": bool(catalog and catalog.available),
            "mission_commands": {"available": False, "commands": []},
            "needs_john_commands": {"available": False, "commands": []},
            "chat": {
                "available": STATE.connected and STATE.js is not None,
                "group_transcript": True,
                "group_fanout": False,
                "direct_message": True,
                "semantic_reply_promised": False,
            },
            "durable_event_resume": False,
        },
        "cursor": f"{STATE.bus.epoch}-{STATE.bus.n}",
    }


@app.get("/api/v1/missions")
async def api_v1_missions(
    request: Request,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=25, ge=1, le=100),
):
    catalog = await _mission_catalog(request)
    if catalog is None:
        return _mission_unavailable(request, code="mission_provider_unavailable")
    if not catalog.available:
        return _mission_unavailable(
            request,
            code=catalog.error_code or "mission_provider_unavailable",
            extra={"missions": catalog.model_dump(mode="json")},
        )
    version = _catalog_version(catalog)
    summaries = list(catalog.missions)
    try:
        page, next_cursor = _page(
            summaries,
            cursor=cursor,
            limit=limit,
            kind="missions",
            version=version,
        )
    except CursorError as exc:
        return _cursor_error_response(request, exc)
    return {
        "available": True,
        **_v1_meta(source=catalog.authority),
        "source_version": version,
        "discovery_complete": False,
        "configured_mission_ids": list(catalog.configured_mission_ids),
        "missions": [item.model_dump(mode="json") for item in page],
        "count": len(page),
        "total_configured_visible": len(summaries),
        "next_cursor": next_cursor,
        "authority": catalog.authority,
        "commands_available": False,
        "commands": [],
        "capabilities": {"commands_available": False, "commands": []},
    }


def _projection_error_response(
    request: Request, projection: MissionSnapshotProjection
) -> JSONResponse:
    status = (
        503 if projection.error_code == "provider_unavailable" else 404
    )
    return _mission_unavailable(
        request,
        code=projection.error_code or "mission_snapshot_unavailable",
        status=status,
        extra={"mission_id": projection.mission_id},
    )


def _projection_binding_error(request: Request) -> JSONResponse:
    return _mission_unavailable(
        request,
        code="mission_snapshot_identity_mismatch",
        status=503,
    )


@app.get("/api/v1/missions/{mission_id}/snapshot")
async def api_v1_mission_snapshot(request: Request, mission_id: str):
    if not re.fullmatch(_CALLER_ID, mission_id):
        return _error(
            request,
            status=400,
            code="invalid_mission_id",
            message="Mission ID is invalid",
        )
    catalog = await _mission_catalog(request)
    if catalog is None or not catalog.available:
        return _mission_unavailable(request, code="mission_provider_unavailable")
    if mission_id not in catalog.configured_mission_ids:
        return _mission_unavailable(
            request,
            code="mission_not_configured",
            status=404,
            extra={"mission_id": mission_id},
        )
    projection = await _mission_snapshot(request, mission_id)
    if projection is None:
        return _mission_unavailable(request, code="mission_provider_unavailable")
    if not projection.available or projection.snapshot is None:
        return _projection_error_response(request, projection)
    if not _projection_matches_mission(projection, mission_id):
        return _projection_binding_error(request)
    return {
        "available": True,
        **_v1_meta(
            source=projection.authority,
            observed_at=projection.snapshot.observed_at.isoformat(),
        ),
        **projection.model_dump(mode="json"),
        "capabilities": {"commands_available": False, "commands": []},
    }


@app.get("/api/v1/needs-john")
async def api_v1_needs_john(
    request: Request,
    mission_id: str | None = Query(default=None, max_length=128),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=25, ge=1, le=100),
):
    catalog = await _mission_catalog(request)
    if catalog is None or not catalog.available:
        return _mission_unavailable(request, code="mission_provider_unavailable")
    selected = mission_id
    if selected is None:
        if not catalog.configured_mission_ids:
            return {
                "available": True,
                **_v1_meta(source=catalog.authority),
                "items": [],
                "count": 0,
                "total": 0,
                "next_cursor": None,
                "scope": "configured_missions_only",
                "discovery_complete": False,
                "capabilities": {"commands_available": False, "commands": []},
            }
        selected = catalog.configured_mission_ids[0]
    if not re.fullmatch(_CALLER_ID, selected):
        return _error(
            request,
            status=400,
            code="invalid_mission_id",
            message="Mission ID is invalid",
        )
    if selected not in catalog.configured_mission_ids:
        return _mission_unavailable(
            request,
            code="mission_not_configured",
            status=404,
            extra={"mission_id": selected},
        )
    projection = await _mission_snapshot(request, selected)
    if projection is None:
        return _mission_unavailable(request, code="mission_provider_unavailable")
    if not projection.available or projection.snapshot is None:
        return _projection_error_response(request, projection)
    if not _projection_matches_mission(projection, selected):
        return _projection_binding_error(request)
    derived = derive_needs_john(
        projection.snapshot, source_version=projection.source_version
    )
    items = list(derived.items)
    version = f"{derived.rule_version}:{derived.source_version}"
    try:
        page, next_cursor = _page(
            items,
            cursor=cursor,
            limit=limit,
            kind=f"needs-john:{selected}",
            version=version,
        )
    except CursorError as exc:
        return _cursor_error_response(request, exc)
    return {
        "available": True,
        **_v1_meta(
            source=derived.source_authority,
            observed_at=derived.observed_at.isoformat(),
        ),
        "mission_id": selected,
        "rule_version": derived.rule_version,
        "source_version": derived.source_version,
        "items": [item.model_dump(mode="json") for item in page],
        "count": len(page),
        "total": derived.count,
        "next_cursor": next_cursor,
        "authority": derived.source_authority,
        "actions_available": False,
        "commands_available": False,
        "commands": [],
        "capabilities": {"commands_available": False, "commands": []},
        "proves_executor_liveness": False,
    }


@app.get("/api/v1/roster")
async def api_v1_roster(
    include: str | None = Query(default=None, pattern=r"^(archived)?$")
) -> dict[str, Any]:
    legacy = await api_roster(include)
    return {
        "available": True,
        **_v1_meta(source="configured_roster+nats_observations"),
        **legacy,
        "identity_claim_policy": "payload senders are reported_unverified",
    }


@app.get("/api/v1/topology")
async def api_v1_topology() -> dict[str, Any]:
    return {
        "available": True,
        **_v1_meta(source="FleetHub.allowlisted_topology"),
        "nodes": NODES,
        "broker": {
            "connected": STATE.connected,
            "endpoint": _safe_endpoint(NATS_URL),
            "stream": STREAM,
        },
        "capabilities": {"mutation": False},
    }


# --- v1 mutations -----------------------------------------------------------


def _validated_idempotency_key(request: Request) -> str | None:
    value = request.headers.get("idempotency-key")
    if value is None or not re.fullmatch(_CALLER_ID, value):
        return None
    return value


@app.post("/api/v1/intents/chat")
async def api_v1_chat_intent(body: ChatIntent, request: Request):
    text = body.text.strip()
    if not text:
        return _error(
            request,
            status=400,
            code="empty_text",
            message="Chat text is empty",
        )
    idempotency_key = _validated_idempotency_key(request)
    if idempotency_key is None:
        return _error(
            request,
            status=400,
            code="invalid_idempotency_key",
            message="A valid Idempotency-Key is required",
        )
    context: auth.AuthContext = request.state.auth
    fingerprint_body = body.model_dump(mode="json")
    fingerprint_body["text"] = text
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_body, separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()
    seed = hashlib.sha256(
        f"{DEDUPE_NAMESPACE}\x00{context.principal_key}\x00"
        f"{idempotency_key}\x00{body.msg_id or ''}".encode()
    ).hexdigest()
    # Canonical message identity and transport dedupe identity are distinct.
    # Both remain deterministic for a same-principal retry.
    message_id = f"msg-{seed[:32]}"
    correlation_id = body.correlation_id or f"corr-{seed[24:48]}"
    trace_id = body.trace_id or hashlib.sha256(f"trace:{seed}".encode()).hexdigest()[:32]

    async def publish() -> dict:
        return await natsio.send(
            STATE,
            CFG,
            ROSTER,
            text,
            body.to,
            message_id,
            principal_scope=context.principal_key,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            causation_id=body.causation_id or "",
            trace_id=trace_id,
            require_jetstream=True,
        )

    try:
        result, reused = await STATE.idempotency.run(
            principal=context.principal_key,
            key=idempotency_key,
            fingerprint=fingerprint,
            operation=publish,
            cache_if=lambda candidate: candidate.get("accepted") is True,
        )
    except IdempotencyConflict:
        return _error(
            request,
            status=409,
            code="idempotency_conflict",
            message="Idempotency-Key was already used for another intent",
        )
    except IdempotencySaturated:
        return _error(
            request,
            status=503,
            code="idempotency_unavailable",
            message="Idempotency registry is temporarily unavailable",
        )
    response = {
        **result,
        **_v1_meta(source="FleetHub->NATS/JetStream"),
        "idempotency_reused": reused,
        "client_message_id": body.msg_id,
        "transport_claim": (
            "broker_stored"
            if result.get("ack_tier") == "PUBLISH_ACCEPTED"
            else "broker_deduplicated_body_unverified"
            if result.get("ack_tier") == "DEDUPLICATED_UNVERIFIED"
            else "unaccepted"
        ),
        "semantic_effect": "unobserved",
    }
    deterministic_error = result.get("error")
    if deterministic_error in {
        "ambiguous_recipient",
        "packet_id_invalid",
        "recipient_archived",
        "recipient_inbox_unratified",
        "sender_identity_invalid",
        "unknown_recipient",
    }:
        return _error(
            request,
            status=422,
            code=deterministic_error,
            message="The requested recipient is not addressable",
            extra={
                "accepted": False,
                "transport_claim": "unaccepted",
                "semantic_effect": "unobserved",
                **_v1_meta(source="FleetHub.RosterProjection"),
            },
        )
    if not result.get("accepted"):
        return JSONResponse(response, status_code=503)
    return response


@app.post("/api/v1/missions/{mission_id}/commands")
async def api_v1_mission_commands(request: Request, mission_id: str):
    del mission_id
    return _error(
        request,
        status=503,
        code="mission_commands_unavailable",
        message="Mission commands are not available in this read-only build",
        extra={
            "available": False,
            "commands": [],
            **_v1_meta(source="TaskBoard+RuntimeStateStore"),
        },
    )


@app.post("/api/v1/needs-john/{item_id}/commands")
async def api_v1_needs_john_commands(request: Request, item_id: str):
    del item_id
    return _error(
        request,
        status=503,
        code="needs_john_commands_unavailable",
        message="Needs-John commands are not available in this read-only build",
        extra={
            "available": False,
            "commands": [],
            **_v1_meta(source="TaskBoard+RuntimeStateStore"),
        },
    )


# --- SSE --------------------------------------------------------------------


def _reserve_sse(principal: str, ip: str) -> str | None:
    """Atomically reserve one event stream on the current event loop."""

    if SSE_BY_PRINCIPAL.get(principal, 0) >= SSE_MAX_PER_PRINCIPAL:
        return "principal"
    if SSE_BY_IP.get(ip, 0) >= SSE_MAX_PER_IP:
        return "ip"
    SSE_BY_PRINCIPAL[principal] = SSE_BY_PRINCIPAL.get(principal, 0) + 1
    SSE_BY_IP[ip] = SSE_BY_IP.get(ip, 0) + 1
    return None


def _release_sse(principal: str, ip: str) -> None:
    for table, key in ((SSE_BY_PRINCIPAL, principal), (SSE_BY_IP, ip)):
        count = table.get(key, 0)
        if count <= 1:
            table.pop(key, None)
        else:
            table[key] = count - 1


def _sse_frame(envelope: dict[str, Any]) -> str:
    data = {
        **envelope["data"],
        "schema_version": "fleet-hub.event.v1",
        "observed_at": utc_now(),
        "source": "FleetHub.process_event_bus",
        "resume_scope": STATE.bus.resume_scope,
    }
    return (
        f"id: {envelope['id']}\n"
        f"event: {envelope['event']}\n"
        f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
    )


def _event_after_cutoff(envelope: dict[str, Any], epoch: str, cutoff: int) -> bool:
    try:
        event_epoch, raw_n = str(envelope["id"]).rsplit("-", 1)
        return event_epoch == epoch and int(raw_n) > cutoff
    except (KeyError, TypeError, ValueError):
        return False


@app.get("/events/stream")
async def events_stream(request: Request):
    bus = STATE.bus
    context: auth.AuthContext = request.state.auth
    principal = context.principal_key
    ip = request.client.host if request.client else "unknown"
    quota = _reserve_sse(principal, ip)
    if quota is not None:
        return _error(
            request,
            status=429,
            code=f"sse_{quota}_quota_reached",
            message="Event stream connection quota is reached",
        )
    try:
        queue = bus.attach()
    except TooManySubscribers:
        _release_sse(principal, ip)
        return _error(
            request,
            status=503,
            code="sse_capacity_reached",
            message="Event stream capacity is reached",
        )
    # attach() and this cutoff read contain no await, so the event-loop view is
    # atomic. Ring backlog is capped at this point; later events arrive only on
    # the attached queue and cannot be delivered twice.
    cutoff_epoch = bus.epoch
    cutoff_n = bus.n
    last_event_id = request.headers.get("last-event-id")

    async def generate():
        try:
            hello = {
                "schema_version": "fleet-hub.event.v1",
                "epoch": bus.epoch,
                "resume": bus.n,
                "resume_scope": bus.resume_scope,
                "durable_resume": False,
                "version": VERSION,
                "build_status": BUILD_STATUS,
            }
            yield f"retry: 3000\nevent: hello\ndata: {json.dumps(hello)}\n\n"
            backlog, needs_reset = bus.since(last_event_id)
            backlog = [
                envelope
                for envelope in backlog
                if not _event_after_cutoff(envelope, cutoff_epoch, cutoff_n)
            ]
            if needs_reset:
                reset = {
                    "reason": "cursor_outside_process_ring",
                    "resume_scope": bus.resume_scope,
                    "durable_resume": False,
                }
                yield f"event: reset_required\ndata: {json.dumps(reset)}\n\n"
                # One-release v0.6 compatibility event.
                yield f"event: reset\ndata: {json.dumps(reset)}\n\n"
            else:
                for envelope in backlog:
                    yield _sse_frame(envelope)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    envelope = await asyncio.wait_for(queue.get(), timeout=15)
                    if not _event_after_cutoff(envelope, cutoff_epoch, cutoff_n):
                        continue
                    yield _sse_frame(envelope)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.detach(queue)
            _release_sse(principal, ip)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


# --- Static shell -----------------------------------------------------------


@app.get("/sw.js")
async def service_worker():
    path = STATIC / "sw.js"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Service-Worker-Allowed": BASE_PATH,
        },
    )


@app.get("/")
@app.get("/index.html")
async def index() -> FileResponse:
    return FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# Added last so this pure ASGI limiter is the outermost application boundary,
# ahead of Starlette/FastAPI request-body parsing and validation.
app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
