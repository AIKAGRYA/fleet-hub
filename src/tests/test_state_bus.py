"""Contract: hub/state.py (CONTRACT.md "hub/ module interfaces" — state).

Verifies: EventBus epoch-prefixed monotonic ids, since() resume semantics
(None / same-epoch tail / wrong-epoch reset / fallen-off-buffer reset),
attach/detach fan-out, SentIds LRU cap, and HubState default fields.
"""
from __future__ import annotations

import pytest

state_mod = pytest.importorskip("hub.state")


class TestEventBusPublish:
    def test_sequential_ids_with_epoch_prefix(self):
        bus = state_mod.EventBus()
        e1 = bus.publish("chat", {"a": 1})
        e2 = bus.publish("raw", {"b": 2})
        assert e1["id"] == f"{bus.epoch}-1"
        assert e2["id"] == f"{bus.epoch}-2"

    def test_envelope_shape(self):
        bus = state_mod.EventBus()
        env = bus.publish("presence", {"uid": "x"})
        assert set(env.keys()) >= {"id", "event", "data"}
        assert env["event"] == "presence"
        assert env["data"] == {"uid": "x"}

    def test_epoch_is_integer_string(self):
        bus = state_mod.EventBus()
        int(bus.epoch)  # raises if not numeric


class TestEventBusSince:
    def test_none_returns_empty_no_reset(self):
        bus = state_mod.EventBus()
        bus.publish("chat", {})
        assert bus.since(None) == ([], False)

    def test_same_epoch_mid_id_returns_tail(self):
        bus = state_mod.EventBus()
        bus.publish("chat", {"n": 1})
        e2 = bus.publish("chat", {"n": 2})
        e3 = bus.publish("chat", {"n": 3})
        events, needs_reset = bus.since(f"{bus.epoch}-1")
        assert needs_reset is False
        assert [e["id"] for e in events] == [e2["id"], e3["id"]]

    def test_up_to_date_returns_empty_no_reset(self):
        bus = state_mod.EventBus()
        e = bus.publish("chat", {})
        events, needs_reset = bus.since(e["id"])
        assert events == []
        assert needs_reset is False

    def test_wrong_epoch_needs_reset(self):
        bus = state_mod.EventBus()
        bus.publish("chat", {})
        events, needs_reset = bus.since("1-1")
        assert events == []
        assert needs_reset is True

    def test_fallen_off_capacity_needs_reset(self):
        bus = state_mod.EventBus(capacity=3)
        for i in range(5):  # ids 1..5; ring holds 3..5
            bus.publish("raw", {"i": i})
        events, needs_reset = bus.since(f"{bus.epoch}-1")
        assert events == []
        assert needs_reset is True

    def test_in_ring_after_wraparound_still_resumes(self):
        bus = state_mod.EventBus(capacity=3)
        for i in range(5):
            bus.publish("raw", {"i": i})
        events, needs_reset = bus.since(f"{bus.epoch}-3")
        assert needs_reset is False
        assert [e["id"] for e in events] == [f"{bus.epoch}-4", f"{bus.epoch}-5"]


class TestEventBusAttachDetach:
    def test_attached_queue_receives_publishes(self):
        bus = state_mod.EventBus()
        q = bus.attach()
        env = bus.publish("chat", {"x": 1})
        assert q.get_nowait() == env

    def test_detached_queue_stops_receiving(self):
        bus = state_mod.EventBus()
        q = bus.attach()
        bus.detach(q)
        bus.publish("chat", {})
        assert q.empty()


class TestSentIds:
    def test_membership(self):
        s = state_mod.SentIds(cap=3)
        s.add("a")
        assert "a" in s
        assert "b" not in s

    def test_lru_eviction_at_cap(self):
        s = state_mod.SentIds(cap=3)
        for i in ("a", "b", "c"):
            s.add(i)
        s.add("d")  # oldest ("a") evicted
        assert "a" not in s
        assert "b" in s and "c" in s and "d" in s


class TestHubState:
    def test_default_fields(self):
        st = state_mod.HubState()
        assert st.chat.maxlen == 300
        assert st.raw.maxlen == 400
        assert st.dms == {}
        assert st.presence == {}
        assert st.nc is None
        assert st.js is None
        assert st.connected is False
        assert st.last_error is None
        assert st.messages == 0
        assert st.last_seq is None
        assert isinstance(st.bus, state_mod.EventBus)
        assert set(st.replay.keys()) >= {"ok", "scanned", "took_ms", "error", "ran_at"}
        assert st.replay["ok"] is None
