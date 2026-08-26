"""NATS connection loop, JetStream startup replay, message handling, send."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
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
_SECRET_FRAGMENTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)
MAX_INBOUND_BYTES = 64 * 1024
MAX_SUBJECT_CHARS = 256
MAX_INBOUND_TEXT_CHARS = 8_000
MAX_WIRE_ID_CHARS = 128
MAX_RAW_PREVIEW_CHARS = 160
DEFAULT_DEDUPE_NAMESPACE = "fleet-hub-v1"
BROKER_OPERATION_TIMEOUT_S = 3.0
MAX_REPLAY_MESSAGES = 5_000
REPLAY_FETCH_BATCH = 500
_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SUBJECT = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")


def build_indexes(roster: dict) -> dict:
    """Precompute subject->uid and lowercase-name->uid lookup tables."""
    agents = roster.get("agents") or {}
    by_subject: dict[str, str] = {}
    by_name: dict[str, str] = {}
    ambiguous_subjects: set[str] = set()
    ambiguous_names: set[str] = set()
    for uid, info in agents.items():
        if not isinstance(info, dict):
            continue
        subject = info.get("subject")
        if isinstance(uid, str) and isinstance(subject, str) and _safe_subject(subject):
            previous = by_subject.get(subject)
            if previous is not None and previous != uid:
                ambiguous_subjects.add(subject)
                by_subject.pop(subject, None)
            elif subject not in ambiguous_subjects:
                by_subject[subject] = uid
        for name in (uid, info.get("callsign"), info.get("display_name")):
            if isinstance(name, str) and name:
                key = name.casefold()
                previous = by_name.get(key)
                if previous is not None and previous != uid:
                    ambiguous_names.add(key)
                    by_name.pop(key, None)
                elif key not in ambiguous_names:
                    by_name[key] = uid
    return {
        "agents": agents,
        "by_subject": by_subject,
        "by_name": by_name,
        "ambiguous_subjects": ambiguous_subjects,
        "ambiguous_names": ambiguous_names,
    }


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
    wire_payload = payload.get("payload")
    if isinstance(wire_payload, dict):
        value = wire_payload.get("text")
        if isinstance(value, str):
            return value
    return ""


def _safe_claim(value, depth: int = 0):
    """Bound and redact a wire value before it enters the public raw feed."""

    if depth >= 3:
        return "[truncated]"
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 24:
                result["_truncated"] = True
                break
            if not isinstance(key, str):
                continue
            key_text = key[:80]
            if any(fragment in key_text.lower() for fragment in _SECRET_FRAGMENTS):
                result[key_text] = "[redacted]"
            else:
                result[key_text] = _safe_claim(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_safe_claim(item, depth + 1) for item in value[:24]]
    if isinstance(value, str):
        return value[:500]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "[unsupported]"
    return "[unsupported]"


def _raw_preview(payload: dict | None) -> str:
    if payload is None:
        return "[non-json payload omitted]"
    return json.dumps(
        _safe_claim(payload), ensure_ascii=True, separators=(",", ":")
    )[:MAX_RAW_PREVIEW_CHARS]


def _safe_subject(subject: object) -> str | None:
    if not isinstance(subject, str) or not 1 <= len(subject) <= MAX_SUBJECT_CHARS:
        return None
    return subject if _SUBJECT.fullmatch(subject) else None


def _subject_preview(subject: object) -> str:
    if not isinstance(subject, str):
        return "[invalid-subject]"
    return "".join(
        char if 0x21 <= ord(char) < 0x7F else "?"
        for char in subject[:MAX_SUBJECT_CHARS]
    )


def _safe_wire_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_WIRE_ID_CHARS:
        return None
    return value if _WIRE_ID.fullmatch(value) else None


def outbound_echo_key(subject: str, data: bytes) -> str:
    """Scope echo suppression to the exact subject and canonical wire bytes."""

    digest = hashlib.sha256(subject.encode("utf-8") + b"\x00" + data).hexdigest()
    return f"wire:{digest}"


def _record_raw(
    state,
    *,
    subject: str,
    now: float,
    live: bool,
    preview: str,
    quarantine_reason: str | None = None,
    size_bytes: int | None = None,
) -> None:
    """Append only bounded public raw metadata, optionally as quarantine."""

    state.messages += 1
    entry = {
        "seq": state.messages,
        "subject": subject[:MAX_SUBJECT_CHARS],
        "preview": preview[:MAX_RAW_PREVIEW_CHARS],
        "ts": presence_mod.iso(now),
        "quarantined": quarantine_reason is not None,
        "quarantine_reason": quarantine_reason,
        "size_bytes": size_bytes,
    }
    state.raw.append(entry)
    if live:
        state.bus.publish("raw", {"n": state.messages, **entry})


def _quarantine(
    state,
    *,
    subject: object,
    now: float,
    live: bool,
    reason: str,
    size_bytes: int | None,
) -> None:
    _record_raw(
        state,
        subject=_subject_preview(subject),
        now=now,
        live=live,
        preview=f"[quarantined:{reason}]",
        quarantine_reason=reason,
        size_bytes=size_bytes,
    )


def handle_msg(
    state,
    cfg,
    roster_indexes: dict,
    subject: str,
    data: bytes,
    now: float,
    *,
    live: bool,
    verified_sender_uid: str | None = None,
) -> None:
    """Sync core of message handling — replay (live=False) and live traffic
    share it; replay never touches the SSE bus."""
    safe_subject = _safe_subject(subject)
    if safe_subject is None:
        _quarantine(
            state,
            subject=subject,
            now=now,
            live=live,
            reason="invalid_subject",
            size_bytes=len(data) if hasattr(data, "__len__") else None,
        )
        return
    if not isinstance(data, (bytes, bytearray, memoryview)):
        _quarantine(
            state,
            subject=safe_subject,
            now=now,
            live=live,
            reason="invalid_payload_type",
            size_bytes=None,
        )
        return
    size_bytes = len(data)
    if size_bytes > MAX_INBOUND_BYTES:
        # Check before decode/json parsing so an oversized broker frame never
        # creates a second full-size string or object graph in Fleet Hub.
        _quarantine(
            state,
            subject=safe_subject,
            now=now,
            live=live,
            reason="payload_too_large",
            size_bytes=size_bytes,
        )
        return

    parsed_payload: dict | None
    try:
        parsed = json.loads(bytes(data))
        parsed_payload = parsed if isinstance(parsed, dict) else None
    except Exception:
        parsed_payload = None
    payload = parsed_payload or {}

    text = _extract_text(payload)
    if len(text) > MAX_INBOUND_TEXT_CHARS:
        _quarantine(
            state,
            subject=safe_subject,
            now=now,
            live=live,
            reason="text_too_large",
            size_bytes=size_bytes,
        )
        return
    msg_id = _safe_wire_id(payload.get("message_id")) or _safe_wire_id(
        payload.get("msg_id")
    )
    if msg_id is None:
        # Stable across startup replay: wall-clock replay time is not part of
        # the synthesized identity.
        digest = hashlib.sha256(
            safe_subject.encode() + b"\x00" + bytes(data)
        ).hexdigest()[:20]
        msg_id = f"srv-{digest}"

    # Presence: subject observation proves only that traffic was addressed.
    # Payload sender fields remain a separate reported observation.  A caller
    # must supply identity-bound evidence explicitly to update last_heard.
    changed: set[str] = set()
    addressed_uid = presence_mod.uid_for_subject(
        safe_subject, roster_indexes["by_subject"]
    )
    if addressed_uid:
        rec = state.presence.setdefault(
            addressed_uid, {"last_heard": None, "last_addressed": None}
        )
        if rec.get("last_addressed") != now:
            rec["last_addressed"] = now
            rec["last_addressed_source"] = "nats.subject_observation"
            rec["last_addressed_verification"] = "observed_transport"
            changed.add(addressed_uid)
    reported_sender = presence_mod.reported_sender(payload)
    sender_uid = presence_mod.resolve_sender(payload, roster_indexes["by_name"])
    if sender_uid:
        rec = state.presence.setdefault(
            sender_uid, {"last_heard": None, "last_addressed": None}
        )
        if rec.get("last_reported_heard") != now:
            rec["last_reported_heard"] = now
            rec["last_reported_sender"] = reported_sender
            rec["last_reported_heard_source"] = "nats.payload_sender_claim"
            changed.add(sender_uid)
    if verified_sender_uid in roster_indexes["agents"]:
        rec = state.presence.setdefault(
            verified_sender_uid, {"last_heard": None, "last_addressed": None}
        )
        if rec.get("last_heard") != now:
            rec["last_heard"] = now
            rec["last_heard_source"] = "nats.identity_bound_transport"
            rec["last_heard_verification"] = "identity_bound_transport"
            changed.add(verified_sender_uid)
    if live:
        for uid in sorted(changed):
            rec = state.presence[uid]
            state.bus.publish(
                "presence",
                {
                    "uid": uid,
                    "last_heard": presence_mod.iso(
                        presence_mod.verified_last_heard(rec)
                    ),
                    "last_addressed": presence_mod.iso(rec.get("last_addressed")),
                    "contact": presence_mod.contact(
                        presence_mod.verified_last_heard(rec),
                        rec.get("last_addressed"),
                    ),
                    "freshness": presence_mod.freshness(
                        presence_mod.verified_last_heard(rec), now
                    ),
                    "signals": {
                        "last_heard": {
                            "value": presence_mod.iso(
                                presence_mod.verified_last_heard(rec)
                            ),
                            "source": rec.get("last_heard_source") or "unattributed",
                            "observed_at": presence_mod.iso(
                                presence_mod.verified_last_heard(rec)
                            ),
                            "ttl_s": cfg.recent_window_s,
                            "verification": rec.get("last_heard_verification")
                            or "unknown",
                        },
                        "last_addressed": {
                            "value": presence_mod.iso(rec.get("last_addressed")),
                            "source": rec.get("last_addressed_source") or "unattributed",
                            "observed_at": presence_mod.iso(rec.get("last_addressed")),
                            "ttl_s": cfg.recent_window_s,
                            "verification": rec.get("last_addressed_verification")
                            or "unknown",
                        },
                        "reported_sender": {
                            "value": rec.get("last_reported_sender"),
                            "matched_uid": uid
                            if rec.get("last_reported_heard") is not None
                            else None,
                            "source": rec.get("last_reported_heard_source")
                            or "unattributed",
                            "observed_at": presence_mod.iso(
                                rec.get("last_reported_heard")
                            ),
                            "ttl_s": cfg.recent_window_s,
                            "verification": "reported_unverified"
                            if rec.get("last_reported_heard") is not None
                            else "unknown",
                        },
                    },
                },
            )

    # Our own message coming back off the wire: presence already updated,
    # the chat/dm entry was appended locally at send time — stop here.
    if outbound_echo_key(safe_subject, bytes(data)) in state.sent:
        return

    _record_raw(
        state,
        subject=safe_subject,
        now=now,
        live=live,
        preview=_raw_preview(parsed_payload),
        size_bytes=size_bytes,
    )
    ts = presence_mod.iso(now)

    sender_claim = {
        "value": reported_sender,
        "status": "reported_unverified",
        "source": "nats.payload",
        "matched_roster_uid": sender_uid,
    }
    common = {
        "msg_id": msg_id,
        "message_id": msg_id,
        "from": reported_sender,
        "sender_claim": sender_claim,
        "correlation_id": _safe_wire_id(payload.get("correlation_id")),
        "trace_id": _safe_wire_id(payload.get("trace_id")),
        "ts": ts,
    }
    if safe_subject == cfg.chat_subject and text:
        message = {**common, "text": text, "subject": safe_subject}
        state.chat.append(message)
        if live:
            state.bus.publish("chat", message)
    elif safe_subject.startswith("dharma.a2a.") and addressed_uid and text:
        message = {**common, "uid": addressed_uid, "text": text}
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
    truncated = False
    stream_last_seq: dict[str, int] = {}

    # Pass A — last message on each active agent's subject.
    for uid, info in indexes["agents"].items():
        info = info or {}
        if (info.get("seat") or "active") != "active":
            continue
        subject = info.get("subject")
        if not subject:
            continue
        try:
            msg = await asyncio.wait_for(
                state.js.get_last_msg(cfg.stream, subject),
                timeout=BROKER_OPERATION_TIMEOUT_S,
            )
            ts = None
            if getattr(msg, "time", None):
                ts = _parse_rfc3339(str(msg.time))
            if ts is not None:
                rec = state.presence.setdefault(
                    uid, {"last_heard": None, "last_addressed": None}
                )
                if rec.get("last_addressed") is None or ts > rec["last_addressed"]:
                    rec["last_addressed"] = ts
                    rec["last_addressed_source"] = "jetstream.subject_observation"
                    rec["last_addressed_verification"] = "observed_transport"
        except Exception as exc:
            name = type(exc).__name__
            if "NotFound" not in name:  # no message on subject is a normal state
                errors.append(f"get_last_msg_failed:{name}")

    # Pass B — walk the replay window for sender attribution + chat backlog.
    start_time = (
        datetime.now(timezone.utc) - timedelta(hours=cfg.replay_hours)
    ).isoformat()
    for stream in cfg.replay_streams:
        psub = None
        try:
            psub = await asyncio.wait_for(
                state.js.pull_subscribe(
                    "",
                    stream=stream,
                    config=ConsumerConfig(
                        deliver_policy=DeliverPolicy.BY_START_TIME,
                        opt_start_time=start_time,
                    ),
                ),
                timeout=BROKER_OPERATION_TIMEOUT_S,
            )
            while scanned < MAX_REPLAY_MESSAGES:
                try:
                    msgs = await asyncio.wait_for(
                        psub.fetch(REPLAY_FETCH_BATCH, timeout=2),
                        timeout=BROKER_OPERATION_TIMEOUT_S,
                    )
                except Exception as exc:
                    if "timeout" in type(exc).__name__.lower():
                        break  # window exhausted — normal end of replay
                    raise
                if not msgs:
                    break
                remaining = MAX_REPLAY_MESSAGES - scanned
                selected = msgs[:remaining]
                if len(msgs) > remaining:
                    truncated = True
                for msg in selected:
                    scanned += 1
                    ts = time.time()
                    try:
                        meta = msg.metadata
                        ts = meta.timestamp.timestamp()
                        seq = meta.sequence.stream
                        previous = stream_last_seq.get(stream)
                        if previous is None or seq > previous:
                            stream_last_seq[stream] = seq
                        # Stream sequence numbers are comparable only within a
                        # stream. The legacy aggregate field describes the
                        # configured primary stream, never a cross-stream max.
                        if stream == cfg.stream and (
                            state.last_seq is None or seq > state.last_seq
                        ):
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
                        await asyncio.wait_for(
                            msg.ack(), timeout=BROKER_OPERATION_TIMEOUT_S
                        )
                    except Exception:
                        pass
                if scanned >= MAX_REPLAY_MESSAGES:
                    # We deliberately do not fetch once more just to prove
                    # exhaustion: reaching the public work cap is reported as
                    # potentially truncated, even if this batch happened to be
                    # the exact end of the stream.
                    truncated = True
                    break
                if len(msgs) < REPLAY_FETCH_BATCH:
                    break
        except Exception as exc:
            errors.append(f"replay_failed:{type(exc).__name__}")
        finally:
            if psub is not None:
                try:
                    await asyncio.wait_for(
                        psub.unsubscribe(), timeout=BROKER_OPERATION_TIMEOUT_S
                    )
                except Exception:
                    pass

    state.replay = {
        "ok": not errors,
        "complete": not errors and not truncated,
        "truncated": truncated,
        "limit": MAX_REPLAY_MESSAGES,
        "scanned": scanned,
        "stream_last_seq": stream_last_seq,
        "took_ms": int((time.time() - started) * 1000),
        "error": ";".join(errors) or None,
        "scope": "startup_backfill",
        "durable_resume": False,
        "ran_at": presence_mod.iso(time.time()),
    }


async def nats_loop(state, cfg, roster) -> None:
    """Reconnect loop with honest state: connected flips only on a real
    connection, every failure lands in state.last_error."""
    if nats is None:
        state.last_error = "nats-py not importable"
        return
    indexes = build_indexes(roster)

    async def _on_error(exc: Exception) -> None:
        # Replaces nats-py's default stderr traceback; the error still
        # surfaces honestly via state.last_error / /api/health.
        state.last_error = f"nats_client_error:{type(exc).__name__}"

    while True:
        nc = None
        try:
            nc = await nats.connect(
                cfg.url,
                user=cfg.user,
                password=cfg.password,
                name="fleet-hub-v1-candidate",
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
                except Exception as exc:
                    state.last_error = f"handle_message_failed:{type(exc).__name__}"

            await nc.subscribe("dharma.a2a.>", cb=on_msg)
            await nc.subscribe(cfg.chat_subject, cb=on_msg)
            while nc.is_connected:
                await asyncio.sleep(2)
            state.connected = False
            state.last_error = "connection lost"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.connected = False
            state.last_error = f"nats_connect_failed:{type(exc).__name__}"
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


def _chat_envelope(
    *,
    subject: str,
    text: str,
    uid: str | None,
    message_id: str,
    correlation_id: str,
    causation_id: str,
    trace_id: str,
    timestamp: str,
) -> tuple[dict, dict]:
    """Build the canonical envelope plus an explicit, non-fan-out route plan."""

    span_id = hashlib.sha256(f"span:{message_id}".encode()).hexdigest()[:16]
    if uid is None:
        route_plan = {
            "mode": "group_transcript",
            "subject": subject,
            "recipient_uid": None,
            "fanout": False,
            "semantic_reply_promised": False,
            "claim": "one transcript publish",
        }
        to_agent = "fleet-transcript"
    else:
        route_plan = {
            "mode": "direct_message",
            "subject": subject,
            "recipient_uid": uid,
            "fanout": False,
            "semantic_reply_promised": False,
            "claim": "one UID-addressed publish",
        }
        to_agent = uid
    payload = {
        "schema": "dharma.nats.envelope.v1",
        "message_id": message_id,
        "msg_id": message_id,  # v0.6 compatibility alias
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": "",
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "subject": subject,
        "from_agent": "operator",
        "from": "operator",  # v0.6 compatibility alias; server-derived
        "to_agent": to_agent,
        "to": uid,
        "actor": {
            "from_agent": "operator",
            "to_agent": to_agent,
            "identity_source": "fleet_hub_authenticated_principal",
        },
        "causality": {
            "message_id": message_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
        },
        "kind": "message",
        "created_at": timestamp,
        "timestamp": timestamp,
        "requires_ack": False,
        "route_plan": route_plan,
        "payload": {
            "schema": "fleet.chat.message.v1",
            "text": text,
            "route_plan": route_plan,
        },
        "text": text,  # v0.6 compatibility alias
        "via": "fleet-hub-v1-candidate",
    }
    return payload, route_plan


def broker_dedupe_id(
    *, namespace: str, principal_scope: str, idempotency_key: str
) -> str:
    """Derive an opaque JetStream dedupe ID scoped to app/deployment/principal.

    The authenticated principal and caller key are inputs to the digest but are
    never placed in the broker header itself.
    """

    components = (namespace, principal_scope, idempotency_key)
    limits = (128, 256, MAX_WIRE_ID_CHARS)
    if any(
        not isinstance(value, str) or not value or len(value) > limit
        for value, limit in zip(components, limits, strict=True)
    ):
        raise ValueError("invalid broker dedupe scope")
    digest = hashlib.sha256("\x1f".join(components).encode("utf-8")).hexdigest()
    return f"fh1-{digest}"


async def send(
    state,
    cfg,
    roster,
    text: str,
    to: str | None,
    msg_id: str,
    *,
    principal_scope: str,
    idempotency_key: str | None = None,
    correlation_id: str | None = None,
    causation_id: str = "",
    trace_id: str | None = None,
    require_jetstream: bool = False,
) -> dict:
    """Publish one operator chat intent with precise transport claims.

    ``require_jetstream`` is used by the v1 intent route. The v0.6-compatible
    route may still fall back to Core NATS, but that path is labelled ``NO_ACK``
    and is never presented as broker storage or semantic delivery.
    """
    indexes = build_indexes(roster)
    uid = None
    if not to:
        subject = cfg.chat_subject
    else:
        recipient_key = to.casefold()
        if recipient_key in indexes["ambiguous_names"]:
            return {"ok": False, "accepted": False, "error": "ambiguous_recipient"}
        uid = indexes["by_name"].get(recipient_key)
        info = indexes["agents"].get(uid) if uid else None
        if isinstance(info, dict) and info.get("seat") == "archived":
            return {"ok": False, "accepted": False, "error": "recipient_archived"}
        subject = (info or {}).get("subject")
        if not subject:
            return {
                "ok": False,
                "accepted": False,
                "error": "unknown_recipient",
            }

    if state.nc is None:
        return {"ok": False, "accepted": False, "error": "nats_unavailable"}

    now = time.time()
    ts = presence_mod.iso(now)
    correlation_id = correlation_id or f"corr-{msg_id}"
    trace_id = trace_id or hashlib.sha256(f"trace:{msg_id}".encode()).hexdigest()[:32]
    payload, route_plan = _chat_envelope(
        subject=subject,
        text=text,
        uid=uid,
        message_id=msg_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        trace_id=trace_id,
        timestamp=ts,
    )
    data = json.dumps(payload).encode()
    try:
        dedupe_id = broker_dedupe_id(
            namespace=getattr(cfg, "dedupe_namespace", DEFAULT_DEDUPE_NAMESPACE),
            principal_scope=principal_scope,
            idempotency_key=idempotency_key or msg_id,
        )
    except ValueError:
        return {
            "ok": False,
            "accepted": False,
            "error": "dedupe_scope_invalid",
            "msg_id": msg_id,
            "message_id": msg_id,
            "correlation_id": correlation_id,
            "trace_id": trace_id,
            "route_plan": route_plan,
        }
    headers = {
        "Nats-Msg-Id": dedupe_id,
        "Dharma-Nats-Schema": "dharma.nats.envelope.v1",
        "Dharma-Correlation-Id": correlation_id,
        "Dharma-Trace-Id": trace_id,
        "Content-Type": "application/json",
    }
    # Establish the exact outbound identity before awaiting publish: a local
    # subscription can echo the frame before the PubAck coroutine resumes.
    echo_key = outbound_echo_key(subject, data)
    state.sent.add(echo_key)

    seq = None
    ack_tier = None
    duplicate = False
    try:
        if state.js is None:
            raise RuntimeError("no jetstream context")
        ack = await asyncio.wait_for(
            state.js.publish(subject, data, headers=headers, timeout=2.0),
            timeout=BROKER_OPERATION_TIMEOUT_S,
        )
        seq = getattr(ack, "seq", None)
        duplicate = bool(getattr(ack, "duplicate", False))
        ack_tier = (
            "DEDUPLICATED_UNVERIFIED" if duplicate else "PUBLISH_ACCEPTED"
        )
    except Exception:
        if require_jetstream:
            state.sent.discard(echo_key)
            return {
                "ok": False,
                "accepted": False,
                "error": "jetstream_publish_unavailable",
                "msg_id": msg_id,
                "message_id": msg_id,
                "correlation_id": correlation_id,
                "trace_id": trace_id,
                "route_plan": route_plan,
            }
        try:
            await asyncio.wait_for(
                state.nc.publish(subject, data),
                timeout=BROKER_OPERATION_TIMEOUT_S,
            )
            ack_tier = "NO_ACK"
        except Exception:
            state.sent.discard(echo_key)
            return {
                "ok": False,
                "accepted": False,
                "error": "core_publish_failed",
                "msg_id": msg_id,
                "message_id": msg_id,
                "correlation_id": correlation_id,
                "trace_id": trace_id,
                "route_plan": route_plan,
            }

    if (
        not duplicate
        and seq is not None
        and (state.last_seq is None or seq > state.last_seq)
    ):
        state.last_seq = seq

    if not duplicate:
        common = {
            "msg_id": msg_id,
            "message_id": msg_id,
            "from": "operator",
            "sender_claim": {
                "value": "operator",
                "status": "authenticated_server_derived",
                "source": "fleet_hub_session",
                "matched_roster_uid": None,
            },
            "text": text,
            "ts": ts,
            "correlation_id": correlation_id,
            "trace_id": trace_id,
            "route_plan": route_plan,
        }
        if uid is None:
            message = {**common, "subject": subject}
            state.chat.append(message)
            state.bus.publish("chat", message)
        else:
            message = {**common, "uid": uid}
            state.dms.setdefault(uid, deque(maxlen=200)).append(message)
            state.bus.publish("dm", message)

    return {
        "ok": True,
        "accepted": ack_tier in {"PUBLISH_ACCEPTED", "DEDUPLICATED_UNVERIFIED"},
        "accepted_by": (
            "jetstream_dedupe_window" if duplicate else "jetstream"
        )
        if ack_tier in {"PUBLISH_ACCEPTED", "DEDUPLICATED_UNVERIFIED"}
        else None,
        "msg_id": msg_id,
        "message_id": msg_id,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "seq": seq,
        "subject": subject,
        "ack_tier": ack_tier,
        "duplicate": duplicate,
        "deduplicated": duplicate,
        "new_storage_event": ack_tier == "PUBLISH_ACCEPTED",
        "current_body_stored": True
        if ack_tier == "PUBLISH_ACCEPTED"
        else None,
        "dedupe_scope": "app_deployment+authenticated_principal+idempotency_key",
        "idempotency_scope": "process_local_then_jetstream_window",
        "route_plan": route_plan,
        "semantic_effect": "unobserved",
    }
