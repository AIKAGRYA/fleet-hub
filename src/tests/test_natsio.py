"""Contract: hub/natsio.py (CONTRACT.md "hub/ module interfaces" — natsio).

Verifies: handle_msg sync core (chat append + synthesized srv- msg_id, echo
suppression via state.sent, bounded raw preview, canonical/legacy inbound DM
observation, exact ACK/reply correlation, json fallback, live bus emits) and
async send() (canonical agent-inbox envelope, fail-closed roster binding,
JetStream PUBLISH_ACCEPTED, core-NATS NO_ACK fallback, and broker dedupe).

Uses fakes from conftest — no network, no real NATS.
"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from hub import natsio
from hub import state as state_mod

CHAT_SUBJECT = "dharma.fleet.chat"
PRINCIPAL = "session:test-principal"


@pytest.fixture
def cfg():
    """Config carrier for natsio fns (contract does not pin its type; the
    server config section defines these knobs)."""
    return SimpleNamespace(
        chat_subject=CHAT_SUBJECT,
        stream="DHARMA_A2A",
        nats_url="nats://127.0.0.1:4222",
        nats_user=None,
        nats_pass=None,
        monitor_url="http://127.0.0.1:8222",
        replay_hours=48,
        replay_streams=["DHARMA_A2A"],
        live_window_s=300,
        recent_window_s=7200,
        fleet_sender_uid="fleet-hub-test",
    )


@pytest.fixture
def hub_state():
    return state_mod.HubState()


class TestCanonicalRoutingIndexes:
    def test_only_exact_roster_bindings_are_ratified(self, roster):
        indexes = natsio.build_indexes(roster)

        assert indexes["by_a2a_uid"] == {
            "hermes-m5": "agni-hermes",
        }
        assert indexes["by_inbox_subject"] == {
            "dharma.agent.hermes-m5.inbox": "agni-hermes",
        }
        assert "fable_composer" not in indexes["by_a2a_uid"]

    def test_identity_collision_removes_outbound_binding(self, roster):
        collided = deepcopy(roster)
        collided["agents"]["second-agni"] = {
            "callsign": "second-agni",
            "display_name": "Second AGNI claim",
            "a2a_uid": "hermes-m5",
            "inbox_subject": "dharma.agent.hermes-m5.inbox",
            "inbox_authority": "A2ACard",
            "inbox_card_sha256": "b" * 64,
            "inbox_evidence": "test-fixture://owner-card/collision",
            "subject": "dharma.a2a.second-agni",
            "seat": "active",
        }

        indexes = natsio.build_indexes(collided)

        assert "hermes-m5" in indexes["ambiguous_a2a_uids"]
        assert "dharma.agent.hermes-m5.inbox" in indexes[
            "ambiguous_inbox_subjects"
        ]
        assert "hermes-m5" not in indexes["by_a2a_uid"]
        assert "dharma.agent.hermes-m5.inbox" not in indexes["by_inbox_subject"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("inbox_authority", "roster-self-claim"),
            ("inbox_card_sha256", "not-a-sha256"),
            ("inbox_evidence", ""),
            ("inbox_evidence", "bad\ncontrol"),
        ],
    )
    def test_card_authority_evidence_is_required(self, roster, field, value):
        unproven = deepcopy(roster)
        unproven["agents"]["agni-hermes"][field] = value

        indexes = natsio.build_indexes(unproven)

        assert "hermes-m5" not in indexes["by_a2a_uid"]
        assert "dharma.agent.hermes-m5.inbox" not in indexes[
            "by_inbox_subject"
        ]


class TestHandleMsg:
    def test_foreign_chat_appended_once_with_synthesized_id(
        self, hub_state, cfg, roster_indexes, frozen_now
    ):
        data = json.dumps({"from": "hermes", "text": "hello fleet"}).encode()
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=False
        )
        assert len(hub_state.chat) == 1
        msg = hub_state.chat[0]
        assert msg["text"] == "hello fleet"
        assert msg["subject"] == CHAT_SUBJECT
        assert msg["msg_id"].startswith("srv-")

    def test_payload_sender_updates_only_reported_presence(
        self, hub_state, cfg, roster_indexes, frozen_now
    ):
        data = json.dumps({"from": "hermes", "text": "hi"}).encode()
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=False
        )
        signal = hub_state.presence["agni-hermes"]
        assert signal["last_heard"] is None
        assert signal["last_reported_heard"] == frozen_now
        assert signal["last_reported_sender"] == "hermes"

    def test_echo_suppression_own_msg_id(self, hub_state, cfg, roster_indexes, frozen_now):
        data = json.dumps({"msg_id": "op-1", "from": "hermes", "text": "echo"}).encode()
        hub_state.sent.add(natsio.outbound_echo_key(CHAT_SUBJECT, data))
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=False
        )
        # chat NOT re-appended; the payload claim remains only reported.
        assert len(hub_state.chat) == 0
        assert hub_state.presence["agni-hermes"]["last_heard"] is None
        assert hub_state.presence["agni-hermes"]["last_reported_heard"] == frozen_now

    def test_identity_bound_sender_can_update_verified_last_heard(
        self, hub_state, cfg, roster_indexes, frozen_now
    ):
        data = json.dumps({"from": "spoofed-other", "text": "bound"}).encode()
        natsio.handle_msg(
            hub_state,
            cfg,
            roster_indexes,
            CHAT_SUBJECT,
            data,
            frozen_now,
            live=False,
            verified_sender_uid="agni-hermes",
        )
        signal = hub_state.presence["agni-hermes"]
        assert signal["last_heard"] == frozen_now
        assert signal["last_heard_verification"] == "identity_bound_transport"

    def test_raw_preview_truncated_to_160(self, hub_state, cfg, roster_indexes, frozen_now):
        data = json.dumps({"from": "hermes", "text": "x" * 500}).encode()
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=False
        )
        assert len(hub_state.raw) == 1
        entry = hub_state.raw[0]
        assert entry["subject"] == CHAT_SUBJECT
        assert len(entry["preview"]) <= 160
        assert "ts" in entry

    def test_legacy_dm_subject_remains_inbound_compatibility_only(
        self, hub_state, cfg, roster_indexes, frozen_now
    ):
        data = json.dumps({"from": "operator-x", "text": "private ping"}).encode()
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, "dharma.a2a.hermes", data, frozen_now, live=False
        )
        assert "agni-hermes" in hub_state.dms
        assert len(hub_state.dms["agni-hermes"]) == 1
        assert hub_state.dms["agni-hermes"][0]["text"] == "private ping"
        # a DM is not group chat
        assert len(hub_state.chat) == 0

    def test_canonical_inbox_message_routes_to_verified_roster_uid(
        self, hub_state, cfg, roster_indexes, frozen_now
    ):
        data = json.dumps(
            {"from": "hermes-m5", "text": "canonical reply"}
        ).encode()
        natsio.handle_msg(
            hub_state,
            cfg,
            roster_indexes,
            "dharma.agent.hermes-m5.inbox",
            data,
            frozen_now,
            live=False,
        )

        message = hub_state.dms["agni-hermes"][0]
        assert message["text"] == "canonical reply"
        assert message["sender_claim"]["status"] == "reported_unverified"
        assert hub_state.presence["agni-hermes"]["last_heard"] is None

    def test_dm_reply_style_subject(self, hub_state, cfg, roster_indexes, frozen_now):
        data = json.dumps({"text": "reply lane"}).encode()
        natsio.handle_msg(
            hub_state,
            cfg,
            roster_indexes,
            "dharma.a2a.fleet.reply.meghadharma_hermes",
            data,
            frozen_now,
            live=False,
        )
        assert len(hub_state.dms.get("meghadharma-hermes", ())) == 1

    def test_non_json_payload_does_not_crash(self, hub_state, cfg, roster_indexes, frozen_now):
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, b"\x00not-json", frozen_now, live=False
        )
        assert len(hub_state.raw) == 1  # raw feed still records it

    def test_live_true_publishes_chat_event_on_bus(
        self, hub_state, cfg, roster_indexes, frozen_now
    ):
        q = hub_state.bus.attach()
        data = json.dumps({"from": "hermes", "text": "live msg"}).encode()
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=True
        )
        kinds = []
        while not q.empty():
            kinds.append(q.get_nowait()["event"])
        assert "chat" in kinds

    def test_live_false_emits_nothing_on_bus(self, hub_state, cfg, roster_indexes, frozen_now):
        q = hub_state.bus.attach()
        data = json.dumps({"from": "hermes", "text": "replay msg"}).encode()
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=False
        )
        assert q.empty()

    def test_body_dict_text_extraction(self, hub_state, cfg, roster_indexes, frozen_now):
        data = json.dumps({"from": "hermes", "body": {"text": "nested text"}}).encode()
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=False
        )
        assert len(hub_state.chat) == 1
        assert hub_state.chat[0]["text"] == "nested text"

    def test_oversized_frame_is_quarantined_before_json_parse(
        self, hub_state, cfg, roster_indexes, frozen_now, monkeypatch
    ):
        def must_not_parse(*args, **kwargs):
            raise AssertionError("oversized frame reached json.loads")

        monkeypatch.setattr(natsio.json, "loads", must_not_parse)
        data = b"x" * (natsio.MAX_INBOUND_BYTES + 1)
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=False
        )
        assert not hub_state.chat
        assert hub_state.dms == {}
        assert hub_state.presence == {}
        assert hub_state.raw[-1]["quarantined"] is True
        assert hub_state.raw[-1]["quarantine_reason"] == "payload_too_large"
        assert hub_state.raw[-1]["size_bytes"] == len(data)
        assert len(hub_state.raw[-1]["preview"]) <= natsio.MAX_RAW_PREVIEW_CHARS

    def test_oversized_text_is_quarantined_before_chat_and_sse(
        self, hub_state, cfg, roster_indexes, frozen_now
    ):
        queue = hub_state.bus.attach()
        data = json.dumps(
            {"from": "hermes", "text": "x" * (natsio.MAX_INBOUND_TEXT_CHARS + 1)}
        ).encode()
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=True
        )
        assert not hub_state.chat
        assert not hub_state.presence
        events = [queue.get_nowait() for _ in range(queue.qsize())]
        assert [event["event"] for event in events] == ["raw"]
        assert events[0]["data"]["quarantine_reason"] == "text_too_large"
        assert len(json.dumps(events[0])) < 1_000

    def test_hostile_ids_and_sender_are_dropped_not_relayed(
        self, hub_state, cfg, roster_indexes, frozen_now
    ):
        hostile = "z" * (natsio.MAX_WIRE_ID_CHARS + 1)
        data = json.dumps(
            {
                "message_id": hostile,
                "correlation_id": hostile,
                "trace_id": hostile,
                "from": "s" * 201,
                "text": "bounded",
            }
        ).encode()
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=False
        )
        message = hub_state.chat[0]
        assert message["message_id"].startswith("srv-")
        assert len(message["message_id"]) <= natsio.MAX_WIRE_ID_CHARS
        assert message["correlation_id"] is None
        assert message["trace_id"] is None
        assert message["from"] == "unknown"
        assert hostile not in json.dumps(message)
        assert hub_state.presence == {}


class TestSend:
    @pytest.mark.asyncio
    async def test_jetstream_publish_accepted(self, hub_state, cfg, roster, fake_js, fake_nc):
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        res = await natsio.send(
            hub_state, cfg, roster, "hello fleet", None, "m-1",
            principal_scope=PRINCIPAL,
        )
        assert res["ok"] is True
        assert res["ack_tier"] == "PUBLISH_ACCEPTED"
        assert res["seq"] == 7
        assert len(fake_js.published) == 1
        subject, payload = fake_js.published[0]
        assert subject == CHAT_SUBJECT
        body = json.loads(payload)
        assert body["msg_id"] == "m-1"
        assert body["message_id"] == "m-1"
        assert body["schema"] == "dharma.nats.envelope.v1"
        assert body["payload"]["schema"] == "fleet.chat.message.v1"
        assert body["route_plan"]["mode"] == "group_transcript"
        assert body["route_plan"]["fanout"] is False
        assert body["via"] == "fleet-hub-v1-candidate"
        assert body["from"] == "operator"
        assert body["text"] == "hello fleet"
        broker_id = fake_js.published_headers[0]["Nats-Msg-Id"]
        assert broker_id == natsio.broker_dedupe_id(
            namespace=natsio.DEFAULT_DEDUPE_NAMESPACE,
            principal_scope=PRINCIPAL,
            idempotency_key="m-1",
        )
        assert PRINCIPAL not in broker_id
        assert "m-1" not in broker_id

    @pytest.mark.asyncio
    async def test_send_records_sent_id_and_appends_locally(
        self, hub_state, cfg, roster, fake_js, fake_nc
    ):
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        await natsio.send(
            hub_state, cfg, roster, "hi", None, "m-2",
            principal_scope=PRINCIPAL,
        )
        subject, payload = fake_js.published[0]
        assert natsio.outbound_echo_key(subject, payload) in hub_state.sent
        # local append so the sender sees it immediately
        assert len(hub_state.chat) == 1
        assert hub_state.chat[0]["msg_id"] == "m-2"

    @pytest.mark.asyncio
    async def test_send_dm_by_callsign(self, hub_state, cfg, roster, fake_js, fake_nc):
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        res = await natsio.send(
            hub_state, cfg, roster, "psst", "hermes", "m-3",
            principal_scope=PRINCIPAL,
        )
        assert res["ok"] is True
        subject, payload = fake_js.published[0]
        body = json.loads(payload)
        assert subject == "dharma.agent.hermes-m5.inbox"
        assert subject != roster["agents"]["agni-hermes"]["subject"]
        assert body["schema_version"] == "dharma.a2a.send.v1"
        assert body["packet_id"] == "m-3"
        assert body["from"] == "fleet-hub-test"
        assert body["to"] == "hermes-m5"
        assert body["route"] == "agent-inbox"
        assert body["target_uid"] == "hermes-m5"
        assert body["subject"] == subject
        assert body["ack_subject"] == f"{subject}.ack.m-3"
        assert body["reply_subject"] == f"{subject}.reply.m-3"
        assert body["content"] == body["text"] == "psst"
        assert fake_js.published_headers[0]["Dharma-Nats-Schema"] == (
            "dharma.a2a.send.v1"
        )
        assert len(hub_state.dms.get("agni-hermes", ())) == 1
        assert res["route_plan"]["mode"] == "direct_message"
        assert res["route_plan"]["route"] == "agent-inbox"
        assert res["route_plan"]["recipient_uid"] == "agni-hermes"
        assert res["target_uid"] == "hermes-m5"
        assert res["inbox_authority"] == "A2ACard"
        assert res["inbox_card_sha256"] == "a" * 64
        assert res["inbox_evidence"] == "test-fixture://owner-card/hermes-m5"
        assert res["route_plan"]["inbox_authority"] == "A2ACard"
        assert res["packet_id"] == "m-3"
        assert res["handler_acknowledged"] is False
        assert res["proves_executor_liveness"] is False

    @pytest.mark.asyncio
    async def test_legacy_subject_is_never_an_outbound_fallback(
        self, hub_state, cfg, roster, fake_js, fake_nc
    ):
        unratified = deepcopy(roster)
        agent = unratified["agents"]["agni-hermes"]
        agent.pop("a2a_uid")
        agent.pop("inbox_subject")
        hub_state.js = fake_js
        hub_state.nc = fake_nc

        result = await natsio.send(
            hub_state,
            cfg,
            unratified,
            "do not guess",
            "hermes",
            "m-unratified",
            principal_scope=PRINCIPAL,
        )

        assert result == {
            "ok": False,
            "accepted": False,
            "error": "recipient_inbox_unratified",
        }
        assert fake_js.published == []
        assert fake_nc.published == []

    @pytest.mark.asyncio
    async def test_mismatched_canonical_binding_fails_closed(
        self, hub_state, cfg, roster, fake_js, fake_nc
    ):
        mismatched = deepcopy(roster)
        mismatched["agents"]["agni-hermes"]["inbox_subject"] = (
            "dharma.agent.someone-else.inbox"
        )
        hub_state.js = fake_js
        hub_state.nc = fake_nc

        result = await natsio.send(
            hub_state,
            cfg,
            mismatched,
            "do not guess",
            "hermes",
            "m-mismatch",
            principal_scope=PRINCIPAL,
        )

        assert result["error"] == "recipient_inbox_unratified"
        assert fake_js.published == []

    @pytest.mark.asyncio
    async def test_dm_requires_subject_safe_packet_id(
        self, hub_state, cfg, roster, fake_js, fake_nc
    ):
        hub_state.js = fake_js
        hub_state.nc = fake_nc

        result = await natsio.send(
            hub_state,
            cfg,
            roster,
            "bounded",
            "hermes",
            "unsafe.packet",
            principal_scope=PRINCIPAL,
        )

        assert result["error"] == "packet_id_invalid"
        assert fake_js.published == []

    @pytest.mark.asyncio
    async def test_invalid_local_sender_identity_fails_closed(
        self, hub_state, cfg, roster, fake_js, fake_nc
    ):
        cfg.fleet_sender_uid = "bad.sender"
        hub_state.js = fake_js
        hub_state.nc = fake_nc

        result = await natsio.send(
            hub_state,
            cfg,
            roster,
            "bounded",
            "hermes",
            "m-sender",
            principal_scope=PRINCIPAL,
        )

        assert result["error"] == "sender_identity_invalid"
        assert fake_js.published == []

    @pytest.mark.asyncio
    async def test_correlated_handler_ack_is_contact_not_liveness_or_effect(
        self, hub_state, cfg, roster, roster_indexes, frozen_now, fake_js, fake_nc
    ):
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        result = await natsio.send(
            hub_state,
            cfg,
            roster,
            "contact me",
            "hermes",
            "m-handler-ack",
            principal_scope=PRINCIPAL,
        )

        ack_data = json.dumps(
            {
                "packet_id": "m-handler-ack",
                "status": "consumed",
                "from": "untrusted-payload-claim",
            }
        ).encode()
        natsio.handle_msg(
            hub_state,
            cfg,
            roster_indexes,
            result["ack_subject"],
            ack_data,
            frozen_now,
            live=True,
        )
        # Duplicate delivery is receipt-idempotent.
        natsio.handle_msg(
            hub_state,
            cfg,
            roster_indexes,
            result["ack_subject"],
            ack_data,
            frozen_now + 1,
            live=True,
        )

        outgoing = hub_state.dms["agni-hermes"][0]
        assert outgoing["tier"] == "HANDLER_ACKED"
        assert outgoing["contact_tier"] == "HANDLER_ACKED"
        assert outgoing["handler_acknowledged"] is True
        assert outgoing["proves_executor_liveness"] is False
        assert outgoing["semantic_effect"] == "unobserved"
        assert len(outgoing["contact_receipts"]) == 1
        receipt = outgoing["contact_receipts"][0]
        assert receipt["contact_evidence_tier"] == "HANDLER_ACKED"
        assert receipt["proves_executor_liveness"] is False
        assert receipt["proves_semantic_effect"] is False
        assert hub_state.presence == {}

    @pytest.mark.asyncio
    async def test_correlated_semantic_reply_stays_in_originating_dm(
        self, hub_state, cfg, roster, roster_indexes, frozen_now, fake_js, fake_nc
    ):
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        result = await natsio.send(
            hub_state,
            cfg,
            roster,
            "origin",
            "hermes",
            "m-reply",
            principal_scope=PRINCIPAL,
            correlation_id="corr-origin",
            causation_id="cause-origin",
            trace_id="trace-origin",
        )
        hostile_wire_claims = json.dumps(
            {
                "message_id": "reply-safe",
                "text": "semantic answer",
                "from": "meghadharma-hermes",
                "to": "meghadharma-hermes",
                "correlation_id": "corr-hostile",
                "trace_id": "trace-hostile",
            }
        ).encode()

        natsio.handle_msg(
            hub_state,
            cfg,
            roster_indexes,
            result["reply_subject"],
            hostile_wire_claims,
            frozen_now,
            live=True,
        )

        assert len(hub_state.dms["agni-hermes"]) == 2
        assert not hub_state.dms.get("meghadharma-hermes")
        reply = hub_state.dms["agni-hermes"][-1]
        assert reply["text"] == "semantic answer"
        assert reply["from"] == "hermes-m5"
        assert reply["sender_claim"]["status"] == "reply_subject_correlated"
        assert reply["correlation_id"] == "corr-origin"
        assert reply["causation_id"] == "cause-origin"
        assert reply["trace_id"] == "trace-origin"
        assert reply["reply_to_message_id"] == "m-reply"
        assert reply["semantic_reply_observed"] is True
        assert reply["proves_executor_liveness"] is False
        assert reply["proves_original_effect"] is False
        assert result["causation_id"] == "cause-origin"
        assert hub_state.dms["agni-hermes"][0]["causation_id"] == "cause-origin"
        assert hub_state.raw[-1]["correlation_id"] == "corr-origin"
        assert hub_state.raw[-1]["causation_id"] == "cause-origin"
        assert hub_state.raw[-1]["trace_id"] == "trace-origin"
        reported = hub_state.presence["meghadharma-hermes"]
        assert reported["last_heard"] is None
        assert reported["last_reported_sender"] == "meghadharma-hermes"

    @pytest.mark.asyncio
    async def test_owner_typed_domain_receipt_without_text_is_projected(
        self, hub_state, cfg, roster, roster_indexes, frozen_now, fake_js, fake_nc
    ):
        """Mirror scripts/runtime/a2a_domain_reply_worker.py's exact wire shape."""
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        result = await natsio.send(
            hub_state,
            cfg,
            roster,
            "build it",
            "hermes",
            "m-domain-receipt",
            principal_scope=PRINCIPAL,
            correlation_id="corr-domain",
            causation_id="cause-domain",
            trace_id="trace-domain",
        )
        wire = {
            "schema_version": "dharma.a2a.domain_receipt.v1",
            "timestamp": "2026-08-28T00:00:00+00:00",
            "from_agent": "hermes-m5",
            "to_agent": "fleet-hub-test",
            "packet_id": "m-domain-receipt",
            "reply_subject": result["reply_subject"],
            "domain_receipt": True,
            "semantic_reply_claim": True,
            "target_owned_artifact_claim": True,
            "peer_model_processed_claim": True,
            "author_kind": "target_outbox_artifact",
            "verdict": "accepted",
            "summary": "Owner artifact reports the requested build is ready for review.",
            "evidence_refs": ["memory://hermes/domain-reply"],
            "source_artifact_schema": "dharma.a2a.domain_reply_artifact.v1",
            "source_artifact_path": "/owner/private/artifact.json",
            "source_artifact_sha256": "b" * 64,
            "causation_send_receipt_path": "/owner/private/send.json",
            "causation_send_receipt_sha256": "c" * 64,
            "operator_contact_note": "typed domain receipt published",
        }
        assert "text" not in wire

        natsio.handle_msg(
            hub_state,
            cfg,
            roster_indexes,
            result["reply_subject"],
            json.dumps(wire).encode(),
            frozen_now,
            live=True,
        )

        outgoing, reply = hub_state.dms["agni-hermes"]
        assert outgoing["tier"] == "DOMAIN_RECEIPTED"
        assert outgoing["contact_tier"] == "DOMAIN_RECEIPTED"
        assert outgoing["proves_executor_liveness"] is False
        assert outgoing["semantic_effect"] == "unobserved"
        assert reply["text"] == wire["summary"]
        assert reply["tier"] == "DOMAIN_RECEIPTED"
        assert reply["domain_receipt_observed"] is True
        assert reply["semantic_reply_claim"] is True
        assert reply["correlation_id"] == "corr-domain"
        assert reply["causation_id"] == "cause-domain"
        assert reply["trace_id"] == "trace-domain"
        assert reply["proves_executor_liveness"] is False
        assert reply["proves_original_effect"] is False
        receipt = reply["contact_receipts"][0]
        assert receipt["schema_version"] == "dharma.a2a.domain_receipt.v1"
        assert receipt["contact_evidence_tier"] == "DOMAIN_RECEIPTED"
        assert receipt["domain_receipt_claim"] is True
        assert receipt["proves_original_effect"] is False
        assert "source_artifact_path" not in receipt
        assert "causation_send_receipt_path" not in receipt
        assert hub_state.raw[-1]["tier"] == "DOMAIN_RECEIPTED"
        assert hub_state.raw[-1]["causation_id"] == "cause-domain"
        assert "/owner/private" not in hub_state.raw[-1]["preview"]

    @pytest.mark.asyncio
    async def test_unmatched_reply_lane_cannot_create_a_dm(
        self, hub_state, cfg, roster_indexes, frozen_now
    ):
        natsio.handle_msg(
            hub_state,
            cfg,
            roster_indexes,
            "dharma.agent.hermes-m5.inbox.reply.unknown-packet",
            json.dumps({"text": "orphan"}).encode(),
            frozen_now,
            live=False,
        )

        assert hub_state.dms == {}
        assert len(hub_state.raw) == 1

    @pytest.mark.asyncio
    async def test_dm_correlation_map_saturates_fail_closed(
        self, hub_state, cfg, roster, fake_js, fake_nc, monkeypatch
    ):
        monkeypatch.setattr(natsio, "MAX_PENDING_DM_CORRELATIONS", 1)
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        first = await natsio.send(
            hub_state,
            cfg,
            roster,
            "first",
            "hermes",
            "m-cap-first",
            principal_scope=PRINCIPAL,
        )
        second = await natsio.send(
            hub_state,
            cfg,
            roster,
            "second",
            "hermes",
            "m-cap-second",
            principal_scope=PRINCIPAL,
        )

        assert first["ok"] is True
        assert second["error"] == "dm_correlation_unavailable"
        assert len(fake_js.published) == 1

    @pytest.mark.asyncio
    async def test_ack_and_reply_race_before_puback_is_correlated(
        self, hub_state, cfg, roster, roster_indexes, frozen_now, fake_nc
    ):
        class ContactBeforeAck:
            published = []
            published_headers = []

            async def publish(self, subject, payload, headers=None, timeout=None):
                del timeout
                self.published.append((subject, payload))
                self.published_headers.append(dict(headers or {}))
                body = json.loads(payload)
                natsio.handle_msg(
                    hub_state,
                    cfg,
                    roster_indexes,
                    body["ack_subject"],
                    json.dumps({"status": "consumed"}).encode(),
                    frozen_now,
                    live=True,
                )
                natsio.handle_msg(
                    hub_state,
                    cfg,
                    roster_indexes,
                    body["reply_subject"],
                    json.dumps(
                        {"message_id": "reply-race", "text": "race answer"}
                    ).encode(),
                    frozen_now + 1,
                    live=True,
                )
                from tests.conftest import FakePubAck

                return FakePubAck(7)

        hub_state.js = ContactBeforeAck()
        hub_state.nc = fake_nc
        result = await natsio.send(
            hub_state,
            cfg,
            roster,
            "race question",
            "hermes",
            "m-contact-race",
            principal_scope=PRINCIPAL,
        )

        assert result["accepted"] is True
        assert result["handler_acknowledged"] is True
        assert result["contact_evidence_tier"] == "HANDLER_ACKED"
        outgoing, reply = hub_state.dms["agni-hermes"]
        assert outgoing["message_id"] == "m-contact-race"
        assert outgoing["handler_acknowledged"] is True
        assert reply["message_id"] == "reply-race"
        assert reply["reply_to_message_id"] == "m-contact-race"
        assert getattr(hub_state, "_a2a_dm_correlations") == {}

    @pytest.mark.asyncio
    async def test_failed_dm_publish_releases_bounded_correlation(
        self, hub_state, cfg, roster, fake_nc
    ):
        from tests.conftest import FakeJS

        hub_state.js = FakeJS(publish_exc=RuntimeError("not published"))
        hub_state.nc = fake_nc
        result = await natsio.send(
            hub_state,
            cfg,
            roster,
            "will fail",
            "hermes",
            "m-failed-dm",
            principal_scope=PRINCIPAL,
            require_jetstream=True,
        )

        assert result["error"] == "jetstream_publish_unavailable"
        assert getattr(hub_state, "_a2a_dm_correlations") == {}

    @pytest.mark.asyncio
    async def test_jetstream_failure_falls_back_to_core_no_ack(
        self, hub_state, cfg, roster, fake_nc
    ):
        from tests.conftest import FakeJS

        hub_state.js = FakeJS(publish_exc=RuntimeError("nats: no responders"))
        hub_state.nc = fake_nc
        res = await natsio.send(
            hub_state, cfg, roster, "fallback msg", None, "m-4",
            principal_scope=PRINCIPAL,
        )
        assert res["ok"] is True
        assert res["ack_tier"] == "NO_ACK"
        assert res["seq"] is None
        assert len(fake_nc.published) == 1
        subject, payload = fake_nc.published[0]
        assert natsio.outbound_echo_key(subject, payload) in hub_state.sent

    @pytest.mark.asyncio
    async def test_echo_arriving_before_puback_is_suppressed_exactly_once(
        self, hub_state, cfg, roster, roster_indexes, frozen_now, fake_nc
    ):
        class EchoBeforeAck:
            published = []
            published_headers = []

            async def publish(self, subject, payload, headers=None, timeout=None):
                del timeout
                self.published.append((subject, payload))
                self.published_headers.append(dict(headers or {}))
                natsio.handle_msg(
                    hub_state,
                    cfg,
                    roster_indexes,
                    subject,
                    payload,
                    frozen_now,
                    live=True,
                )
                from tests.conftest import FakePubAck

                return FakePubAck(7)

        hub_state.js = EchoBeforeAck()
        hub_state.nc = fake_nc
        result = await natsio.send(
            hub_state,
            cfg,
            roster,
            "race-safe",
            None,
            "message-race",
            principal_scope=PRINCIPAL,
        )
        assert result["accepted"] is True
        assert len(hub_state.chat) == 1
        assert hub_state.chat[0]["message_id"] == "message-race"
        assert not hub_state.raw

    @pytest.mark.asyncio
    async def test_unknown_recipient(self, hub_state, cfg, roster, fake_js, fake_nc):
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        res = await natsio.send(
            hub_state, cfg, roster, "hi", "nobody-here", "m-5",
            principal_scope=PRINCIPAL,
        )
        assert res["ok"] is False
        assert "unknown" in res["error"].lower()
        assert fake_js.published == []

    @pytest.mark.asyncio
    async def test_nc_none_is_nats_down(self, hub_state, cfg, roster):
        hub_state.nc = None
        hub_state.js = None
        res = await natsio.send(
            hub_state, cfg, roster, "hi", None, "m-6",
            principal_scope=PRINCIPAL,
        )
        assert res["ok"] is False
        assert "nats" in res["error"].lower()

    @pytest.mark.asyncio
    async def test_v1_requires_jetstream_and_never_falls_back(
        self, hub_state, cfg, roster, fake_nc
    ):
        from tests.conftest import FakeJS

        hub_state.js = FakeJS(publish_exc=RuntimeError("secret internal detail"))
        hub_state.nc = fake_nc
        res = await natsio.send(
            hub_state,
            cfg,
            roster,
            "v1 intent",
            None,
            "m-v1",
            principal_scope=PRINCIPAL,
            idempotency_key="retry-v1",
            require_jetstream=True,
        )
        assert res["ok"] is False
        assert res["accepted"] is False
        assert res["error"] == "jetstream_publish_unavailable"
        assert fake_nc.published == []
        assert "secret internal detail" not in json.dumps(res)

    @pytest.mark.asyncio
    async def test_custom_idempotency_key_is_nats_message_id(
        self, hub_state, cfg, roster, fake_js, fake_nc
    ):
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        await natsio.send(
            hub_state,
            cfg,
            roster,
            "retry-safe",
            None,
            "message-1",
            principal_scope=PRINCIPAL,
            idempotency_key="request-1",
        )
        broker_id = fake_js.published_headers[0]["Nats-Msg-Id"]
        assert broker_id != "request-1"
        assert broker_id != "message-1"

    @pytest.mark.asyncio
    async def test_broker_dedupe_scope_separates_authenticated_principals(
        self, hub_state, cfg, roster, fake_js, fake_nc
    ):
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        for principal in ("session:first", "session:second"):
            await natsio.send(
                hub_state,
                cfg,
                roster,
                "same body",
                None,
                f"message-{principal[-5:]}",
                principal_scope=principal,
                idempotency_key="same-caller-key",
            )
        first, second = [row["Nats-Msg-Id"] for row in fake_js.published_headers]
        assert first != second
        assert "session" not in first + second
        assert "same-caller-key" not in first + second

    @pytest.mark.asyncio
    async def test_duplicate_puback_is_not_a_new_storage_claim(
        self, hub_state, cfg, roster, fake_nc
    ):
        from tests.conftest import FakeJS

        hub_state.js = FakeJS(duplicate=True)
        hub_state.nc = fake_nc
        result = await natsio.send(
            hub_state,
            cfg,
            roster,
            "retry body",
            None,
            "message-retry",
            principal_scope=PRINCIPAL,
            idempotency_key="retry-key",
            require_jetstream=True,
        )
        assert result["accepted"] is True
        assert result["ack_tier"] == "DEDUPLICATED_UNVERIFIED"
        assert result["deduplicated"] is True
        assert result["new_storage_event"] is False
        assert result["current_body_stored"] is None
        assert result["dedupe_scope"].startswith("app_deployment+")
        assert not hub_state.chat

    @pytest.mark.asyncio
    async def test_publish_boundary_has_strict_async_timeout(
        self, hub_state, cfg, roster, fake_nc, monkeypatch
    ):
        class SlowJetStream:
            async def publish(self, *args, **kwargs):
                del args, kwargs
                await asyncio.sleep(60)

        monkeypatch.setattr(natsio, "BROKER_OPERATION_TIMEOUT_S", 0.01)
        hub_state.js = SlowJetStream()
        hub_state.nc = fake_nc
        result = await natsio.send(
            hub_state,
            cfg,
            roster,
            "bounded timeout",
            None,
            "message-timeout",
            principal_scope=PRINCIPAL,
            require_jetstream=True,
        )
        assert result["accepted"] is False
        assert result["error"] == "jetstream_publish_unavailable"


