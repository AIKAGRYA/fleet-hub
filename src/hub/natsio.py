"""NATS connection loop, JetStream startup replay, message handling, send."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone

try:
    import nats
    from nats.js.api import ConsumerConfig, DeliverPolicy
except Exception:  # pragma: no cover — hub must import without a working nats-py
    nats = None
    ConsumerConfig = None
    DeliverPolicy = None

from . import presence as presence_mod

_TEXT_KEYS = ("text", "message", "body")


def build_indexes(roster: dict) -> dict:
    """Precompute subject->uid and lowercase-name->uid lookup tables."""
    agents = roster.get("agents") or {}
    by_subject: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for uid, info in agents.items():
        info = info or {}
        subject = info.get("subject")
        if subject:
            by_subject[subject] = uid
        for name in (uid, info.get("callsign"), info.get("display_name")):
            if isinstance(name, str) and name:
                by_name[name.lower()] = uid
    return {"agents": agents, "by_subject": by_subject, "by_name": by_name}


def _extract_text(payload: dict) -> str:
    for key in _TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    body = payload.get("body")
    if isinstance(body, dict):
        value = body.get("text")
        if isinstance(value, str):
            return value
    return ""


def _sender_label(payload: dict, sender_uid: str | None) -> str:
    if sender_uid:
        return sender_uid
    for key in ("from", "sender", "agent", "from_agent"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def handle_msg(
    state,
    cfg,
    roster_indexes: dict,
    subject: str,
    data: bytes,
    now: float,
    *,
    live: bool,
) -> None:
    """Sync core of message handling — replay (live=False) and live traffic
    share it; replay never touches the SSE bus."""
    text_raw = data.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text_raw)
        if not isinstance(payload, dict):
            payload = {"raw": text_raw}
    except Exception:
        payload = {"raw": text_raw[:500]}

    text = _extract_text(payload)
    msg_id = payload.get("msg_id")
    if not isinstance(msg_id, str) or not msg_id:
        digest = hashlib.sha1(f"{subject}{now}{text}".encode()).hexdigest()[:12]
        msg_id = f"srv-{digest}"

    # Presence: the addressed uid (subject match) and the heard uid (sender).
    changed: set[str] = set()
    addressed_uid = presence_mod.uid_for_subject(subject, roster_indexes["by_subject"])
    if addressed_uid:
        rec = state.presence.setdefault(
            addressed_uid, {"last_heard": None, "last_addressed": None}
        )
        if rec.get("last_addressed") != now:
            rec["last_addressed"] = now
            changed.add(addressed_uid)
    sender_uid = presence_mod.resolve_sender(payload, roster_indexes["by_name"])
    if sender_uid:
        rec = state.presence.setdefault(
            sender_uid, {"last_heard": None, "last_addressed": None}
        )
        if rec.get("last_heard") != now:
            rec["last_heard"] = now
            changed.add(sender_uid)
    if live:
        for uid in sorted(changed):
            rec = state.presence[uid]
            state.bus.publish(
                "presence",
                {
                    "uid": uid,
                    "last_heard": presence_mod.iso(rec.get("last_heard")),
                    "last_addressed": presence_mod.iso(rec.get("last_addressed")),
                    "contact": presence_mod.contact(
                        rec.get("last_heard"), rec.get("last_addressed")
                    ),
                    "freshness": presence_mod.freshness(rec.get("last_heard"), now),
                },
            )

    # Our own message coming back off the wire: presence already updated,
    # the chat/dm entry was appended locally at send time — stop here.
    if msg_id in state.sent:
        return

    state.messages += 1
    ts = presence_mod.iso(now)
    raw_entry = {
        "seq": state.messages,
        "subject": subject,
        "preview": text_raw[:160],
        "ts": ts,
    }
    state.raw.append(raw_entry)
    if live:
        state.bus.publish(
            "raw",
            {"n": state.messages, "subject": subject, "preview": text_raw[:160], "ts": ts},
        )

    sender = _sender_label(payload, sender_uid)
    if subject == cfg.chat_subject and text:
        message = {"msg_id": msg_id, "from": sender, "text": text, "ts": ts, "subject": subject}
        state.chat.append(message)
        if live:
            state.bus.publish("chat", message)
    elif subject.startswith("dharma.a2a.") and addressed_uid and text:
        message = {"msg_id": msg_id, "uid": addressed_uid, "from": sender, "text": text, "ts": ts}
        state.dms.setdefault(addressed_uid, deque(maxlen=200)).append(message)
        if live:
            state.bus.publish("dm", message)


def _parse_rfc3339(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


async def replay(state, cfg, roster) -> None:
    """Startup backfill from JetStream so the hub does not boot amnesiac.

    Pass A: get_last_msg per active roster subject -> last_addressed.
    Pass B: ephemeral by-start-time pull consumer over the replay window ->
    last_heard + chat backlog. Both passes are best-effort; failures land in
    state.replay, never crash the loop. No SSE emits — replay precedes clients.
    """
    started = time.time()
    indexes = build_indexes(roster)
    errors: list[str] = []
    scanned = 0

    # Pass A — last message on each active agent's subject.
    for uid, info in indexes["agents"].items():
        info = info or {}
        if (info.get("seat") or "active") != "active":
            continue
        subject = info.get("subject")
        if not subject:
            continue
        try:
            msg = await state.js.get_last_msg(cfg.stream, subject)
            ts = None
            if getattr(msg, "time", None):
                ts = _parse_rfc3339(str(msg.time))
            if ts is not None:
                rec = state.presence.setdefault(
                    uid, {"last_heard": None, "last_addressed": None}
                )
                if rec.get("last_addressed") is None or ts > rec["last_addressed"]:
                    rec["last_addressed"] = ts
        except Exception as e:
            name = type(e).__name__
            if "NotFound" not in name:  # no message on subject is a normal state
                errors.append(f"get_last_msg {subject}: {name}: {e}"[:200])

    # Pass B — walk the replay window for sender attribution + chat backlog.
    start_time = (
        datetime.now(timezone.utc) - timedelta(hours=cfg.replay_hours)
    ).isoformat()
    for stream in cfg.replay_streams:
        psub = None
        try:
            psub = await state.js.pull_subscribe(
                "",
                stream=stream,
                config=ConsumerConfig(
                    deliver_policy=DeliverPolicy.BY_START_TIME,
                    opt_start_time=start_time,
                ),
            )
            while scanned < 5000:
                try:
                    msgs = await psub.fetch(500, timeout=2)
                except Exception as e:
                    if "timeout" in type(e).__name__.lower():
                        break  # window exhausted — normal end of replay
                    raise
                if not msgs:
                    break
                for msg in msgs:
                    scanned += 1
                    ts = time.time()
                    try:
                        meta = msg.metadata
                        ts = meta.timestamp.timestamp()
                        seq = meta.sequence.stream
                        if state.last_seq is None or seq > state.last_seq:
                            state.last_seq = seq
                    except Exception:
                        pass
                    handle_msg(
                        state,
                        cfg,
                        indexes,
                        msg.subject,
                        msg.data,
                        ts,
                        live=False,
                    )
                    try:
                        await msg.ack()
                    except Exception:
                        pass
                if len(msgs) < 500:
                    break
        except Exception as e:
            errors.append(f"replay {stream}: {type(e).__name__}: {e}"[:200])
        finally:
            if psub is not None:
                try:
                    await psub.unsubscribe()
                except Exception:
                    pass

    state.replay = {
        "ok": not errors,
        "scanned": scanned,
        "took_ms": int((time.time() - started) * 1000),
        "error": "; ".join(errors) or None,
        "ran_at": presence_mod.iso(time.time()),
    }


async def nats_loop(state, cfg, roster) -> None:
    """Reconnect loop with honest state: connected flips only on a real
    connection, every failure lands in state.last_error."""
    if nats is None:
        state.last_error = "nats-py not importable"
        return
    indexes = build_indexes(roster)

    async def _on_error(e: Exception) -> None:
        # Replaces nats-py's default stderr traceback; the error still
        # surfaces honestly via state.last_error / /api/health.
        state.last_error = f"{type(e).__name__}: {e}"[:300]

    while True:
        nc = None
        try:
            nc = await nats.connect(
                cfg.url,
                user=cfg.user,
                password=cfg.password,
                name="fleet-hub-v0.6",
                connect_timeout=5,
                allow_reconnect=False,  # this loop owns reconnection (5s backoff)
                error_cb=_on_error,
            )
            state.nc = nc
            state.js = nc.jetstream()
            state.connected = True
            state.last_error = None
            if state.replay["ran_at"] is None:
                await replay(state, cfg, roster)

            async def on_msg(msg) -> None:
                try:
                    handle_msg(
                        state,
                        cfg,
                        indexes,
                        msg.subject,
                        msg.data,
                        time.time(),
                        live=True,
                    )
                except Exception as e:
                    state.last_error = f"handle_msg: {type(e).__name__}: {e}"[:300]

            await nc.subscribe("dharma.a2a.>", cb=on_msg)
            await nc.subscribe(cfg.chat_subject, cb=on_msg)
            while nc.is_connected:
                await asyncio.sleep(2)
            state.connected = False
            state.last_error = "connection lost"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            state.connected = False
            state.last_error = f"{type(e).__name__}: {e}"[:300]
        finally:
            if nc is not None:
                try:
                    await nc.close()
                except Exception:
                    pass
                state.nc = None
                state.js = None
                state.connected = False
        await asyncio.sleep(5)


async def send(state, cfg, roster, text: str, to: str | None, msg_id: str) -> dict:
    """Publish operator text; honest ack tiers only. The msg_id enters the
    echo LRU *before* publish so the subscription callback cannot race us
    into a duplicate bubble."""
    indexes = build_indexes(roster)
    uid = None
    if not to:
        subject = cfg.chat_subject
    else:
        uid = indexes["by_name"].get(to.lower())
        info = indexes["agents"].get(uid) if uid else None
        subject = (info or {}).get("subject")
        if not subject:
            return {"ok": False, "error": "unknown recipient"}

    if state.nc is None:
        return {"ok": False, "error": "nats down"}

    now = time.time()
    ts = presence_mod.iso(now)
    payload = {
        "msg_id": msg_id,
        "from": "operator",
        "text": text,
        "to": to,
        "timestamp": ts,
        "via": "fleet-hub-v0.6",
    }
    data = json.dumps(payload).encode()
    state.sent.add(msg_id)

    seq = None
    ack_tier = None
    try:
        if state.js is None:
            raise RuntimeError("no jetstream context")
        ack = await state.js.publish(subject, data, timeout=2.0)
        seq = getattr(ack, "seq", None)
        ack_tier = "PUBLISH_ACCEPTED"
    except Exception:
        try:
            await state.nc.publish(subject, data)
            ack_tier = "NO_ACK"
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}

    if seq is not None and (state.last_seq is None or seq > state.last_seq):
        state.last_seq = seq

    if uid is None:
        message = {"msg_id": msg_id, "from": "operator", "text": text, "ts": ts, "subject": subject}
        state.chat.append(message)
        state.bus.publish("chat", message)
    else:
        message = {"msg_id": msg_id, "uid": uid, "from": "operator", "text": text, "ts": ts}
        state.dms.setdefault(uid, deque(maxlen=200)).append(message)
        state.bus.publish("dm", message)

    return {"ok": True, "msg_id": msg_id, "seq": seq, "subject": subject, "ack_tier": ack_tier}
