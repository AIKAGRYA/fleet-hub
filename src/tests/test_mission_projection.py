"""Contract tests for Fleet Hub's read-only Mission Control boundary."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from hub.mission_contract import (
    AUTHORITY,
    MAX_OPEN_ITEMS,
    MAX_OPEN_STRING_CHARS,
    REDACTED,
    ReconciliationState,
    snapshot_version,
    validate_owner_snapshot,
)
from hub.mission_provider import (
    FakeMissionProvider,
    MissionProvider,
    UnavailableMissionProvider,
)
from hub.needs_john import NeedsJohnItem, derive_needs_john


OBSERVED_AT = "2026-08-26T00:00:00Z"


def owner_snapshot(
    reconciliation: str = "coherent",
    *,
    tasks: list[dict] | None = None,
    leases: list[dict] | None = None,
    metadata: dict | None = None,
) -> dict:
    return {
        "mission": {
            "mission_id": "mission-alpha",
            "session_id": "mission:mission-alpha",
            "title": "Alpha",
            "goal": "Ship an honest phone projection",
            "operator_id": "john",
            "status": "active",
            "metadata": metadata or {},
            "created_at": "2026-08-25T00:00:00Z",
            "updated_at": "2026-08-26T00:00:00Z",
        },
        "tasks": tasks or [],
        "attempts": [],
        "leases": leases or [],
        "receipts": [],
        "reconciliation": reconciliation,
        "observed_at": OBSERVED_AT,
        "authority": AUTHORITY,
        "proves_executor_liveness": False,
    }


def task(task_id: str = "task-1", status: str = "running") -> dict:
    return {
        "task_id": task_id,
        "mission_id": "mission-alpha",
        "title": "One task",
        "description": "",
        "status": status,
        "priority": "normal",
        "assigned_to": "agent-1",
        "result": "",
        "metadata": {},
        "created_at": "2026-08-25T01:00:00Z",
        "updated_at": "2026-08-26T01:00:00Z",
    }


def lease(*, expired: bool = True, attempt_id: str = "attempt-1") -> dict:
    return {
        "claim_id": "claim-1",
        "mission_id": "mission-alpha",
        "session_id": "mission:mission-alpha",
        "task_id": "task-1",
        "agent_id": "agent-1",
        "attempt_id": attempt_id,
        "status": "active",
        "active": not expired,
        "expired": expired,
        "heartbeat_at": "2026-08-25T01:00:00Z",
        "stale_after": "2026-08-25T01:05:00Z",
        "metadata": {},
    }


EXPECTED_RECONCILIATION_STATES = {
    "coherent",
    "needs_task_projection",
    "missing_terminal_receipt",
    "conflicting_active_claims",
    "active_claim_without_run",
    "expired_lease",
    "evidence_scan_saturated",
    "foreign_runtime_record",
    "conflicting_terminal_evidence",
}


def test_wire_contract_has_all_nine_owner_reconciliation_states():
    assert {state.value for state in ReconciliationState} == (
        EXPECTED_RECONCILIATION_STATES
    )
    assert len(ReconciliationState) == 9


def test_wire_contract_never_accepts_executor_liveness_or_foreign_authority():
    raw = owner_snapshot()
    raw["proves_executor_liveness"] = True
    with pytest.raises(ValidationError):
        validate_owner_snapshot(raw)

    raw = owner_snapshot()
    raw["authority"] = "FleetHubCache"
    with pytest.raises(ValidationError):
        validate_owner_snapshot(raw)


def test_wire_contract_requires_aware_observation_time():
    raw = owner_snapshot()
    raw["observed_at"] = "2026-08-26T00:00:00"
    with pytest.raises(ValidationError):
        validate_owner_snapshot(raw)


def test_open_owner_json_is_recursively_redacted_bounded_and_allowlisted():
    huge = "x" * (MAX_OPEN_STRING_CHARS + 10)
    metadata = {
        "api_key": "top-secret",
        "nested": {
            "authorization": "Bearer should-not-leak",
            "safe": [{"password": "also-secret", "label": huge}],
        },
        "many": list(range(MAX_OPEN_ITEMS + 20)),
    }
    raw = owner_snapshot(metadata=metadata)
    raw["mission"]["unreviewed_secret"] = "dropped-extra-field"
    snapshot = validate_owner_snapshot(raw)
    safe = snapshot.mission.metadata

    assert safe["api_key"] == REDACTED
    assert safe["nested"]["authorization"] == REDACTED
    assert safe["nested"]["safe"][0]["password"] == REDACTED
    assert len(safe["nested"]["safe"][0]["label"]) <= (
        MAX_OPEN_STRING_CHARS + len("[TRUNCATED]")
    )
    assert len(safe["many"]) == MAX_OPEN_ITEMS
    assert "unreviewed_secret" not in snapshot.mission.model_dump()
    dumped = snapshot.model_dump_json()
    assert "top-secret" not in dumped
    assert "should-not-leak" not in dumped
    assert "also-secret" not in dumped
    assert "dropped-extra-field" not in dumped


def test_unknown_reconciliation_state_fails_closed():
    with pytest.raises(ValidationError):
        validate_owner_snapshot(owner_snapshot("everything_is_fine"))


@pytest.mark.parametrize("collection", ["tasks", "attempts", "leases", "receipts"])
def test_snapshot_rejects_cross_mission_records(collection):
    raw = owner_snapshot(tasks=[task()], leases=[lease()])
    raw["attempts"] = [
        {
            "attempt_id": "attempt-1",
            "mission_id": "mission-alpha",
            "session_id": "mission:mission-alpha",
            "task_id": "task-1",
            "claim_id": "claim-1",
            "assigned_to": "agent-1",
            "assigned_by": "john",
            "status": "running",
            "failure_code": "",
            "idempotency_key": "attempt-key-1",
            "metadata": {},
            "started_at": "2026-08-25T01:00:00Z",
            "completed_at": None,
        }
    ]
    raw["receipts"] = [
        {
            "receipt_id": "receipt-1",
            "mission_id": "mission-alpha",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "agent_id": "agent-1",
            "receipt_type": "heartbeat",
            "status": "recorded",
            "idempotency_key": "receipt-key-1",
            "payload": {},
            "created_at": "2026-08-25T01:01:00Z",
        }
    ]
    raw[collection][0]["mission_id"] = "mission-beta"
    with pytest.raises(ValidationError, match="cross-mission"):
        validate_owner_snapshot(raw)


def test_snapshot_version_ignores_poll_time_but_tracks_owner_state():
    first = validate_owner_snapshot(owner_snapshot())
    later_raw = owner_snapshot()
    later_raw["observed_at"] = "2026-08-26T00:05:00Z"
    later = validate_owner_snapshot(later_raw)
    changed_raw = owner_snapshot()
    changed_raw["mission"]["status"] = "complete"
    changed = validate_owner_snapshot(changed_raw)

    assert snapshot_version(first) == snapshot_version(later)
    assert snapshot_version(first) != snapshot_version(changed)


@pytest.mark.asyncio
async def test_unavailable_provider_is_honest_configured_only_and_read_only():
    provider = UnavailableMissionProvider(["mission-z", "mission-alpha"])
    assert isinstance(provider, MissionProvider)
    assert provider.configured_mission_ids == ("mission-alpha", "mission-z")
    assert provider.discovery_complete is False
    assert provider.commands == ()

    catalog = await provider.list_missions()
    assert catalog.available is False
    assert catalog.discovery_complete is False
    assert catalog.configured_mission_ids == provider.configured_mission_ids
    assert catalog.missions == ()
    assert catalog.commands == ()
    assert catalog.commands_available is False
    assert catalog.error_code == "provider_unavailable"

    configured = await provider.get_snapshot("mission-alpha")
    assert configured.available is False
    assert configured.error_code == "provider_unavailable"
    assert configured.proves_executor_liveness is False

    unconfigured = await provider.get_snapshot("mission-secret")
    assert unconfigured.available is False
    assert unconfigured.error_code == "mission_not_configured"


@pytest.mark.asyncio
async def test_fake_provider_is_deterministic_and_never_discovers_other_missions():
    alpha = owner_snapshot()
    provider = FakeMissionProvider(
        {"mission-alpha": alpha},
        mission_ids=("mission-missing", "mission-alpha"),
    )
    assert isinstance(provider, MissionProvider)

    first = await provider.list_missions()
    second = await provider.list_missions()
    assert first == second
    assert first.available is True
    assert first.discovery_complete is False
    assert first.configured_mission_ids == ("mission-alpha", "mission-missing")
    assert tuple(summary.mission_id for summary in first.missions) == (
        "mission-alpha",
    )
    assert first.commands == ()

    found = await provider.get_snapshot("mission-alpha")
    assert found.available is True
    assert found.snapshot is not None
    assert found.source_version == snapshot_version(found.snapshot)
    assert found.commands == ()
    assert found.proves_executor_liveness is False

    missing = await provider.get_snapshot("mission-missing")
    assert missing.error_code == "mission_not_found"
    outside = await provider.get_snapshot("mission-outside")
    assert outside.error_code == "mission_not_configured"


def test_fake_provider_rejects_snapshot_outside_configured_scope():
    with pytest.raises(ValueError, match="not configured"):
        FakeMissionProvider(
            {"mission-alpha": owner_snapshot()}, mission_ids=("mission-z",)
        )


@pytest.mark.parametrize(
    "state", sorted(EXPECTED_RECONCILIATION_STATES - {"coherent"})
)
def test_each_noncoherent_reconciliation_state_derives_evidenced_item(state):
    snapshot = validate_owner_snapshot(owner_snapshot(state))
    projection = derive_needs_john(snapshot)

    assert projection.count == 1
    item = projection.items[0]
    assert item.kind == state
    assert f"mission:{snapshot.mission.mission_id}" in item.evidence_refs
    assert f"reconciliation:{state}" in item.evidence_refs
    assert item.source_authority == AUTHORITY
    assert item.allowed_commands == ()
    assert item.commands_available is False
    assert item.proves_executor_liveness is False


def test_coherent_snapshot_has_no_reconciliation_attention_item():
    projection = derive_needs_john(validate_owner_snapshot(owner_snapshot()))
    assert projection.items == ()
    assert projection.count == 0
    assert projection.commands == ()
    assert projection.commands_available is False
    assert projection.proves_executor_liveness is False


def test_expired_lease_item_has_stable_identity_and_specific_evidence():
    raw = owner_snapshot(
        "expired_lease", tasks=[task()], leases=[lease(expired=True)]
    )
    snapshot = validate_owner_snapshot(raw)
    first = derive_needs_john(snapshot, source_version="owner:v1").items[0]

    later_raw = deepcopy(raw)
    later_raw["observed_at"] = "2026-08-26T00:10:00Z"
    later = validate_owner_snapshot(later_raw)
    second = derive_needs_john(later, source_version="owner:v2").items[0]

    assert first.item_id == second.item_id
    assert first.source_version != second.source_version
    assert first.task_id == "task-1"
    assert set(first.evidence_refs) >= {
        "mission:mission-alpha",
        "task:task-1",
        "claim:claim-1",
        "attempt:attempt-1",
    }
    assert first.deadline == datetime(2026, 8, 25, 1, 5, tzinfo=timezone.utc)


def test_active_claim_without_run_accepts_empty_attempt_id():
    snapshot = validate_owner_snapshot(
        owner_snapshot(
            "active_claim_without_run",
            tasks=[task()],
            leases=[lease(expired=False, attempt_id="")],
        )
    )
    projection = derive_needs_john(snapshot)
    assert projection.count == 1
    assert all(ref != "attempt:" for ref in projection.items[0].evidence_refs)


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        ("blocked", "blocked_task"),
        ("quarantined", "quarantined_task"),
        ("input-required", "input_required"),
        ("auth-required", "auth_required"),
    ],
)
def test_task_attention_rules_are_owner_evidenced_and_stable(status, kind):
    snapshot = validate_owner_snapshot(
        owner_snapshot("coherent", tasks=[task(status=status)])
    )
    item = derive_needs_john(snapshot).items[0]
    assert item.kind == kind
    assert item.evidence_refs == ("mission:mission-alpha", "task:task-1")
    assert item.allowed_commands == ()


def test_read_only_models_reject_nonempty_command_advertisements():
    snapshot = validate_owner_snapshot(owner_snapshot("needs_task_projection"))
    item = derive_needs_john(snapshot).items[0]
    raw = item.model_dump()
    raw["allowed_commands"] = ["retry"]
    with pytest.raises(ValidationError):
        NeedsJohnItem.model_validate(raw)
