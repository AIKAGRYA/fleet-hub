#!/usr/bin/env python3
"""Dharma Fleet Hub v0.6 — AGNI-hosted operator console.

Thin FastAPI layer: config, middleware, routes. All behavior lives in hub/
(auth, state, presence, natsio, monitor). Fail-closed: no FLEET_HUB_TOKEN
means no access, never open.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hub import VERSION, auth, monitor, natsio, presence
from hub.state import HubState

# --- Config (env, read at import) -------------------------------------------

ROOT = Path(os.environ.get("FLEET_HUB_ROOT", Path(__file__).resolve().parent))
STATIC = ROOT / "static"
ROSTER_PATH = Path(os.environ.get("FLEET_HUB_ROSTER", ROOT / "roster.json"))
VISION_PATH = Path(os.environ.get("FLEET_HUB_VISION", ROOT / "vision.json"))
TOKEN = (os.environ.get("FLEET_HUB_TOKEN") or "").strip()
INSECURE_COOKIE = os.environ.get("FLEET_HUB_INSECURE_COOKIE") == "1"
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
NATS_USER = os.environ.get("NATS_USER") or None
NATS_PASS = os.environ.get("NATS_PASS") or os.environ.get("NATS_PASSWORD") or None
STREAM = os.environ.get("NATS_STREAM", "DHARMA_A2A")
CHAT_SUBJECT = os.environ.get("NATS_CHAT_SUBJECT", "dharma.fleet.chat")
MONITOR_URL = os.environ.get("NATS_MONITOR_URL", "http://127.0.0.1:8222")
LIVE_WINDOW_S = int(os.environ.get("FLEET_LIVE_WINDOW_S", "300"))
RECENT_WINDOW_S = int(os.environ.get("FLEET_RECENT_WINDOW_S", "7200"))
REPLAY_HOURS = int(os.environ.get("FLEET_REPLAY_HOURS", "48"))
REPLAY_STREAMS = [
    s.strip()
    for s in os.environ.get("FLEET_REPLAY_STREAMS", "DHARMA_A2A").split(",")
    if s.strip()
]

CFG = SimpleNamespace(
    url=NATS_URL,
    user=NATS_USER,
    password=NATS_PASS,
    stream=STREAM,
    chat_subject=CHAT_SUBJECT,
    monitor_url=MONITOR_URL,
    live_window_s=LIVE_WINDOW_S,
    recent_window_s=RECENT_WINDOW_S,
    replay_hours=REPLAY_HOURS,
    replay_streams=REPLAY_STREAMS,
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
    """Load roster; tolerate v1 (no seat field — treated as active)."""
    try:
        data = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"agents": {}, "count": 0}
    for info in (data.get("agents") or {}).values():
        if isinstance(info, dict) and not info.get("seat"):
            info["seat"] = "active"
    return data


STATE = HubState()
ROSTER = load_roster()
MONITOR_CACHE: dict[str, Any] = {}
LOGIN_FAILURES: dict[str, list[float]] = {}


def utc_now() -> str:
    return presence.iso(time.time())


def decorated_rows() -> list[dict[str, Any]]:
    return presence.decorate(
        ROSTER.get("agents") or {},
        STATE.presence,
        time.time(),
        LIVE_WINDOW_S,
        RECENT_WINDOW_S,
    )


async def _health_snapshot() -> dict[str, Any]:
    mon = await monitor.varz(MONITOR_URL, MONITOR_CACHE)
    return {
        "connected": STATE.connected,
        "last_seq": STATE.last_seq,
        "monitor_ok": bool(mon.get("ok")),
    }


async def health_emit_loop() -> None:
    """Emit the health SSE event on change (checked every 2s from cached
    values) and unconditionally every 30s (with a fresh monitor probe)."""
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
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if STATE.nc is not None:
            try:
                await STATE.nc.close()
            except Exception:
                pass
        for q in list(STATE.bus._queues):
            STATE.bus.detach(q)


app = FastAPI(title="Dharma Fleet Hub", version=VERSION, lifespan=lifespan)

EXEMPT_PATHS = {"/healthz", "/health", "/login", "/logout", "/", "/index.html", "/api/session"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in EXEMPT_PATHS or path.startswith("/static"):
        return await call_next(request)
    if not TOKEN:
        return JSONResponse({"detail": "server token not configured"}, status_code=403)
    bearer = auth.parse_bearer(request.headers.get("authorization"))
    cookie = request.cookies.get("fleet_session")
    if not auth.check(TOKEN, bearer, cookie):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


class LoginRequest(BaseModel):
    token: str


class SendRequest(BaseModel):
    text: str = Field(max_length=8000)
    to: str | None = Field(default=None, max_length=200)
    msg_id: str | None = Field(default=None, max_length=100)


# --- Auth routes -------------------------------------------------------------


def _throttled(ip: str, now: float) -> bool:
    recent = [t for t in LOGIN_FAILURES.get(ip, []) if now - t < 60]
    if recent:
        LOGIN_FAILURES[ip] = recent
    else:
        LOGIN_FAILURES.pop(ip, None)
    for stale_ip in [k for k, v in LOGIN_FAILURES.items() if not any(now - t < 60 for t in v)]:
        LOGIN_FAILURES.pop(stale_ip, None)
    return len(recent) > 10


@app.post("/login")
async def login(body: LoginRequest, request: Request) -> JSONResponse:
    if not TOKEN:
        return JSONResponse(
            {"detail": "FLEET_HUB_TOKEN not set on host — hub locked"}, status_code=503
        )
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    if _throttled(ip, now):
        return JSONResponse({"detail": "too many attempts"}, status_code=429)
    if not auth.check(TOKEN, body.token, None):
        LOGIN_FAILURES.setdefault(ip, []).append(now)
        return JSONResponse({"detail": "invalid token"}, status_code=401)
    LOGIN_FAILURES.pop(ip, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        "fleet_session",
        auth.session_value(TOKEN),
        httponly=True,
        samesite="lax",
        path="/",
        max_age=2592000,
        secure=not INSECURE_COOKIE,
    )
    return resp


@app.post("/logout")
async def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("fleet_session", path="/")
    return resp


@app.get("/api/session")
async def api_session(request: Request) -> dict[str, Any]:
    bearer = auth.parse_bearer(request.headers.get("authorization"))
    cookie = request.cookies.get("fleet_session")
    return {
        "auth_configured": bool(TOKEN),
        "authenticated": auth.check(TOKEN, bearer, cookie),
    }


# --- Health & status ---------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "nats": STATE.connected,
        "version": VERSION,
        "auth_configured": bool(TOKEN),
    }


@app.get("/health")
async def health_v04() -> dict[str, Any]:
    return {"status": "ok", "version": VERSION, "wayfinder": False}


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "nats": {
            "connected": STATE.connected,
            "stream": STREAM,
            "messages": STATE.messages,
            "last_seq": STATE.last_seq,
        },
        "sse_clients": STATE.bus.clients(),
        "version": VERSION,
    }


async def _stream_info() -> dict[str, Any]:
    if STATE.js is None:
        return {"error": STATE.last_error or "not connected"}
    try:
        info = await STATE.js.stream_info(STREAM)
        st = info.state
        return {
            "messages": st.messages,
            "first_seq": st.first_seq,
            "last_seq": st.last_seq,
            "consumer_count": st.consumer_count,
            "bytes": st.bytes,
            "error": None,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:300]}


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    stream = await _stream_info()
    mon = await monitor.varz(MONITOR_URL, MONITOR_CACHE)
    rows = decorated_rows()
    active = [r for r in rows if r["seat"] == "active"]
    summary = {
        "fresh": sum(1 for r in active if r["freshness"] == "fresh"),
        "recent": sum(1 for r in active if r["freshness"] == "recent"),
        "stale": sum(1 for r in active if r["freshness"] == "stale"),
        "never": sum(1 for r in active if r["freshness"] == "never"),
        "archived_seats": len(rows) - len(active),
    }
    return {
        "ok": bool(STATE.connected and TOKEN),
        "version": VERSION,
        "auth_configured": bool(TOKEN),
        "broker": {
            "connected": STATE.connected,
            "url": NATS_URL,
            "stream": STREAM,
            "messages": stream.get("messages", STATE.messages),
            "first_seq": stream.get("first_seq"),
            "last_seq": stream.get("last_seq", STATE.last_seq),
            "consumer_count": stream.get("consumer_count"),
            "bytes": stream.get("bytes"),
            "error": stream.get("error") or STATE.last_error,
        },
        "monitor": mon,
        "replay": dict(STATE.replay),
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


# --- Roster & presence -------------------------------------------------------


@app.get("/api/presence")
async def api_presence() -> dict[str, Any]:
    return {
        "agents": decorated_rows(),
        "live_window_s": LIVE_WINDOW_S,
        "recent_window_s": RECENT_WINDOW_S,
    }


@app.get("/api/roster")
async def api_roster(include: str | None = None) -> dict[str, Any]:
    rows = decorated_rows()
    active = [r for r in rows if r["seat"] == "active"]
    return {
        "agents": rows if include == "archived" else active,
        "count": len(active),
        "archived_count": len(rows) - len(active),
    }


@app.get("/api/agent/{uid}")
async def api_agent(uid: str) -> dict[str, Any]:
    for row in decorated_rows():
        if row["uid"] == uid:
            return row
    raise HTTPException(status_code=404, detail="unknown agent")


@app.get("/api/nodes")
async def api_nodes() -> dict[str, Any]:
    return {"nodes": NODES}


# --- Chat, DMs, vision, kanban ----------------------------------------------


@app.get("/api/chat")
async def api_chat() -> dict[str, Any]:
    return {"messages": list(STATE.chat)[-200:]}


@app.get("/api/dm/{uid}")
async def api_dm(uid: str) -> dict[str, Any]:
    if uid not in (ROSTER.get("agents") or {}):
        raise HTTPException(status_code=404, detail="unknown agent")
    return {"messages": list(STATE.dms.get(uid, []))}


@app.get("/api/vision")
async def api_vision() -> dict[str, Any]:
    try:
        return json.loads(VISION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"schema": "fleet_vision.v1", "ventures": []}


@app.get("/api/kanban")
async def api_kanban() -> dict[str, Any]:
    return {"tasks": []}


@app.post("/api/send")
async def api_send(body: SendRequest) -> dict[str, Any]:
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="empty text")
    msg_id = body.msg_id or str(uuid.uuid4())
    return await natsio.send(STATE, CFG, ROSTER, body.text, body.to, msg_id)


# --- SSE ---------------------------------------------------------------------


def _sse_frame(envelope: dict[str, Any]) -> str:
    return (
        f"id: {envelope['id']}\n"
        f"event: {envelope['event']}\n"
        f"data: {json.dumps(envelope['data'])}\n\n"
    )


@app.get("/events/stream")
async def events_stream(request: Request) -> StreamingResponse:
    bus = STATE.bus
    q = bus.attach()
    last_event_id = request.headers.get("last-event-id")

    async def gen():
        try:
            hello = {"epoch": bus.epoch, "resume": bus.n, "version": VERSION}
            yield f"event: hello\ndata: {json.dumps(hello)}\n\n"
            backlog, needs_reset = bus.since(last_event_id)
            if needs_reset:
                yield "event: reset\ndata: {}\n\n"
            else:
                for envelope in backlog:
                    yield _sse_frame(envelope)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    envelope = await asyncio.wait_for(q.get(), timeout=15)
                    yield _sse_frame(envelope)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.detach(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Static ------------------------------------------------------------------


@app.get("/")
@app.get("/index.html")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
