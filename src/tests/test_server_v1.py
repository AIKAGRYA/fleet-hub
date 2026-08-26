"""Focused v1 route contracts: projection honesty and idempotent chat."""
from __future__ import annotations

import asyncio

from hub.mission_contract import snapshot_version, validate_owner_snapshot
from hub.mission_provider import (
    FakeMissionProvider,
    MissionSnapshotProjection,
)
from tests.conftest import FakeJS, FakeNC

TOKEN = "testtoken"


def authed(client) -> dict[str, str]:
    login = client.post("/login", json={"token": TOKEN})
    assert login.status_code == 200
    headers = {
        "X-CSRF-Token": login.json()["csrf_token"],
        "Origin": "http://testserver",
        "Sec-Fetch-Site": "same-origin",
    }
    client.headers.update(headers)
    return headers


def snapshot(mission_id: str, *, task_status: str = "running") -> dict:
    return {
        "mission": {
            "mission_id": mission_id,
            "session_id": f"mission:{mission_id}",
            "title": mission_id.replace("-", " ").title(),
            "goal": "Ship an honest phone projection",
            "operator_id": "john",
            "status": "active",
            "metadata": {},
            "created_at": "2026-08-25T00:00:00Z",
            "updated_at": "2026-08-26T00:00:00Z",
        },
        "tasks": [
            {
                "task_id": f"task-{mission_id}",
                "mission_id": mission_id,
                "title": "One task",
                "description": "",
                "status": task_status,
                "priority": "normal",
                "assigned_to": "agent-1",
                "result": "",
                "metadata": {},
                "created_at": "2026-08-25T01:00:00Z",
                "updated_at": "2026-08-26T01:00:00Z",
            }
        ],
        "attempts": [],
        "leases": [],
        "receipts": [],
        "reconciliation": "coherent",
        "observed_at": "2026-08-26T00:00:00Z",
        "authority": "TaskBoard+RuntimeStateStore",
        "proves_executor_liveness": False,
    }


