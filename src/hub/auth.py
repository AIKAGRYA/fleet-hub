"""Authentication and server-side session primitives.

The configured operator token is accepted only at the login boundary or as an
explicit Bearer credential. Browser sessions are random, expire in memory,
and can be revoked; no value derived solely from the long-lived operator token
is accepted as a cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

COOKIE_NAME = "fleet_session"
CSRF_HEADER = "x-csrf-token"


@dataclass(frozen=True)
class Session:
    """One opaque browser session and its non-secret, bound CSRF token."""

    session_id: str
    csrf_token: str
    issued_at: float
    expires_at: float


@dataclass(frozen=True)
class AuthContext:
    """Authenticated request identity without exposing a credential value."""

    mode: Literal["bearer", "session"]
    principal_key: str
    session: Session | None = None


class SessionStore:
    """Bounded, expiring, process-local browser-session registry.

    Process-local storage is deliberate for this build: restart revokes every
    session instead of accidentally accepting an unverifiable cookie. A
    durable multi-process store would need a separately reviewed contract.
    """

    def __init__(
        self,
        *,
        ttl_s: int = 12 * 60 * 60,
        max_sessions: int = 256,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_s < 1:
            raise ValueError("ttl_s must be positive")
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        self.ttl_s = int(ttl_s)
        self.max_sessions = int(max_sessions)
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _purge_locked(self, now: float) -> None:
        for session_id, session in list(self._sessions.items()):
            if session.expires_at <= now:
                self._sessions.pop(session_id, None)

    def issue(self, *, now: float | None = None) -> Session:
        instant = self._clock() if now is None else now
        with self._lock:
            self._purge_locked(instant)
            while len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions.values(), key=lambda item: item.issued_at)
                self._sessions.pop(oldest.session_id, None)
            while True:
                session_id = secrets.token_urlsafe(32)
                if session_id not in self._sessions:
                    break
            session = Session(
                session_id=session_id,
                csrf_token=secrets.token_urlsafe(24),
                issued_at=instant,
                expires_at=instant + self.ttl_s,
            )
            self._sessions[session_id] = session
            return session

    def get(self, session_id: str | None, *, now: float | None = None) -> Session | None:
        if not session_id:
            return None
        instant = self._clock() if now is None else now
        with self._lock:
            self._purge_locked(instant)
            return self._sessions.get(session_id)

    def revoke(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            return count

    def check_csrf(self, session_id: str | None, candidate: str | None) -> bool:
        if not candidate:
            return False
        session = self.get(session_id)
        return bool(
            session
            and hmac.compare_digest(
                candidate.encode("utf-8"), session.csrf_token.encode("utf-8")
            )
        )

    def __len__(self) -> int:
        with self._lock:
            self._purge_locked(self._clock())
            return len(self._sessions)


def parse_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        value = parts[1].strip()
        return value or None
    return None


def check_token(token_cfg: str | None, candidate: str | None) -> bool:
    """Constant-time token check which is total for attacker-controlled text."""

    if not token_cfg or candidate is None:
        return False
    return hmac.compare_digest(
        candidate.encode("utf-8"), token_cfg.encode("utf-8")
    )


def authenticate(
    token_cfg: str | None,
    *,
    bearer: str | None,
    cookie: str | None,
    sessions: SessionStore,
) -> AuthContext | None:
    """Authenticate one request, giving an explicit Bearer header priority.

    An invalid explicit Bearer credential never falls back to an ambient
    cookie. This prevents a confused request from silently changing auth
    modes while preserving cookie-only browser requests.
    """

    if not token_cfg:
        return None
    if bearer is not None:
        if not check_token(token_cfg, bearer):
            return None
        digest = hashlib.sha256(bearer.encode("utf-8")).hexdigest()[:24]
        return AuthContext(mode="bearer", principal_key=f"bearer:{digest}")
    session = sessions.get(cookie)
    if session is None:
        return None
    digest = hashlib.sha256(session.session_id.encode("utf-8")).hexdigest()[:24]
    return AuthContext(
        mode="session", principal_key=f"session:{digest}", session=session
    )


__all__ = [
    "AuthContext",
    "COOKIE_NAME",
    "CSRF_HEADER",
    "Session",
    "SessionStore",
    "authenticate",
    "check_token",
    "parse_bearer",
]
