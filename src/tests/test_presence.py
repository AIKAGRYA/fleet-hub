"""Contract: hub/presence.py (CONTRACT.md "hub/ module interfaces" — presence).

Verifies: freshness window boundaries, contact() two-signal logic,
resolve_sender key/name matching, uid_for_subject, and decorate() row shape,
seat ordering, freshness sort, deprecated status/last_seen compat fields.
All functions take `now` explicitly — no wall-clock reads inside.
"""
from __future__ import annotations

import pytest

from hub import presence


class TestFreshness:
    def test_none_is_never(self, frozen_now):
        assert presence.freshness(None, frozen_now) == "never"

    @pytest.mark.parametrize(
        "age,expected",
        [
            (0, "fresh"),
            (299, "fresh"),
            (300, "fresh"),      # boundary: age <= fresh_s
            (301, "recent"),
            (7200, "recent"),    # boundary: age <= recent_s
            (7201, "stale"),
        ],
    )
    def test_boundaries(self, frozen_now, age, expected):
        assert presence.freshness(frozen_now - age, frozen_now) == expected

    def test_custom_windows(self, frozen_now):
        assert presence.freshness(frozen_now - 11, frozen_now, fresh_s=10, recent_s=20) == "recent"
        assert presence.freshness(frozen_now - 21, frozen_now, fresh_s=10, recent_s=20) == "stale"


class TestContact:
    def test_heard(self, frozen_now):
        assert presence.contact(frozen_now, None) == "heard"

    def test_heard_wins_over_addressed(self, frozen_now):
        assert presence.contact(frozen_now, frozen_now) == "heard"

    def test_addressed_only(self, frozen_now):
        assert presence.contact(None, frozen_now) == "addressed_only"

    def test_never(self):
        assert presence.contact(None, None) == "never"


class TestResolveSender:
    def test_from_uid(self, roster_index_by_name):
        assert presence.resolve_sender({"from": "agni-hermes"}, roster_index_by_name) == "agni-hermes"

    def test_sender_callsign_case_insensitive(self, roster_index_by_name):
        assert presence.resolve_sender({"sender": "HERMES"}, roster_index_by_name) == "agni-hermes"

    def test_from_agent_display_name(self, roster_index_by_name):
        assert presence.resolve_sender({"from_agent": "Meghadharma Hermes"}, roster_index_by_name) == "meghadharma-hermes"

    def test_nested_body_from(self, roster_index_by_name):
        assert presence.resolve_sender({"body": {"from": "hermes"}}, roster_index_by_name) == "agni-hermes"

    def test_unknown_is_none(self, roster_index_by_name):
        assert presence.resolve_sender({"from": "totally-unknown"}, roster_index_by_name) is None

    def test_empty_payload_is_none(self, roster_index_by_name):
        assert presence.resolve_sender({}, roster_index_by_name) is None

    def test_non_string_values_skipped(self, roster_index_by_name):
        assert presence.resolve_sender({"from": 42, "sender": None}, roster_index_by_name) is None


class TestUidForSubject:
    def test_known_subject(self, roster_index_by_subject):
        assert presence.uid_for_subject("dharma.a2a.hermes", roster_index_by_subject) == "agni-hermes"

    def test_reply_style_subject(self, roster_index_by_subject):
        assert (
            presence.uid_for_subject(
                "dharma.a2a.fleet.reply.meghadharma_hermes", roster_index_by_subject
            )
            == "meghadharma-hermes"
        )

    def test_unknown_subject(self, roster_index_by_subject):
        assert presence.uid_for_subject("dharma.fleet.chat", roster_index_by_subject) is None


ROW_KEYS = {
    "uid", "callsign", "display_name", "subject", "role", "host", "tailscale",
    "model", "provider", "seat", "bio", "last_heard", "last_addressed",
    "contact", "freshness", "status", "last_seen",
}


class TestDecorate:
    @pytest.fixture
    def rows(self, roster, frozen_now):
        pres = {
            # fresh, heard
            "meghadharma-hermes": {
                "last_heard": frozen_now - 10,
                "last_heard_verification": "identity_bound_transport",
                "last_addressed": None,
            },
            # stale, heard
            "agni-hermes": {
                "last_heard": frozen_now - 8000,
                "last_heard_verification": "owner_verified",
                "last_addressed": None,
            },
            # archived seat: addressed only
            "fable_composer": {"last_heard": None, "last_addressed": frozen_now - 50},
        }
        return presence.decorate(roster["agents"], pres, frozen_now)

    def test_row_count(self, rows):
        assert len(rows) == 3

    def test_row_shape(self, rows):
        for row in rows:
            assert ROW_KEYS <= set(row.keys()), f"missing keys: {ROW_KEYS - set(row.keys())}"

    def test_active_before_archived(self, rows):
        seats = [r["seat"] for r in rows]
        assert seats == ["active", "active", "archived"]

    def test_freshness_sort_within_active(self, rows):
        # fresh meghadharma sorts before stale agni-hermes
        assert rows[0]["uid"] == "meghadharma-hermes"
        assert rows[1]["uid"] == "agni-hermes"

    def test_deprecated_status_live_iff_fresh(self, rows):
        by_uid = {r["uid"]: r for r in rows}
        assert by_uid["meghadharma-hermes"]["freshness"] == "fresh"
        assert by_uid["meghadharma-hermes"]["status"] == "live"
        assert by_uid["agni-hermes"]["freshness"] == "stale"
        assert by_uid["agni-hermes"]["status"] == "offline"

    def test_contact_fields(self, rows):
        by_uid = {r["uid"]: r for r in rows}
        assert by_uid["agni-hermes"]["contact"] == "heard"
        assert by_uid["fable_composer"]["contact"] == "addressed_only"

    def test_last_seen_fallback_to_last_addressed(self, rows):
        by_uid = {r["uid"]: r for r in rows}
        row = by_uid["fable_composer"]
        assert row["last_heard"] is None
        assert row["last_seen"] is not None
        assert row["last_seen"] == row["last_addressed"]

    def test_never_seat(self, roster, frozen_now):
        rows = presence.decorate(roster["agents"], {}, frozen_now)
        by_uid = {r["uid"]: r for r in rows}
        row = by_uid["agni-hermes"]
        assert row["freshness"] == "never"
        assert row["contact"] == "never"
        assert row["last_seen"] is None
        assert row["status"] == "offline"

    def test_unverified_payload_claim_cannot_make_roster_fresh(
        self, roster, frozen_now
    ):
        rows = presence.decorate(
            roster["agents"],
            {
                "agni-hermes": {
                    "last_heard": frozen_now,
                    "last_heard_source": "nats.payload_sender_claim",
                    "last_heard_verification": "reported_unverified",
                    "last_reported_heard": frozen_now,
                    "last_reported_sender": "hermes",
                    "last_reported_heard_source": "nats.payload_sender_claim",
                }
            },
            frozen_now,
        )
        row = next(item for item in rows if item["uid"] == "agni-hermes")
        assert row["last_heard"] is None
        assert row["freshness"] == "never"
        assert row["contact"] == "never"
        assert row["status"] == "offline"
        assert row["signals"]["reported_sender"]["value"] == "hermes"
        assert (
            row["signals"]["reported_sender"]["verification"]
            == "reported_unverified"
        )
