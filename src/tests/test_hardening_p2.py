"""Focused regressions for the Fleet Hub v1 P2 hardening pass."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from hub import auth, natsio
from hub import state as state_mod
from hub.mission_contract import REDACTED, validate_owner_snapshot
from hub.mission_provider import (
    MissionCatalog,
    MissionProvider,
    MissionSnapshotProjection,
)
from hub.needs_john import MAX_EVIDENCE_REFS, derive_needs_john
from tests.conftest import FakeJS, FakeNC
from tests.test_mission_projection import owner_snapshot, task


TOKEN = "testtoken"
PRINCIPAL = "session:hardening-test"


@pytest.fixture
def cfg():
    return SimpleNamespace(
        chat_subject="dharma.fleet.chat",
        stream="DHARMA_A2A",
        replay_hours=48,
        replay_streams=["DHARMA_A2A"],
        recent_window_s=7200,
    )


def _login(client) -> dict[str, str]:
    response = client.post("/login", json={"token": TOKEN})
    assert response.status_code == 200
    headers = {
        "X-CSRF-Token": response.json()["csrf_token"],
        "Origin": "http://testserver",
        "Sec-Fetch-Site": "same-origin",
    }
    client.headers.update(headers)
    return headers


def _attempt(index: int) -> dict:
    return {
        "attempt_id": f"attempt-{index:03d}",
        "mission_id": "mission-alpha",
        "session_id": "mission:mission-alpha",
        "task_id": "task-1",
        "claim_id": f"claim-{index:03d}",
        "assigned_to": "agent-1",
        "assigned_by": "john",
        "status": "complete",
        "failure_code": "",
        "idempotency_key": f"attempt-key-{index:03d}",
        "metadata": {},
        "started_at": "2026-08-25T01:00:00Z",
        "completed_at": "2026-08-25T01:01:00Z",
    }


def _lease(index: int) -> dict:
    return {
        "claim_id": f"claim-{index:03d}",
        "mission_id": "mission-alpha",
        "session_id": "mission:mission-alpha",
        "task_id": "task-1",
        "agent_id": "agent-1",
        "attempt_id": f"attempt-{index:03d}",
        "status": "expired",
        "active": False,
        "expired": True,
        "heartbeat_at": "2026-08-25T01:00:00Z",
        "stale_after": "2026-08-25T01:05:00Z",
        "metadata": {},
    }


def _receipt(index: int) -> dict:
    return {
        "receipt_id": f"receipt-{index:03d}",
        "mission_id": "mission-alpha",
        "task_id": "task-1",
        "attempt_id": f"attempt-{index:03d}",
        "agent_id": "agent-1",
        "receipt_type": "terminal",
        "status": "recorded",
        "idempotency_key": f"receipt-key-{index:03d}",
        "payload": {},
        "created_at": "2026-08-25T01:01:00Z",
    }


@pytest.mark.parametrize(
    "invalid_id",
    ["mission/alpha", "mission alpha", "mission-α", "_mission", "-mission"],
)
def test_owner_identifiers_use_the_bounded_ascii_wire_grammar(invalid_id):
    raw = owner_snapshot()
    raw["mission"]["mission_id"] = invalid_id

    with pytest.raises(ValidationError):
        validate_owner_snapshot(raw)


@pytest.mark.parametrize(
    ("axis", "records"),
    [
        ("task_id", [task("task-duplicate"), task("task-duplicate")]),
        ("attempt_id", [_attempt(1), _attempt(1)]),
        ("claim_id", [_lease(1), _lease(1)]),
        ("receipt_id", [_receipt(1), _receipt(1)]),
    ],
)
def test_owner_snapshot_rejects_duplicate_identity_on_each_axis(axis, records):
    raw = owner_snapshot(tasks=[task()])
    collection = {
        "task_id": "tasks",
        "attempt_id": "attempts",
        "claim_id": "leases",
        "receipt_id": "receipts",
    }[axis]
    raw[collection] = deepcopy(records)

    with pytest.raises(ValidationError, match=f"duplicate {axis}"):
        validate_owner_snapshot(raw)


def test_open_json_redacts_camel_case_and_sensitive_suffix_keys():
    secrets = {
        "apiToken": "camel-token-value",
        "deploymentSecret": "suffix-secret-value",
        "serviceClientSecret": "client-secret-value",
        "nested": {"githubAccessToken": "nested-token-value"},
    }
    snapshot = validate_owner_snapshot(owner_snapshot(metadata=secrets))

    assert snapshot.mission.metadata == {
        "apiToken": REDACTED,
        "deploymentSecret": REDACTED,
        "nested": {"githubAccessToken": REDACTED},
        "serviceClientSecret": REDACTED,
    }
    dumped = snapshot.model_dump_json()
    assert all(value not in dumped for value in secrets.values() if isinstance(value, str))
    assert "nested-token-value" not in dumped


def test_required_evidence_precedes_and_survives_optional_evidence_cap():
    raw = owner_snapshot("missing_terminal_receipt", tasks=[task()])
    raw["attempts"] = [_attempt(index) for index in range(MAX_EVIDENCE_REFS + 8)]
    raw["receipts"] = [_receipt(index) for index in range(MAX_EVIDENCE_REFS + 8)]

    item = derive_needs_john(validate_owner_snapshot(raw)).items[0]

    assert len(item.evidence_refs) == MAX_EVIDENCE_REFS
    assert item.evidence_refs[:2] == (
        "mission:mission-alpha",
        "reconciliation:missing_terminal_receipt",
    )
    assert item.evidence_refs[2] == "attempt:attempt-000"


@pytest.mark.asyncio
async def test_ambiguous_roster_name_is_not_addressable(cfg, roster, fake_js, fake_nc):
    duplicate = deepcopy(roster)
    duplicate["agents"]["second-hermes"] = {
        **duplicate["agents"]["agni-hermes"],
        "subject": "dharma.a2a.second-hermes",
    }
    state = state_mod.HubState()
    state.nc = fake_nc
    state.js = fake_js

    result = await natsio.send(
        state,
        cfg,
        duplicate,
        "hello",
        "hermes",
        "message-ambiguous",
        principal_scope=PRINCIPAL,
        require_jetstream=True,
    )

    assert result == {
        "ok": False,
        "accepted": False,
        "error": "ambiguous_recipient",
    }
    assert fake_js.published == []


@pytest.mark.asyncio
async def test_archived_roster_name_is_not_addressable(cfg, roster, fake_js, fake_nc):
    state = state_mod.HubState()
    state.nc = fake_nc
    state.js = fake_js

    result = await natsio.send(
        state,
        cfg,
        roster,
        "hello",
        "composer",
        "message-archived",
        principal_scope=PRINCIPAL,
        require_jetstream=True,
    )

    assert result == {
        "ok": False,
        "accepted": False,
        "error": "recipient_archived",
    }
    assert fake_js.published == []


@pytest.mark.asyncio
async def test_failed_publish_removes_pre_registered_echo_key(cfg, roster, fake_nc):
    class RecordingFailure:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bytes]] = []

        async def publish(self, subject, payload, headers=None, timeout=None):
            del headers, timeout
            self.calls.append((subject, payload))
            raise RuntimeError("publish failed")

    state = state_mod.HubState()
    state.nc = fake_nc
    state.js = RecordingFailure()

    result = await natsio.send(
        state,
        cfg,
        roster,
        "hello",
        None,
        "message-failed",
        principal_scope=PRINCIPAL,
        require_jetstream=True,
    )

    subject, payload = state.js.calls[0]
    assert result["error"] == "jetstream_publish_unavailable"
    assert natsio.outbound_echo_key(subject, payload) not in state.sent


def test_v1_unknown_recipient_is_a_deterministic_422(configured):
    client, server = configured
    _login(client)
    server.STATE.nc = FakeNC()
    server.STATE.js = FakeJS()

    response = client.post(
        "/api/v1/intents/chat",
        json={"text": "hello", "to": "not-in-roster"},
        headers={"Idempotency-Key": "unknown-recipient-1"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_recipient"
    assert response.json()["accepted"] is False
    assert server.STATE.js.published == []


@pytest.mark.parametrize(
    "error_code",
    [
        "packet_id_invalid",
        "recipient_inbox_unratified",
        "sender_identity_invalid",
    ],
)
def test_v1_invalid_dm_addressing_is_a_deterministic_422(
    configured, monkeypatch, error_code
):
    client, server = configured
    _login(client)

    async def reject_address(*args, **kwargs):
        del args, kwargs
        return {"ok": False, "accepted": False, "error": error_code}

    monkeypatch.setattr(server.natsio, "send", reject_address)
    response = client.post(
        "/api/v1/intents/chat",
        json={"text": "hello", "to": "hermes"},
        headers={"Idempotency-Key": f"invalid-address-{error_code}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == error_code
    assert response.json()["accepted"] is False


def test_bearer_authenticated_logout_also_revokes_present_cookie(configured):
    client, server = configured
    _login(client)
    cookie = client.cookies.get(auth.COOKIE_NAME)
    assert server.SESSIONS.get(cookie) is not None

    response = client.post(
        "/logout", headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 200
    assert response.json()["revoked"] is True
    assert server.SESSIONS.get(cookie) is None
    client.cookies.set(auth.COOKIE_NAME, cookie)
    assert client.get("/api/roster").status_code == 401


def test_provider_model_instances_are_revalidated_before_projection(configured):
    client, server = configured
    _login(client)

    class InvalidProvider:
        configured_mission_ids = ("mission-alpha",)
        discovery_complete = False
        commands = ()

        async def list_missions(self):
            # model_construct models the strongest bypass attempt: a provider
            # hands the server the expected Python type with invalid internals.
            return MissionCatalog.model_construct(
                available=True,
                configured_mission_ids=("mission-alpha",),
                missions=(),
                discovery_complete=True,
                commands=("delete",),
                commands_available=True,
                authority="UntrustedProvider",
                error_code=None,
            )

        async def get_snapshot(self, mission_id):
            del mission_id
            raise AssertionError("not reached")

    provider = InvalidProvider()
    assert isinstance(provider, MissionProvider)
    server.app.state.mission_provider = provider

    response = client.get("/api/v1/missions")

    assert response.status_code == 503
    assert "UntrustedProvider" not in response.text
    assert "delete" not in response.text


def test_nested_owner_model_instances_are_revalidated_before_projection(configured):
    client, server = configured
    _login(client)

    good_catalog = MissionCatalog(
        available=True,
        configured_mission_ids=("mission-alpha",),
        missions=(),
    )
    # The nested snapshot is the second bypass seam: the outer projection can
    # be valid while an owner DTO was created without validating its literal
    # authority and executor-liveness claims.
    forged_snapshot = validate_owner_snapshot(owner_snapshot()).model_copy(
        update={
            "authority": "UntrustedProvider",
            "proves_executor_liveness": True,
        }
    )

    class InvalidSnapshotProvider:
        configured_mission_ids = ("mission-alpha",)
        discovery_complete = False
        commands = ()

        async def list_missions(self):
            return good_catalog

        async def get_snapshot(self, mission_id):
            return MissionSnapshotProjection.model_construct(
                mission_id=mission_id,
                available=True,
                snapshot=forged_snapshot,
                source_version="forged:v1",
                discovery_complete=False,
                commands=(),
                commands_available=False,
                authority="TaskBoard+RuntimeStateStore",
                proves_executor_liveness=False,
                error_code=None,
            )

    provider = InvalidSnapshotProvider()
    assert isinstance(provider, MissionProvider)
    server.app.state.mission_provider = provider

    response = client.get("/api/v1/missions/mission-alpha/snapshot")

    assert response.status_code == 503
    assert response.json()["error_code"] == "mission_provider_unavailable"
    assert "UntrustedProvider" not in response.text
    assert "forged:v1" not in response.text


def test_sse_payload_cannot_override_server_authority_metadata(configured):
    _, server = configured
    frame = server._sse_frame(
        {
            "id": f"{server.STATE.bus.epoch}-1",
            "event": "chat",
            "data": {
                "text": "hello",
                "schema_version": "attacker.schema",
                "observed_at": "1900-01-01T00:00:00Z",
                "source": "UntrustedEnvelope",
                "resume_scope": "durable-global",
            },
        }
    )
    data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
    data = json.loads(data_line.removeprefix("data: "))

    assert data["text"] == "hello"
    assert data["schema_version"] == "fleet-hub.event.v1"
    assert data["source"] == "FleetHub.process_event_bus"
    assert data["resume_scope"] == "process_local"
    assert data["observed_at"] != "1900-01-01T00:00:00Z"
