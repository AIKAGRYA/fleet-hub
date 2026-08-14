"""Fail-closed auth primitives. Pure stdlib — no fastapi imports."""
from __future__ import annotations

import hashlib
import hmac

SESSION_INFO = b"fleet-hub-session-v1"


def session_value(token: str) -> str:
    """HMAC-derived cookie value so the raw token never rides in a cookie."""
    return hmac.new(token.encode(), SESSION_INFO, hashlib.sha256).hexdigest()


def parse_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        value = parts[1].strip()
        return value or None
    return None


def check(token_cfg: str | None, bearer: str | None, cookie: str | None) -> bool:
    """No configured token means no access at all — the hub fails closed
    rather than running open on a host where FLEET_HUB_TOKEN was forgotten."""
    if not token_cfg:
        return False
    # Compare on bytes: hmac.compare_digest raises TypeError on non-ASCII str,
    # and bearer/cookie are attacker-controlled header values. Encoding makes
    # the comparison total — mismatched bytes just return False, never crash.
    if bearer is not None and hmac.compare_digest(bearer.encode("utf-8"), token_cfg.encode("utf-8")):
        return True
    if cookie is not None and hmac.compare_digest(
        cookie.encode("utf-8"), session_value(token_cfg).encode("utf-8")
    ):
        return True
    return False
