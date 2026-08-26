"""Contract tests for random, revocable, expiring browser sessions."""
from __future__ import annotations

from hub import auth

TOKEN = "testtoken"


class Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestParseBearer:
    def test_none_and_wrong_scheme(self):
        assert auth.parse_bearer(None) is None
        assert auth.parse_bearer("") is None
        assert auth.parse_bearer("Token abc") is None
        assert auth.parse_bearer("Bearer") is None

    def test_valid_case_insensitive_scheme(self):
        assert auth.parse_bearer("bEaReR abc123") == "abc123"


class TestTokenCheck:
    def test_fail_closed(self):
        assert auth.check_token(None, TOKEN) is False
        assert auth.check_token("", TOKEN) is False
        assert auth.check_token(TOKEN, None) is False

    def test_match_and_mismatch(self):
        assert auth.check_token(TOKEN, TOKEN) is True
        assert auth.check_token(TOKEN, "wrong") is False

    def test_non_ascii_is_total(self):
        assert auth.check_token(TOKEN, "caf\xe9") is False


class TestSessionStore:
    def test_issue_is_random_and_server_side(self):
        store = auth.SessionStore()
        first = store.issue()
        second = store.issue()
        assert first.session_id != second.session_id
        assert first.csrf_token != second.csrf_token
        assert TOKEN not in first.session_id
        assert store.get(first.session_id) == first

    def test_expiry_removes_session(self):
        clock = Clock()
        store = auth.SessionStore(ttl_s=10, clock=clock)
        session = store.issue()
        clock.now = 109.9
        assert store.get(session.session_id) is not None
        clock.now = 110.0
        assert store.get(session.session_id) is None
        assert len(store) == 0

    def test_revoke_is_immediate(self):
        store = auth.SessionStore()
        session = store.issue()
        assert store.revoke(session.session_id) is True
        assert store.revoke(session.session_id) is False
        assert store.get(session.session_id) is None

    def test_bound_evicts_oldest(self):
        clock = Clock()
        store = auth.SessionStore(max_sessions=2, clock=clock)
        first = store.issue()
        clock.now += 1
        second = store.issue()
        clock.now += 1
        third = store.issue()
        assert store.get(first.session_id) is None
        assert store.get(second.session_id) is not None
        assert store.get(third.session_id) is not None

    def test_csrf_is_session_bound(self):
        store = auth.SessionStore()
        first = store.issue()
        second = store.issue()
        assert store.check_csrf(first.session_id, first.csrf_token) is True
        assert store.check_csrf(first.session_id, second.csrf_token) is False
        assert store.check_csrf(None, first.csrf_token) is False


class TestAuthenticate:
    def test_unconfigured_fails_closed(self):
        store = auth.SessionStore()
        session = store.issue()
        assert (
            auth.authenticate(
                None, bearer=TOKEN, cookie=session.session_id, sessions=store
            )
            is None
        )

    def test_explicit_bearer(self):
        context = auth.authenticate(
            TOKEN, bearer=TOKEN, cookie=None, sessions=auth.SessionStore()
        )
        assert context is not None
        assert context.mode == "bearer"
        assert context.session is None
        assert TOKEN not in context.principal_key

    def test_random_session_cookie(self):
        store = auth.SessionStore()
        session = store.issue()
        context = auth.authenticate(
            TOKEN, bearer=None, cookie=session.session_id, sessions=store
        )
        assert context is not None
        assert context.mode == "session"
        assert context.session == session

    def test_raw_operator_token_is_not_a_cookie(self):
        assert (
            auth.authenticate(
                TOKEN, bearer=None, cookie=TOKEN, sessions=auth.SessionStore()
            )
            is None
        )

    def test_invalid_explicit_bearer_never_falls_back_to_cookie(self):
        store = auth.SessionStore()
        session = store.issue()
        assert (
            auth.authenticate(
                TOKEN,
                bearer="wrong",
                cookie=session.session_id,
                sessions=store,
            )
            is None
        )
