"""Contract: hub/auth.py (CONTRACT.md "hub/ module interfaces" — auth).

Verifies: session_value HMAC derivation, parse_bearer parsing, and the
check() matrix — fail-closed when no token is configured, bearer and
session-cookie acceptance, raw-token-as-cookie rejection. No query-param
auth path exists anywhere in v0.6 (route-level check in test_server_routes).
"""
from __future__ import annotations

import hashlib
import hmac

import pytest

auth = pytest.importorskip("hub.auth")

TOKEN = "testtoken"


class TestSessionValue:
    def test_deterministic(self):
        assert auth.session_value(TOKEN) == auth.session_value(TOKEN)

    def test_matches_hmac_sha256_of_session_info(self):
        expected = hmac.new(
            TOKEN.encode(), auth.SESSION_INFO, hashlib.sha256
        ).hexdigest()
        assert auth.session_value(TOKEN) == expected

    def test_session_info_constant(self):
        assert auth.SESSION_INFO == b"fleet-hub-session-v1"

    def test_differs_per_token(self):
        assert auth.session_value("alpha") != auth.session_value("beta")

    def test_is_hex_digest(self):
        v = auth.session_value(TOKEN)
        assert len(v) == 64
        int(v, 16)  # raises if not hex


class TestParseBearer:
    def test_none(self):
        assert auth.parse_bearer(None) is None

    def test_empty(self):
        assert auth.parse_bearer("") is None

    def test_bearer_token(self):
        assert auth.parse_bearer("Bearer abc123") == "abc123"

    def test_wrong_scheme(self):
        assert auth.parse_bearer("Token abc123") is None

    def test_bare_word(self):
        assert auth.parse_bearer("Bearer") is None


class TestCheck:
    """The full check() matrix. FAIL CLOSED is the load-bearing behavior."""

    def test_unconfigured_none_token(self):
        assert auth.check(None, None, None) is False

    def test_unconfigured_empty_token(self):
        assert auth.check("", None, None) is False

    def test_unconfigured_rejects_even_correct_looking_bearer(self):
        assert auth.check(None, TOKEN, None) is False
        assert auth.check("", TOKEN, None) is False

    def test_unconfigured_rejects_cookie(self):
        assert auth.check(None, None, auth.session_value(TOKEN)) is False

    def test_bearer_ok(self):
        assert auth.check(TOKEN, TOKEN, None) is True

    def test_bearer_wrong(self):
        assert auth.check(TOKEN, "wrongtoken", None) is False

    def test_cookie_ok(self):
        assert auth.check(TOKEN, None, auth.session_value(TOKEN)) is True

    def test_raw_token_as_cookie_rejected(self):
        # The cookie must be the derived session value, never the raw token.
        assert auth.check(TOKEN, None, TOKEN) is False

    def test_both_none(self):
        assert auth.check(TOKEN, None, None) is False

    def test_wrong_bearer_with_good_cookie_still_ok(self):
        # Cookie path is independent of a garbage Authorization header.
        assert auth.check(TOKEN, "garbage", auth.session_value(TOKEN)) is True

    def test_non_ascii_bearer_rejected_not_crashed(self):
        # Regression: hmac.compare_digest raises TypeError on non-ASCII str.
        # Attacker-controlled header bytes must return False, never crash.
        assert auth.check(TOKEN, "caf\xe9", None) is False

    def test_non_ascii_cookie_rejected_not_crashed(self):
        assert auth.check(TOKEN, None, "caf\xe9") is False

    def test_non_ascii_with_unconfigured_token(self):
        assert auth.check("", "caf\xe9", "caf\xe9") is False
