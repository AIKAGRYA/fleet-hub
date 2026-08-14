"""Two-signal presence: heard (agent spoke) vs addressed (traffic sent to it).

Pure functions with an injected clock — every function takes `now` as unix
seconds; nothing here calls datetime.now().
"""
from __future__ import annotations

from datetime import datetime, timezone

_FRESH_RANK = {"fresh": 0, "recent": 1, "stale": 2, "never": 3}
_SENDER_KEYS = ("from", "sender", "agent", "from_agent")


def iso(ts: float | None) -> str | None:
    """Serialize a unix timestamp as ISO8601 UTC ('2026-08-14T01:02:03Z')."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def uid_for_subject(subject: str, roster_index: dict) -> str | None:
    return roster_index.get(subject)


def resolve_sender(payload: dict, roster_index_by_name: dict) -> str | None:
    """Map a payload's claimed sender to a roster uid, or None.

    roster_index_by_name: lowercase uid/callsign/display_name -> uid.
    """
    candidates: list[str] = []
    for key in _SENDER_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)
    body = payload.get("body")
    if isinstance(body, dict):
        value = body.get("from")
        if isinstance(value, str) and value:
            candidates.append(value)
    for candidate in candidates:
        uid = roster_index_by_name.get(candidate.lower())
        if uid:
            return uid
    return None


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
    last_heard = rec.get("last_heard")
    last_addressed = rec.get("last_addressed")
    fresh = freshness(last_heard, now, fresh_s, recent_s)
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