class TestProjectionAvailability:
    def test_bootstrap_keeps_shell_available_but_marks_owner_unavailable(
        self, configured
    ):
        client, _ = configured
        authed(client)
        response = client.get("/api/v1/bootstrap")
        assert response.status_code == 200
        body = response.json()
        assert body["available"] is True
        assert body["build_status"] == "candidate-unqualified"
        assert body["qualified"] is False
        assert body["evidence_mode"] == "local_integration"
        assert body["source_instance"] == "fleet-hub-candidate"
        assert body["generated_by_fixture"] is False
        assert body["process_local"] is True
        assert body["durable_event_resume"] is False
        assert body["missions"]["available"] is False
        assert body["missions"]["error_code"] == "provider_unavailable"
        assert body["needs_john"]["available"] is False
        assert body["capabilities"]["mission_commands"] == {
            "available": False,
            "commands": [],
        }
        assert body["capabilities"]["chat"]["group_fanout"] is False

    def test_unavailable_owner_reads_are_503_not_empty_success(self, configured):
        client, _ = configured
        authed(client)
        for path in (
            "/api/v1/missions",
            "/api/v1/missions/mission-alpha/snapshot",
            "/api/v1/needs-john",
            "/api/kanban",
        ):
            response = client.get(path)
            assert response.status_code == 503, path
            assert response.json()["available"] is False, path

    def test_fake_owner_projection_snapshot_needs_john_and_pagination(
        self, configured
    ):
        client, server = configured
        authed(client)
        alpha = snapshot("mission-alpha", task_status="blocked")
        beta = snapshot("mission-beta")
        server.app.state.mission_provider = FakeMissionProvider(
            {"mission-alpha": alpha, "mission-beta": beta}
        )

        first = client.get("/api/v1/missions", params={"limit": 1})
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["available"] is True
        assert first_body["discovery_complete"] is False
        assert first_body["count"] == 1
        assert first_body["next_cursor"]

        second = client.get(
            "/api/v1/missions",
            params={"limit": 1, "cursor": first_body["next_cursor"]},
        )
        assert second.status_code == 200
        assert second.json()["missions"][0]["mission_id"] == "mission-beta"
        assert second.json()["next_cursor"] is None

        tampered = first_body["next_cursor"][:-1] + "A"
        assert client.get(
            "/api/v1/missions", params={"cursor": tampered}
        ).status_code == 400

        projected = client.get("/api/v1/missions/mission-alpha/snapshot")
        assert projected.status_code == 200
        projected_body = projected.json()
        assert projected_body["source"] == "TaskBoard+RuntimeStateStore"
        assert projected_body["snapshot"]["proves_executor_liveness"] is False
        assert projected_body["capabilities"]["commands_available"] is False

        needs = client.get(
            "/api/v1/needs-john", params={"mission_id": "mission-alpha"}
        )
        assert needs.status_code == 200
        needs_body = needs.json()
        assert needs_body["available"] is True
        assert needs_body["total"] == 1
        assert needs_body["items"][0]["kind"] == "blocked_task"
        assert needs_body["items"][0]["allowed_commands"] == []
        assert needs_body["proves_executor_liveness"] is False

    def test_configured_missing_snapshot_is_404_not_null_success(self, configured):
        client, server = configured
        authed(client)
        server.app.state.mission_provider = FakeMissionProvider(
            {}, mission_ids=("mission-alpha",)
        )
        response = client.get("/api/v1/missions/mission-alpha/snapshot")
        assert response.status_code == 404
        assert response.json()["error_code"] == "mission_not_found"
        assert response.json()["available"] is False
        assert response.text != "null"
        kanban = client.get("/api/kanban")
        assert kanban.status_code == 503
        assert kanban.json()["available"] is False
        assert kanban.json()["error_code"] == "mission_snapshot_unavailable"

    def test_provider_exception_is_redacted(self, configured):
        client, server = configured
        authed(client)

        class BrokenProvider:
            configured_mission_ids = ("mission-alpha",)
            discovery_complete = False
            commands = ()

            async def list_missions(self):
                raise RuntimeError("secret=/root/owner.db token=hunter2")

            async def get_snapshot(self, mission_id):
                del mission_id
                raise RuntimeError("secret=/root/owner.db token=hunter2")

        server.app.state.mission_provider = BrokenProvider()
        response = client.get("/api/v1/missions")
        assert response.status_code == 503
        assert "hunter2" not in response.text
        assert "/root/owner.db" not in response.text

    def test_snapshot_route_rejects_provider_cross_mission_binding(self, configured):
        client, server = configured
        authed(client)
        good = FakeMissionProvider({"mission-alpha": snapshot("mission-alpha")})
        wrong = validate_owner_snapshot(snapshot("mission-beta"))

        class CrossMissionProvider:
            configured_mission_ids = ("mission-alpha",)
            discovery_complete = False
            commands = ()

            async def list_missions(self):
                return await good.list_missions()

            async def get_snapshot(self, mission_id):
                assert mission_id == "mission-alpha"
                return MissionSnapshotProjection(
                    mission_id="mission-alpha",
                    available=True,
                    snapshot=wrong,
                    source_version=snapshot_version(wrong),
                )

        server.app.state.mission_provider = CrossMissionProvider()
        for path in (
            "/api/v1/missions/mission-alpha/snapshot",
            "/api/v1/needs-john?mission_id=mission-alpha",
            "/api/kanban",
        ):
            response = client.get(path)
            assert response.status_code == 503
            assert response.json()["error_code"] == "mission_snapshot_identity_mismatch"
            assert "mission-beta" not in response.text

    def test_provider_timeout_is_stable_and_redacted(self, configured, monkeypatch):
        client, server = configured
        authed(client)
        good = FakeMissionProvider({"mission-alpha": snapshot("mission-alpha")})

        class SlowProvider:
            configured_mission_ids = ("mission-alpha",)
            discovery_complete = False
            commands = ()

            async def list_missions(self):
                return await good.list_missions()

            async def get_snapshot(self, mission_id):
                del mission_id
                await asyncio.sleep(60)
                raise RuntimeError("owner secret should never surface")

        monkeypatch.setattr(server, "MISSION_PROVIDER_TIMEOUT_S", 0.01)
        server.app.state.mission_provider = SlowProvider()
        response = client.get("/api/v1/missions/mission-alpha/snapshot")
        assert response.status_code == 503
        assert response.json()["error_code"] == "mission_provider_unavailable"
        assert "secret" not in response.text

    def test_stream_info_timeout_is_bounded_and_redacted(self, configured, monkeypatch):
        _, server = configured

        class SlowJetStream:
            async def stream_info(self, stream):
                del stream
                await asyncio.sleep(60)
                raise RuntimeError("broker secret should never surface")

        monkeypatch.setattr(server, "BROKER_TIMEOUT_S", 0.01)
        server.STATE.js = SlowJetStream()
        result = asyncio.run(server._stream_info())
        assert result["error"].startswith("stream_info_unavailable:")
        assert "secret" not in str(result)

    def test_commands_are_explicitly_unavailable(self, configured):
        client, _ = configured
        authed(client)
        for path in (
            "/api/v1/missions/mission-alpha/commands",
            "/api/v1/needs-john/needs_john_0123456789abcdef01234567/commands",
        ):
            response = client.post(path, json={"command": "retry"})
            assert response.status_code == 503
            assert response.json()["available"] is False
            assert response.json()["commands"] == []


