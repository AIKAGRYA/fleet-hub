#!/usr/bin/env python3
"""Dharma Fleet Hub v0.5 — AGNI-hosted operator console.

P0: token gate, last-seen presence (not hardcoded live), real status.
P1: phone-first static UI, broker health, browser notifications (frontend).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import nats
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(os.environ.get("FLEET_HUB_ROOT", Path(__file__).resolve().parent))
STATIC = ROOT / "static"
ROSTER_PATH = Path(os.environ.get("FLEET_HUB_ROSTER", ROOT / "roster.json"))
TOKEN = (os.environ.get("FLEET_HUB_TOKEN") or "").strip()
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
NATS_USER = os.environ.get("NATS_USER") or None
NATS_PASS = os.environ.get("NATS_PASS") or os.environ.get("NATS_PASSWORD") or None
STREAM = os.environ.get("NATS_STREAM", "DHARMA_A2A")
CHAT_SUBJECT = os.environ.get("NATS_CHAT_SUBJECT", "dharma.fleet.chat")
LIVE_WINDOW_S = int(os.environ.get("FLEET_LIVE_WINDOW_S", "300"))

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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_roster() -> dict[str, Any]:
    if ROSTER_PATH.exists():
        return json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    return {"agents": {}, "count": 0}


class HubState:
    def __init__(self) -> None:
        self.nc: Any = None
        self.js: Any = None
        self.connected = False
        self.last_seq = 0
        self.messages = 0
        self.last_error = ""
        self.chat: list[dict[str, Any]] = []
        self.raw: list[dict[str, Any]] = []
        self.dms: dict[str, list[dict[str, Any]]] = {}
        self.last_seen: dict[str, str] = {}
        self.sse_waiters: dict[str, list[asyncio.Queue]] = {}
        self.lock = asyncio.Lock()

    async def emit(self, channel: str, event: dict[str, Any]) -> None:
        for q in list(self.sse_waiters.get(channel, [])):
            try:
                q.put_nowait(event)
            except Exception:
                pass


STATE = HubState()
BASE_ROSTER = load_roster()


def token_ok(request: Request, token: str | None = None) -> bool:
    if not TOKEN:
        return True
    if token and token == TOKEN:
        return True
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer ") and auth.split(" ", 1)[1].strip() == TOKEN:
        return True
    if request.cookies.get("fleet_token") == TOKEN:
        return True
    q = request.query_params.get("token")
    return q == TOKEN


def require_auth(request: Request, token: str | None = None) -> None:
    if not token_ok(request, token):
        raise HTTPException(status_code=401, detail="auth required")


def agent_status(uid: str, info: dict[str, Any]) -> str:
    ts = STATE.last_seen.get(uid)
    if not ts:
        return "offline"
    try:
        age = time.time() - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        return "live" if age <= LIVE_WINDOW_S else "offline"
    except Exception:
        return "offline"


def decorate_roster() -> dict[str, Any]:
    agents = {}
    for uid, info in (BASE_ROSTER.get("agents") or {}).items():
        row = dict(info)
        seen = STATE.last_seen.get(uid)
        row["last_seen"] = seen
        row["status"] = agent_status(uid, info)
        agents[uid] = row
    return {"agents": agents, "count": len(agents), "live_window_s": LIVE_WINDOW_S}


def uid_for_subject(subject: str) -> str | None:
    for uid, info in (BASE_ROSTER.get("agents") or {}).items():
        if info.get("subject") == subject:
            return uid
    return None


app = FastAPI(title="Dharma Fleet Hub", version="0.5.0")


@app.middleware("http")
async def auth_mw(request: Request, call_next):
    path = request.url.path
    if path in {"/healthz", "/login"} or path.startswith("/static"):
        return await call_next(request)
    # login page itself
    if path in {"/", "/index.html"} and request.method == "GET":
        return await call_next(request)
    if path.startswith("/api") or path.startswith("/events"):
        if not token_ok(request):
            return JSONResponse({"detail": "auth required"}, status_code=401)
    return await call_next(request)


class SendRequest(BaseModel):
    from_: str = Field(default="operator", alias="from_")
    text: str
    to: str | None = None

    model_config = {"populate_by_name": True}


class LoginRequest(BaseModel):
    token: str


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "nats": STATE.connected, "version": "0.5.0"}


@app.post("/login")
async def login(body: LoginRequest) -> JSONResponse:
    if TOKEN and body.token != TOKEN:
        raise HTTPException(status_code=401, detail="bad token")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("fleet_token", body.token, httponly=False, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "nats": {
            "connected": STATE.connected,
            "stream": STREAM,
            "messages": STATE.messages,
            "last_seq": STATE.last_seq,
        },
        "sse_channels": {k: len(v) for k, v in STATE.sse_waiters.items()},
        "timestamp": utc_now(),
        "version": "0.5.0",
    }


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    return {
        "ok": STATE.connected,
        "version": "0.5.0",
        "broker": {
            "connected": STATE.connected,
            "url": NATS_URL,
            "stream": STREAM,
            "messages": STATE.messages,
            "last_seq": STATE.last_seq,
            "error": STATE.last_error,
        },
        "presence": {
            uid: {"last_seen": ts, "status": agent_status(uid, {})}
            for uid, ts in STATE.last_seen.items()
        },
        "timestamp": utc_now(),
    }


@app.get("/api/presence")
async def api_presence() -> dict[str, Any]:
    return decorate_roster()


@app.get("/api/nodes")
async def api_nodes() -> dict[str, Any]:
    return {"nodes": NODES, "timestamp": utc_now()}


@app.get("/api/roster")
async def api_roster() -> dict[str, Any]:
    return decorate_roster()


@app.get("/api/agent/{uid}")
async def api_agent(uid: str) -> dict[str, Any]:
    info = (BASE_ROSTER.get("agents") or {}).get(uid)
    if not info:
        raise HTTPException(status_code=404, detail="unknown agent")
    row = dict(info)
    row["last_seen"] = STATE.last_seen.get(uid)
    row["status"] = agent_status(uid, info)
    return row


@app.get("/api/chat")
async def api_chat() -> dict[str, Any]:
    return {"messages": STATE.chat[-200:]}


@app.get("/api/kanban")
async def api_kanban() -> dict[str, Any]:
    # Keep the existing shape the v0.4 UI already consumes.
    return {"tasks": []}


@app.get("/api/dispatch")
async def api_dispatch(task: str) -> dict[str, Any]:
    return {
        "decision": "self_handle_unaided",
        "agent": None,
        "confidence": 0.0,
        "rationale": "wayfinder optional in v0.5; broadcast unless a live uid is named",
        "task": task,
    }


@app.post("/api/send")
async def api_send(body: SendRequest) -> dict[str, Any]:
    if not body.text.strip():
        return {"ok": False, "error": "empty"}
    if STATE.nc is None:
        return {"ok": False, "error": "nats down"}
    target = body.to or "group"
    subject = CHAT_SUBJECT
    dispatch = None
    agents = BASE_ROSTER.get("agents") or {}
    if target not in (None, "", "group", "wayfinder") and target in agents:
        subject = agents[target]["subject"]
    payload = {
        "from": body.from_,
        "text": body.text,
        "to": target,
        "timestamp": utc_now(),
        "via": "fleet-hub-v0.5",
    }
    ack = await STATE.nc.publish(subject, json.dumps(payload).encode())
    ev = {
        "type": "message",
        "from": body.from_,
        "text": body.text,
        "ts": payload["timestamp"],
        "subject": subject,
    }
    if target in (None, "", "group", "wayfinder"):
        STATE.chat.append(ev)
        await STATE.emit("group", ev)
    else:
        STATE.dms.setdefault(target, []).append(ev)
        await STATE.emit(f"agent:{target}", ev)
    return {"ok": True, "seq": getattr(ack, "seq", None), "subject": subject, "dispatch": dispatch}


@app.get("/events/{channel}")
async def sse_events(channel: str, request: Request) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue()
    STATE.sse_waiters.setdefault(channel, []).append(q)

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'connected', 'channel': channel})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ":\n\n"
        finally:
            lst = STATE.sse_waiters.get(channel) or []
            if q in lst:
                lst.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def note_presence(subject: str, ts: str) -> None:
    uid = uid_for_subject(subject)
    if uid:
        STATE.last_seen[uid] = ts


async def on_msg(msg: Any) -> None:
    ts = utc_now()
    try:
        data = json.loads(msg.data.decode("utf-8", errors="replace"))
    except Exception:
        data = {"raw": msg.data[:200].decode("utf-8", errors="replace")}
    STATE.last_seq = getattr(msg, "seq", STATE.last_seq)
    STATE.messages += 1
    note_presence(msg.subject, ts)
    ev = {
        "type": "nats",
        "subject": msg.subject,
        "seq": getattr(msg, "seq", None),
        "data": data,
        "timestamp": ts,
    }
    STATE.raw.append(ev)
    STATE.raw = STATE.raw[-400:]
    await STATE.emit("raw", ev)
    text = ""
    if isinstance(data, dict):
        text = str(data.get("text") or data.get("body") or data.get("message") or "")
        if isinstance(data.get("body"), dict):
            text = str(data["body"].get("text") or text)
    frm = data.get("from") if isinstance(data, dict) else "nats"
    if msg.subject == CHAT_SUBJECT and text:
        chat_ev = {"from": frm, "text": text, "ts": ts, "type": "message"}
        STATE.chat.append(chat_ev)
        STATE.chat = STATE.chat[-300:]
        await STATE.emit("group", chat_ev)
    uid = uid_for_subject(msg.subject)
    if uid and text:
        dm = {"from": frm, "text": text, "ts": ts, "type": "message"}
        STATE.dms.setdefault(uid, []).append(dm)
        await STATE.emit(f"agent:{uid}", dm)


async def nats_loop() -> None:
    while True:
        try:
            STATE.nc = await nats.connect(
                NATS_URL,
                user=NATS_USER,
                password=NATS_PASS,
                name="fleet-hub-v05",
            )
            STATE.js = STATE.nc.jetstream()
            STATE.connected = True
            STATE.last_error = ""
            await STATE.nc.subscribe("dharma.a2a.>", cb=on_msg)
            await STATE.nc.subscribe(CHAT_SUBJECT, cb=on_msg)
            while STATE.nc.is_connected:
                await asyncio.sleep(2)
        except Exception as e:
            STATE.connected = False
            STATE.last_error = f"{type(e).__name__}: {e}"[:300]
        await asyncio.sleep(3)


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(nats_loop())
