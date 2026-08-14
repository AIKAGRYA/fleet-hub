"""Fleet Hub v0.6 test fixtures.

Written against the binding build contract (CONTRACT.md): hub/ module
interfaces, roster.json v2 shape, env-at-import server config. The backend
may land after these tests are written — heavy imports (server) happen inside
fixtures, and each test module guards its hub.* imports with
pytest.importorskip so collection never crashes on an absent backend.

Run from src/:  python3 -m pytest tests/ -q
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

# Defensive: make src/ importable even if pytest is invoked from elsewhere.
SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

TEST_TOKEN = "testtoken"


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

@pytest.fixture
def frozen_now() -> float:
    """A fixed unix timestamp; presence fns take `now` explicitly (contract)."""
    return 1_755_100_000.0


# ---------------------------------------------------------------------------
# Roster (contract: roster.json v2 — do NOT read the real src/roster.json)
# ---------------------------------------------------------------------------

@pytest.fixture
def roster() -> dict:
    """Minimal 3-seat roster in fleet_roster.v2 shape.

    - agni-hermes: active, plain a2a subject
    - meghadharma-hermes: active, reply-style subject
    - fable_composer: archived seat
    """
    return {
        "schema": "fleet_roster.v2",
        "updated": "2026-08-14",
        "amnesty": "truth-amnesty-2026-08-14",
        "count": 3,
        "agents": {
            "agni-hermes": {
                "callsign": "hermes",
                "display_name": "AGNI Hermes",
                "subject": "dharma.a2a.hermes",
                "host": "AGNI VPS 157.245.193.15",
                "tailscale": "100.79.111.89",
                "role": "Infrastructure / NATS Hub",
                "model": "glm-5.2",
                "provider": "zai",
                "seat": "active",
                "bio": "Infrastructure anchor.",
            },
            "meghadharma-hermes": {
                "callsign": "fleet.reply.meghadharma_hermes",
                "display_name": "Meghadharma Hermes",
                "subject": "dharma.a2a.fleet.reply.meghadharma_hermes",
                "host": "Meghadharma VPS 178.128.87.170",
                "tailscale": "100.103.106.70",
                "role": "Hub / Semantic Bridge",
                "model": "kimi-k3",
                "provider": "kimi_code",
                "seat": "active",
                "bio": "Semantic bridge.",
            },
            "fable_composer": {
                "callsign": "composer",
                "display_name": "Fable Composer",
                "subject": "dharma.a2a.fable_composer",
                "host": "roaming",
                "tailscale": "",
                "role": "Editor seat",
                "model": "fable-5",
                "provider": "anthropic",
                "seat": "archived",
                "archive_reason": "truth amnesty 2026-08-14 — awaiting proof-of-life heartbeat",
                "bio": "Archived editor seat.",
            },
        },
    }


@pytest.fixture
def roster_index_by_subject(roster) -> dict:
    """subject -> uid map (contract: uid_for_subject roster_index)."""
    return {a["subject"]: uid for uid, a in roster["agents"].items()}


@pytest.fixture
def roster_index_by_name(roster) -> dict:
    """lowercased uid/callsign/display_name -> uid (contract: resolve_sender
    matches uid, callsign, display_name case-insensitively)."""
    idx: dict = {}
    for uid, a in roster["agents"].items():
        idx[uid.lower()] = uid
        idx[a["callsign"].lower()] = uid
        idx[a["display_name"].lower()] = uid
    return idx


@pytest.fixture
def roster_indexes(roster, roster_index_by_subject, roster_index_by_name):
    """The indexes bundle handle_msg receives. Contract names the argument
    `roster_indexes` without pinning its shape; prefer a builder exported by
    hub.natsio if one exists, else a plain dict of both indexes."""
    natsio = pytest.importorskip("hub.natsio")
    for name in ("build_indexes", "roster_indexes", "make_indexes", "index_roster"):
        fn = getattr(natsio, name, None)
        if callable(fn):
            return fn(roster)
    return {
        "by_subject": roster_index_by_subject,
        "by_name": roster_index_by_name,
    }


# ---------------------------------------------------------------------------
# NATS fakes (contract: hub/natsio.py send/handle_msg)
# ---------------------------------------------------------------------------

class FakeMsg:
    """Minimal NATS message: subject, data bytes, and a seq via metadata."""

    class _Meta:
        class _SeqPair:
            def __init__(self, stream):
                self.stream = stream
                self.consumer = stream

        def __init__(self, seq):
            self.sequence = self._SeqPair(seq)

    def __init__(self, subject: str, data: bytes, seq: int = 1):
        self.subject = subject
        self.data = data
        self.metadata = self._Meta(seq)
        self.headers = None


class FakePubAck:
    def __init__(self, seq: int = 7):
        self.seq = seq
        self.stream = "DHARMA_A2A"


class FakePullSub:
    """fetch() returns scripted batches, then raises TimeoutError."""

    def __init__(self, batches=None):
        self._batches = list(batches or [])

    async def fetch(self, batch: int = 500, timeout: float = 2):
        if self._batches:
            return self._batches.pop(0)
        raise TimeoutError("no more messages")

    async def unsubscribe(self):
        return None


class FakeJS:
    """JetStream fake: publish records calls and acks seq 7 (or raises)."""

    def __init__(self, publish_exc: Exception | None = None,
                 last_msgs: dict | None = None, batches=None):
        self.published: list[tuple[str, bytes]] = []
        self.publish_exc = publish_exc
        self.last_msgs = last_msgs or {}
        self._batches = batches

    async def publish(self, subject: str, payload: bytes, timeout=None):
        if self.publish_exc is not None:
            raise self.publish_exc
        self.published.append((subject, payload))
        return FakePubAck(7)

    async def get_last_msg(self, stream: str, subject: str):
        try:
            return self.last_msgs[subject]
        except KeyError:
            raise Exception(f"no message found on {subject}")

    async def pull_subscribe(self, subject: str, stream=None, config=None,
                             durable=None):
        return FakePullSub(self._batches)


class FakeNC:
    """Core NATS fake: publish records and returns None (no ack)."""

    def __init__(self):
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, payload: bytes):
        self.published.append((subject, payload))
        return None


@pytest.fixture
def fake_js():
    return FakeJS()


@pytest.fixture
def fake_nc():
    return FakeNC()


# ---------------------------------------------------------------------------
# FastAPI TestClient (contract: env read at import in server config section,
# so set env first, then import + reload server)
# ---------------------------------------------------------------------------

def _build_client(token: str | None, tmp_path, roster, monkeypatch):
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps(roster))
    vision_path = tmp_path / "vision.json"
    vision_path.write_text(json.dumps({
        "schema": "fleet_vision.v1",
        "north_star": "test",
        "updated": "2026-08-14",
        "ventures": [],
    }))

    monkeypatch.setenv("FLEET_HUB_ROSTER", str(roster_path))
    monkeypatch.setenv("FLEET_HUB_VISION", str(vision_path))
    # http:// test transport: Secure-flagged cookies would not be replayed by
    # the client cookie jar, so exercise the documented insecure-cookie knob.
    monkeypatch.setenv("FLEET_HUB_INSECURE_COOKIE", "1")
    if token is None:
        monkeypatch.setenv("FLEET_HUB_TOKEN", "")  # empty => LOCKED
    else:
        monkeypatch.setenv("FLEET_HUB_TOKEN", token)

    import server  # heavy import deliberately inside the fixture
    server = importlib.reload(server)

    from fastapi.testclient import TestClient

    # No context manager: lifespan (nats_loop) intentionally NOT started —
    # routes must answer honestly with no broker.
    client = TestClient(server.app, raise_server_exceptions=False)
    return client, server


@pytest.fixture
def configured(tmp_path, roster, monkeypatch):
    """(client, server_module) with FLEET_HUB_TOKEN=testtoken."""
    client, server = _build_client(TEST_TOKEN, tmp_path, roster, monkeypatch)
    yield client, server
    client.close()


@pytest.fixture
def unconfigured(tmp_path, roster, monkeypatch):
    """(client, server_module) with FLEET_HUB_TOKEN empty (locked)."""
    client, server = _build_client(None, tmp_path, roster, monkeypatch)
    yield client, server
    client.close()


@pytest.fixture
def client(configured):
    return configured[0]


@pytest.fixture
def unconfigured_client(unconfigured):
    return unconfigured[0]
