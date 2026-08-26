"""Contract: hub/natsio.py (CONTRACT.md "hub/ module interfaces" — natsio).

Verifies: handle_msg sync core (chat append + synthesized srv- msg_id, echo
suppression via state.sent, raw preview truncation, DM routing by subject,
json fallback, live bus emits) and async send() (JetStream PUBLISH_ACCEPTED
ack, core-NATS NO_ACK fallback, unknown recipient, nats-down error, payload
shape with a canonical envelope and broker dedupe header).

Uses fakes from conftest — no network, no real NATS.
"""
from __future__ import annotations

import asyncio
import json
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
    )


@pytest.fixture
def hub_state():
    return state_mod.HubState()


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

    def test_dm_routed_to_subject_uid(self, hub_state, cfg, roster_indexes, frozen_now):
        data = json.dumps({"from": "operator-x", "text": "private ping"}).encode()
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, "dharma.a2a.hermes", data, frozen_now, live=False
        )
        assert "agni-hermes" in hub_state.dms
        assert len(hub_state.dms["agni-hermes"]) == 1
        assert hub_state.dms["agni-hermes"][0]["text"] == "private ping"
        # a DM is not group chat
        assert len(hub_state.chat) == 0

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
        subject, _ = fake_js.published[0]
        assert subject == "dharma.a2a.hermes"
        assert len(hub_state.dms.get("agni-hermes", ())) == 1
        assert res["route_plan"]["mode"] == "direct_message"
        assert res["route_plan"]["recipient_uid"] == "agni-hermes"

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
