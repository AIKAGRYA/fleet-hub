"""Populated, test-only app used by the local browser qualification pass.

Run with a disposable token and ``uvicorn tests.browser_demo:app --app-dir src``.
The production installer excludes ``tests/``; importing :mod:`server` normally
never selects this provider or seeds this state.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import server
from hub.mission_provider import FakeMissionProvider


MISSION_ID = "fleet-hub-browser-fixture"
TASK_ID = "task-browser-fixture"
ATTEMPT_ID = "attempt-browser-fixture"
CLAIM_ID = "claim-browser-fixture"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


SNAPSHOT = {
    "mission": {
        "mission_id": MISSION_ID,
        "session_id": f"mission:{MISSION_ID}",
        "title": "Fleet Hub browser qualification",
        "goal": "Verify the phone projection without touching a production owner.",
        "operator_id": "john",
        "status": "active",
        "metadata": {"fixture": True},
        "created_at": iso(NOW - timedelta(hours=3)),
        "updated_at": iso(NOW - timedelta(minutes=2)),
    },
    "tasks": [
        {
            "task_id": TASK_ID,
            "mission_id": MISSION_ID,
            "title": "Review the blocked owner decision",
            "description": "A deterministic fixture proving Board and Needs John rendering.",
            "status": "running",
            "priority": "high",
            "assigned_to": "agni-hermes",
            "result": "",
            "metadata": {"fixture": True},
            "created_at": iso(NOW - timedelta(hours=2)),
            "updated_at": iso(NOW - timedelta(minutes=3)),
        }
    ],
    "attempts": [
        {
            "attempt_id": ATTEMPT_ID,
            "mission_id": MISSION_ID,
            "session_id": f"mission:{MISSION_ID}",
            "task_id": TASK_ID,
            "claim_id": CLAIM_ID,
            "assigned_to": "agni-hermes",
            "assigned_by": "browser-fixture",
            "status": "running",
            "failure_code": "",
            "idempotency_key": "browser-fixture-attempt",
            "metadata": {"fixture": True},
            "started_at": iso(NOW - timedelta(hours=1)),
            "completed_at": None,
        }
    ],
    "leases": [
        {
            "claim_id": CLAIM_ID,
            "mission_id": MISSION_ID,
            "session_id": f"mission:{MISSION_ID}",
            "task_id": TASK_ID,
            "agent_id": "agni-hermes",
            "attempt_id": ATTEMPT_ID,
            "status": "expired",
            "active": False,
            "expired": True,
            "heartbeat_at": iso(NOW - timedelta(minutes=20)),
            "stale_after": iso(NOW - timedelta(minutes=15)),
            "metadata": {"fixture": True},
        }
    ],
    "receipts": [
        {
            "receipt_id": "receipt-browser-fixture",
            "mission_id": MISSION_ID,
            "task_id": TASK_ID,
            "attempt_id": ATTEMPT_ID,
            "agent_id": "agni-hermes",
            "receipt_type": "attempt_started",
            "status": "recorded",
            "idempotency_key": "browser-fixture-receipt",
            "payload": {"fixture": True, "effect": "unobserved"},
            "created_at": iso(NOW - timedelta(hours=1)),
        }
    ],
    "reconciliation": "expired_lease",
    "observed_at": iso(NOW),
    "authority": "TaskBoard+RuntimeStateStore",
    "proves_executor_liveness": False,
}


server.app.state.mission_provider = FakeMissionProvider({MISSION_ID: SNAPSHOT})
server.app.state.evidence_mode = "fixture"
server.app.state.source_instance = "browser_demo.fixture"
server.app.state.generated_by_fixture = True

# This is fixture transport state, not a live broker claim.  The persistent
# SIMULATION banner is the visual guard that makes the populated shell useful
# for local review without laundering it into production evidence.
server.STATE.connected = True

# Explicitly test-scoped observations. They demonstrate independent labels and
# do not claim a production agent is live.
instant = time.time()
server.STATE.presence["agni-hermes"] = {
    "last_heard": instant - 45,
    "last_heard_source": "browser_demo.fixture",
    "last_heard_verification": "owner_verified",
    "last_addressed": instant - 15,
    "last_addressed_source": "browser_demo.fixture",
    "last_addressed_verification": "test_fixture_only",
}
server.STATE.chat.append(
    {
        "msg_id": "fixture-reported-sender",
        "message_id": "fixture-reported-sender",
        "from": "operator",
        "sender_claim": {
            "value": "operator",
            "status": "reported_unverified",
            "source": "browser_demo.fixture",
            "matched_roster_uid": None,
        },
        "text": "This reported sender must not receive trusted operator styling.",
        "ts": iso(NOW - timedelta(minutes=4)),
    }
)
server.STATE.chat.append(
    {
        "msg_id": "fixture-server-derived-operator",
        "message_id": "fixture-server-derived-operator",
        "from": "operator",
        "sender_claim": {
            "value": "operator",
            "status": "authenticated_server_derived",
            "source": "fleet_hub_session",
            "matched_roster_uid": None,
        },
        "text": "This fixture is a server-derived operator message.",
        "ts": iso(NOW - timedelta(minutes=3)),
    }
)

app = server.app