class TestV1ChatIntent:
    def test_requires_valid_idempotency_key(self, configured):
        client, _ = configured
        authed(client)
        missing = client.post("/api/v1/intents/chat", json={"text": "hello"})
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == "invalid_idempotency_key"

    def test_nats_unavailable_is_503_and_retryable(self, configured):
        client, server = configured
        authed(client)
        payload = {"text": "hello"}
        headers = {"Idempotency-Key": "chat-down-1"}
        first = client.post("/api/v1/intents/chat", json=payload, headers=headers)
        second = client.post("/api/v1/intents/chat", json=payload, headers=headers)
        assert first.status_code == second.status_code == 503
        assert first.json()["accepted"] is False
        assert first.json()["transport_claim"] == "unaccepted"
        assert second.json()["idempotency_reused"] is False
        assert server.STATE.idempotency is not None

    def test_one_publish_is_reused_and_uses_broker_dedupe(self, configured):
        client, server = configured
        authed(client)
        fake_js = FakeJS()
        server.STATE.nc = FakeNC()
        server.STATE.js = fake_js
        payload = {"text": "hello fleet", "msg_id": "chat-retry-1"}
        headers = {"Idempotency-Key": "chat-retry-1"}

        first = client.post("/api/v1/intents/chat", json=payload, headers=headers)
        second = client.post("/api/v1/intents/chat", json=payload, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json()["accepted"] is True
        assert first.json()["ack_tier"] == "PUBLISH_ACCEPTED"
        assert first.json()["semantic_effect"] == "unobserved"
        assert first.json()["route_plan"]["mode"] == "group_transcript"
        assert first.json()["route_plan"]["fanout"] is False
        assert first.json()["idempotency_reused"] is False
        assert second.json()["idempotency_reused"] is True
        assert second.json()["message_id"] == first.json()["message_id"]
        assert second.json()["correlation_id"] == first.json()["correlation_id"]
        assert second.json()["trace_id"] == first.json()["trace_id"]
        assert len(fake_js.published) == 1
        broker_id = fake_js.published_headers[0]["Nats-Msg-Id"]
        assert broker_id.startswith("fh1-")
        assert "chat-retry-1" not in broker_id
        assert first.json()["message_id"] != broker_id
        assert first.json()["message_id"] != "chat-retry-1"

    def test_caller_message_id_is_input_not_broker_dedupe_identity(self, configured):
        client, server = configured
        authed(client)
        server.STATE.nc = FakeNC()
        server.STATE.js = FakeJS()
        response = client.post(
            "/api/v1/intents/chat",
            json={"text": "hello", "msg_id": "message-one"},
            headers={"Idempotency-Key": "message-two"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["client_message_id"] == "message-one"
        assert body["message_id"] != "message-one"
        broker_id = server.STATE.js.published_headers[0]["Nats-Msg-Id"]
        assert broker_id not in {"message-one", "message-two", body["message_id"]}

    def test_key_reuse_with_changed_body_is_conflict(self, configured):
        client, server = configured
        authed(client)
        server.STATE.nc = FakeNC()
        server.STATE.js = FakeJS()
        headers = {"Idempotency-Key": "chat-conflict-1"}
        assert client.post(
            "/api/v1/intents/chat", json={"text": "first"}, headers=headers
        ).status_code == 200
        changed = client.post(
            "/api/v1/intents/chat", json={"text": "second"}, headers=headers
        )
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "idempotency_conflict"

    def test_invalid_caller_supplied_id_is_bounded_validation_error(self, configured):
        client, _ = configured
        authed(client)
        response = client.post(
            "/api/v1/intents/chat",
            json={"text": "hello", "msg_id": "not allowed spaces"},
            headers={"Idempotency-Key": "valid-key"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"
        assert "not allowed spaces" not in response.text

    def test_same_key_isolated_between_authenticated_sessions(self, configured):
        from fastapi.testclient import TestClient

        first_client, server = configured
        authed(first_client)
        second_client = TestClient(server.app, raise_server_exceptions=False)
        try:
            authed(second_client)
            fake_js = FakeJS()
            server.STATE.nc = FakeNC()
            server.STATE.js = fake_js
            payload = {"text": "same body"}
            headers = {"Idempotency-Key": "shared-key"}

            first = first_client.post(
                "/api/v1/intents/chat", json=payload, headers=headers
            )
            retry = first_client.post(
                "/api/v1/intents/chat", json=payload, headers=headers
            )
            second = second_client.post(
                "/api/v1/intents/chat", json=payload, headers=headers
            )

            assert first.status_code == retry.status_code == second.status_code == 200
            assert retry.json()["idempotency_reused"] is True
            assert second.json()["idempotency_reused"] is False
            assert len(fake_js.published) == 2
            broker_ids = [row["Nats-Msg-Id"] for row in fake_js.published_headers]
            assert broker_ids[0] != broker_ids[1]
            assert first.json()["message_id"] != second.json()["message_id"]
            assert all("shared-key" not in value for value in broker_ids)
        finally:
            second_client.close()

    def test_duplicate_puback_has_distinct_honest_transport_claim(self, configured):
        client, server = configured
        authed(client)
        server.STATE.nc = FakeNC()
        server.STATE.js = FakeJS(duplicate=True)
        response = client.post(
            "/api/v1/intents/chat",
            json={"text": "already attempted"},
            headers={"Idempotency-Key": "prior-process-key"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] is True
        assert body["ack_tier"] == "DEDUPLICATED_UNVERIFIED"
        assert body["transport_claim"] == "broker_deduplicated_body_unverified"
        assert body["new_storage_event"] is False
        assert body["current_body_stored"] is None
        assert body["dedupe_scope"].startswith("app_deployment+")
        assert not server.STATE.chat