@pytest.mark.asyncio
async def test_replay_queries_canonical_and_legacy_observation_lanes(
    hub_state, cfg, roster
):
    from tests.conftest import FakePullSub

    class NotFound(Exception):
        pass

    class RecordingReplayJS:
        def __init__(self):
            self.lanes = []

        async def get_last_msg(self, stream, subject):
            self.lanes.append((stream, subject))
            raise NotFound(subject)

        async def pull_subscribe(self, subject, stream=None, config=None):
            del subject, stream, config
            return FakePullSub()

    replay_js = RecordingReplayJS()
    hub_state.js = replay_js

    await natsio.replay(hub_state, cfg, roster)

    assert replay_js.lanes == [
        ("DHARMA_A2A", "dharma.agent.hermes-m5.inbox"),
        ("DHARMA_A2A", "dharma.a2a.hermes"),
        ("DHARMA_A2A", "dharma.a2a.fleet.reply.meghadharma_hermes"),
    ]
    assert hub_state.replay["ok"] is True


@pytest.mark.asyncio
async def test_live_loop_subscribes_canonical_and_compatibility_namespaces(
    hub_state, cfg, roster, monkeypatch
):
    class LoopNC:
        def __init__(self):
            self.is_connected = False
            self.subjects = []
            self.closed = False

        def jetstream(self):
            return object()

        async def subscribe(self, subject, cb=None):
            assert cb is not None
            self.subjects.append(subject)

        async def close(self):
            self.closed = True

    nc = LoopNC()

    async def connect(*args, **kwargs):
        del args, kwargs
        return nc

    async def stop_after_first_connection(delay):
        assert delay == 5
        raise asyncio.CancelledError

    cfg.url = "nats://unit.test:4222"
    cfg.user = None
    cfg.password = None
    hub_state.replay["ran_at"] = "already-replayed"
    monkeypatch.setattr(natsio.nats, "connect", connect)
    monkeypatch.setattr(natsio.asyncio, "sleep", stop_after_first_connection)

    with pytest.raises(asyncio.CancelledError):
        await natsio.nats_loop(hub_state, cfg, roster)

    assert nc.subjects == [
        "dharma.agent.>",
        "dharma.a2a.>",
        CHAT_SUBJECT,
    ]
    assert nc.closed is True


def test_payload_sender_remains_reported_unverified(
    hub_state, cfg, roster_indexes, frozen_now
):
    data = json.dumps({"from": "hermes", "text": "claimed sender"}).encode()
    natsio.handle_msg(
        hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=False
    )
    message = hub_state.chat[0]
    assert message["from"] == "hermes"
    assert message["sender_claim"] == {
        "value": "hermes",
        "status": "reported_unverified",
        "source": "nats.payload",
        "matched_roster_uid": "agni-hermes",
    }
    signal = hub_state.presence["agni-hermes"]
    assert signal["last_heard"] is None
    assert signal["last_reported_heard"] == frozen_now
    assert signal["last_reported_heard_source"] == "nats.payload_sender_claim"
