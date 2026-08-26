"""Trust-separated presence: verified heard, addressed, and reported claims.

Pure functions with an injected clock — every function takes `now` as unix
seconds; nothing here calls datetime.now().
"""
from __future__ import annotations

from datetime import datetime, timezone

_FRESH_RANK = {"fresh": 0, "recent": 1, "stale": 2, "never": 3}
_SENDER_KEYS = ("from", "sender", "agent", "from_agent")
MAX_REPORTED_ID_CHARS = 200
_VERIFIED_HEARD = frozenset(
    {"identity_bound", "identity_bound_transport", "owner_verified"}
)


def iso(ts: float | None) -> str | None:
    """Serialize a unix timestamp as ISO8601 UTC ('2026-08-14T01:02:03Z')."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def uid_for_subject(subject: str, roster_index: dict) -> str | None:
    return roster_index.get(subject)


def resolve_sender(payload: dict, roster_index_by_name: dict) -> str | None:
    """Map an explicitly *unverified* payload claim to a roster uid, or None.

    This is useful for displaying which seat a claim names.  Its return value
    is never sufficient to update verified ``last_heard`` presence.
    """
    candidates: list[str] = []
    for key in _SENDER_KEYS:
        value = payload.get(key)
        if _valid_reported_id(value):
            candidates.append(value)
    body = payload.get("body")
    if isinstance(body, dict):
        value = body.get("from")
        if _valid_reported_id(value):
            candidates.append(value)
    actor = payload.get("actor")
    if isinstance(actor, dict):
        value = actor.get("from_agent")
        if _valid_reported_id(value):
            candidates.append(value)
    for candidate in candidates:
        uid = roster_index_by_name.get(candidate.lower())
        if uid:
            return uid
    return None


def reported_sender(payload: dict) -> str:
    """Return the sender string claimed by the payload, never an identity grant."""

    for key in _SENDER_KEYS:
        value = payload.get(key)
        if _valid_reported_id(value):
            return value
    actor = payload.get("actor")
    if isinstance(actor, dict):
        value = actor.get("from_agent")
        if _valid_reported_id(value):
            return value
    body = payload.get("body")
    if isinstance(body, dict):
        value = body.get("from")
        if _valid_reported_id(value):
            return value
    return "unknown"


def _valid_reported_id(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and 0 < len(value) <= MAX_REPORTED_ID_CHARS
        and all(0x20 <= ord(char) < 0x7F for char in value)
    )


def verified_last_heard(rec: dict) -> float | None:
    """Return ``last_heard`` only when its evidence is identity-bound."""

    if rec.get("last_heard_verification") not in _VERIFIED_HEARD:
        return None
    value = rec.get("last_heard")
    return value if isinstance(value, (int, float)) else None


def freshness(
    last_heard: float | None, now: float, fresh_s: int = 300, recent_s: int = 7200
) -> str:
    if last_heard is None:
        return "never"
    age = now - last_heard
    if age <= fresh_s:
        return "fresh"
    if age <= recent_s:
        return "recent"
    return "stale"


def contact(last_heard: float | None, last_addressed: float | None) -> str:
    if last_heard is not None:
        return "heard"
    if last_addressed is not None:
        return "addressed_only"
    return "never"


def _row(
    uid: str,
    info: dict,
    rec: dict,
    now: float,
    fresh_s: int,
    recent_s: int,
) -> dict:
    last_heard = verified_last_heard(rec)
    last_addressed = rec.get("last_addressed")
    last_reported_heard = rec.get("last_reported_heard")
    fresh = freshness(last_heard, now, fresh_s, recent_s)
    heard_source = rec.get("last_heard_source") or "unattributed"
    addressed_source = rec.get("last_addressed_source") or "unattributed"
    return {
        "uid": uid,
        "callsign": info.get("callsign"),
        "display_name": info.get("display_name"),
        "subject": info.get("subject"),
        "role": info.get("role"),
        "host": info.get("host"),
        "tailscale": info.get("tailscale"),
        "model": info.get("model"),
        "provider": info.get("provider"),
        "seat": info.get("seat") or "active",
        "bio": info.get("bio"),
        "last_heard": iso(last_heard),
        "last_addressed": iso(last_addressed),
        "contact": contact(last_heard, last_addressed),
        "freshness": fresh,
        "signals": {
            "last_heard": {
                "value": iso(last_heard),
                "source": heard_source,
                "observed_at": iso(last_heard),
                "ttl_s": recent_s,
                "expires_at": iso(last_heard + recent_s)
                if last_heard is not None
                else None,
                "verification": rec.get("last_heard_verification") or "unknown",
            },
            "last_addressed": {
                "value": iso(last_addressed),
                "source": addressed_source,
                "observed_at": iso(last_addressed),
                "ttl_s": recent_s,
                "expires_at": iso(last_addressed + recent_s)
                if last_addressed is not None
                else None,
                "verification": rec.get("last_addressed_verification") or "unknown",
            },
            "reported_sender": {
                "value": rec.get("last_reported_sender") or None,
                "matched_uid": uid if last_reported_heard is not None else None,
                "source": rec.get("last_reported_heard_source") or "unattributed",
                "observed_at": iso(last_reported_heard),
                "ttl_s": recent_s,
                "expires_at": iso(last_reported_heard + recent_s)
                if last_reported_heard is not None
                else None,
                "verification": "reported_unverified"
                if last_reported_heard is not None
                else "unknown",
            },
        },
        # DEPRECATED compat fields (v0.5 UI shapes), one release only:
        "status": "live" if fresh == "fresh" else "offline",
        "last_seen": iso(last_heard if last_heard is not None else last_addressed),
    }


def decorate(
    roster_agents: dict,
    presence: dict,
    now: float,
    fresh_s: int = 300,
    recent_s: int = 7200,
) -> list[dict]:
    rows = [
        _row(uid, info or {}, presence.get(uid) or {}, now, fresh_s, recent_s)
        for uid, info in roster_agents.items()
    ]
    rows.sort(
        key=lambda r: (
            0 if r["seat"] == "active" else 1,
            _FRESH_RANK.get(r["freshness"], 3),
            r["uid"],
        )
    )
    return rows


__all__ = [
    "MAX_REPORTED_ID_CHARS",
    "contact",
    "decorate",
    "freshness",
    "iso",
    "reported_sender",
    "resolve_sender",
    "uid_for_subject",
    "verified_last_heard",
]
