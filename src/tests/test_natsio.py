"""Contract: hub/natsio.py (CONTRACT.md "hub/ module interfaces" — natsio).

Verifies: handle_msg sync core (chat append + synthesized srv- msg_id, echo
suppression via state.sent, raw preview truncation, DM routing by subject,
json fallback, live bus emits) and async send() (JetStream PUBLISH_ACCEPTED
ack, core-NATS NO_ACK fallback, unknown recipient, nats-down error, payload
shape with via fleet-hub-v0.6).

Uses fakes from conftest — no network, no real NATS.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

natsio = pytest.importorskip("hub.natsio")
state_mod = pytest.importorskip("hub.state")

CHAT_SUBJECT = "dharma.fleet.chat"


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

    def test_sender_presence_updated(self, hub_state, cfg, roster_indexes, frozen_now):
        data = json.dumps({"from": "hermes", "text": "hi"}).encode()
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=False
        )
        assert hub_state.presence["agni-hermes"]["last_heard"] == frozen_now

    def test_echo_suppression_own_msg_id(self, hub_state, cfg, roster_indexes, frozen_now):
        hub_state.sent.add("op-1")
        data = json.dumps({"msg_id": "op-1", "from": "hermes", "text": "echo"}).encode()
        natsio.handle_msg(
            hub_state, cfg, roster_indexes, CHAT_SUBJECT, data, frozen_now, live=False
        )
        # chat NOT re-appended, but presence still updated
        assert len(hub_state.chat) == 0
        assert hub_state.presence["agni-hermes"]["last_heard"] == frozen_now

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


class TestSend:
    @pytest.mark.asyncio
    async def test_jetstream_publish_accepted(self, hub_state, cfg, roster, fake_js, fake_nc):
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        res = await natsio.send(hub_state, cfg, roster, "hello fleet", None, "m-1")
        assert res["ok"] is True
        assert res["ack_tier"] == "PUBLISH_ACCEPTED"
        assert res["seq"] == 7
        assert len(fake_js.published) == 1
        subject, payload = fake_js.published[0]
        assert subject == CHAT_SUBJECT
        body = json.loads(payload)
        assert body["msg_id"] == "m-1"
        assert body["via"] == "fleet-hub-v0.6"
        assert body["from"] == "operator"
        assert body["text"] == "hello fleet"

    @pytest.mark.asyncio
    async def test_send_records_sent_id_and_appends_locally(
        self, hub_state, cfg, roster, fake_js, fake_nc
    ):
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        await natsio.send(hub_state, cfg, roster, "hi", None, "m-2")
        assert "m-2" in hub_state.sent
        # local append so the sender sees it immediately
        assert len(hub_state.chat) == 1
        assert hub_state.chat[0]["msg_id"] == "m-2"

    @pytest.mark.asyncio
    async def test_send_dm_by_callsign(self, hub_state, cfg, roster, fake_js, fake_nc):
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        res = await natsio.send(hub_state, cfg, roster, "psst", "hermes", "m-3")
        assert res["ok"] is True
        subject, _ = fake_js.published[0]
        assert subject == "dharma.a2a.hermes"
        assert len(hub_state.dms.get("agni-hermes", ())) == 1

    @pytest.mark.asyncio
    async def test_jetstream_failure_falls_back_to_core_no_ack(
        self, hub_state, cfg, roster, fake_nc
    ):
        from tests.conftest import FakeJS

        hub_state.js = FakeJS(publish_exc=RuntimeError("nats: no responders"))
        hub_state.nc = fake_nc
        res = await natsio.send(hub_state, cfg, roster, "fallback msg", None, "m-4")
        assert res["ok"] is True
        assert res["ack_tier"] == "NO_ACK"
        assert res["seq"] is None
        assert len(fake_nc.published) == 1
        assert "m-4" in hub_state.sent  # recorded even though JS ack failed

    @pytest.mark.asyncio
    async def test_unknown_recipient(self, hub_state, cfg, roster, fake_js, fake_nc):
        hub_state.js = fake_js
        hub_state.nc = fake_nc
        res = await natsio.send(hub_state, cfg, roster, "hi", "nobody-here", "m-5")
        assert res["ok"] is False
        assert "unknown" in res["error"].lower()
        assert fake_js.published == []

    @pytest.mark.asyncio
    async def test_nc_none_is_nats_down(self, hub_state, cfg, roster):
        hub_state.nc = None
        hub_state.js = None
        res = await natsio.send(hub_state, cfg, roster, "hi", None, "m-6")
        assert res["ok"] is False
        assert "nats" in res["error"].lower()
