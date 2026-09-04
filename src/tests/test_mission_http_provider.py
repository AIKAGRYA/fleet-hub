"""HttpMissionProvider: the first real owner adapter, exercised offline.

Every test drives the adapter through ``httpx.MockTransport`` so no network
and no owner process is needed.  The assertions pin the trust posture from
CLAUDE.md items 3, 5, 6 and 7: configured-only discovery, fail-closed
validation, no credential leakage, and no command advertisement.
"""

from __future__ import annotations

import json

import httpx
import pytest

from hub.mission_contract import AUTHORITY, snapshot_version, validate_owner_snapshot
from hub.mission_http_provider import (
    MAX_RESPONSE_BYTES,
    HttpMissionProvider,
    normalize_base_url,
)
from hub.mission_provider import MissionProvider
from tests.test_server_v1 import authed, snapshot

OWNER = "http://owner.invalid:8420"
TOKEN = "owner-fixture-token-not-live"


def owner_envelope(mission_id: str, **kwargs) -> dict:
    body = snapshot(mission_id, **kwargs)
    return {
        "ok": True,
        "schema_version": "dharma.mission_control.v1",
        "mission_id": mission_id,
        "authority": AUTHORITY,
        "proves_executor_liveness": False,
        "observed_at": body["observed_at"],
        "snapshot": body,
        "commands": [],
        "commands_available": False,
    }


class OwnerStub:
    """Scripted owner: records requests, answers per mission id."""

    def __init__(self, responses: dict[str, tuple[int, object]]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        prefix = "/api/mission-control/missions/"
        assert request.url.path.startswith(prefix)
        mission_id = request.url.path[len(prefix) :].removesuffix("/snapshot")
        status, body = self.responses.get(mission_id, (404, {"error_code": "not_found"}))
        if isinstance(body, (bytes, str)):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def provider(stub: OwnerStub, mission_ids=("mission-alpha",)) -> HttpMissionProvider:
    return HttpMissionProvider(OWNER, TOKEN, mission_ids, client=stub.client())


class TestConstruction:
    def test_satisfies_read_only_protocol(self):
        adapter = provider(OwnerStub({}))
        assert isinstance(adapter, MissionProvider)
        assert adapter.discovery_complete is False
        assert adapter.commands == ()
        assert adapter.configured_mission_ids == ("mission-alpha",)

    @pytest.mark.parametrize(
        "url",
        ["", "owner:8420", "ftp://owner", "http://owner/?token=x", "http://owner/#f"],
    )
    def test_rejects_unsafe_base_urls(self, url):
        with pytest.raises(ValueError):
            normalize_base_url(url)

    def test_trailing_slash_is_normalized(self):
        assert normalize_base_url("https://owner.example/ ") == "https://owner.example"

    def test_requires_token(self):
        with pytest.raises(ValueError):
            HttpMissionProvider(OWNER, "  ", ["mission-alpha"])


class TestSnapshot:
    async def test_happy_path_validates_and_versions_owner_snapshot(self):
        stub = OwnerStub({"mission-alpha": (200, owner_envelope("mission-alpha"))})
        projection = await provider(stub).get_snapshot("mission-alpha")
        assert projection.available is True
        assert projection.error_code is None
        assert projection.snapshot is not None
        assert projection.snapshot.mission.mission_id == "mission-alpha"
        expected = validate_owner_snapshot(snapshot("mission-alpha"))
        assert projection.source_version == snapshot_version(expected)
        assert projection.commands == () and projection.commands_available is False
        assert projection.proves_executor_liveness is False

    async def test_sends_bearer_and_only_get(self):
        stub = OwnerStub({"mission-alpha": (200, owner_envelope("mission-alpha"))})
        await provider(stub).get_snapshot("mission-alpha")
        (request,) = stub.requests
        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)

    async def test_unconfigured_mission_never_hits_owner(self):
        stub = OwnerStub({"mission-beta": (200, owner_envelope("mission-beta"))})
        projection = await provider(stub).get_snapshot("mission-beta")
        assert projection.available is False
        assert projection.error_code == "mission_not_configured"
        assert stub.requests == []

    async def test_owner_404_is_mission_not_found(self):
        projection = await provider(OwnerStub({})).get_snapshot("mission-alpha")
        assert projection.error_code == "mission_not_found"
        assert projection.snapshot is None

    @pytest.mark.parametrize(
        "status,body",
        [
            (500, {"error_code": "boom"}),
            (503, {"error_code": "state_not_initialized"}),
            (401, {"error": "unauthorized"}),
            (200, "not json at all"),
            (200, {"snapshot": {"mission": {"mission_id": "mission-alpha"}}}),
            (200, {"no_snapshot_key": True}),
        ],
    )
    async def test_bad_owner_answers_fail_closed(self, status, body):
        stub = OwnerStub({"mission-alpha": (status, body)})
        projection = await provider(stub).get_snapshot("mission-alpha")
        assert projection.available is False
        assert projection.error_code == "provider_unavailable"
        assert projection.snapshot is None

    async def test_forged_liveness_or_authority_is_rejected(self):
        forged = owner_envelope("mission-alpha")
        forged["snapshot"]["proves_executor_liveness"] = True
        stub = OwnerStub({"mission-alpha": (200, forged)})
        projection = await provider(stub).get_snapshot("mission-alpha")
        assert projection.error_code == "provider_unavailable"

    async def test_identity_mismatch_is_rejected(self):
        wrong = owner_envelope("mission-beta")
        stub = OwnerStub({"mission-alpha": (200, wrong)})
        projection = await provider(stub).get_snapshot("mission-alpha")
        assert projection.available is False
        assert projection.error_code == "provider_unavailable"

    async def test_transport_error_is_provider_unavailable(self):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        adapter = HttpMissionProvider(
            OWNER,
            TOKEN,
            ["mission-alpha"],
            client=httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        )
        projection = await adapter.get_snapshot("mission-alpha")
        assert projection.error_code == "provider_unavailable"

    async def test_oversized_owner_body_is_rejected(self):
        padding = "x" * (MAX_RESPONSE_BYTES + 1)
        stub = OwnerStub({"mission-alpha": (200, json.dumps({"pad": padding}))})
        projection = await provider(stub).get_snapshot("mission-alpha")
        assert projection.error_code == "provider_unavailable"


class TestCatalog:
    async def test_lists_configured_missions_only(self):
        stub = OwnerStub(
            {
                "mission-alpha": (200, owner_envelope("mission-alpha")),
                "mission-beta": (200, owner_envelope("mission-beta")),
                "mission-gamma": (200, owner_envelope("mission-gamma")),
            }
        )
        catalog = await provider(stub, ("mission-beta", "mission-alpha")).list_missions()
        assert catalog.available is True
        assert catalog.discovery_complete is False
        assert catalog.configured_mission_ids == ("mission-alpha", "mission-beta")
        assert [m.mission_id for m in catalog.missions] == ["mission-alpha", "mission-beta"]
        assert {r.url.path.split("/")[-2] for r in stub.requests} == {
            "mission-alpha",
            "mission-beta",
        }
        assert catalog.commands_available is False and catalog.commands == ()

    async def test_unknown_configured_mission_is_omitted_not_fatal(self):
        stub = OwnerStub({"mission-alpha": (200, owner_envelope("mission-alpha"))})
        catalog = await provider(stub, ("mission-alpha", "mission-zeta")).list_missions()
        assert catalog.available is True
        assert [m.mission_id for m in catalog.missions] == ["mission-alpha"]

    async def test_owner_down_is_unavailable_not_empty(self):
        stub = OwnerStub({"mission-alpha": (503, {"error_code": "state_not_initialized"})})
        catalog = await provider(stub).list_missions()
        assert catalog.available is False
        assert catalog.error_code == "provider_unavailable"
        assert catalog.missions == ()

    async def test_no_configured_missions_is_available_and_empty(self):
        stub = OwnerStub({})
        catalog = await provider(stub, ()).list_missions()
        assert catalog.available is True and catalog.missions == ()
        assert stub.requests == []


class TestServerWiring:
    def test_phone_routes_render_owner_data_through_http_adapter(self, configured):
        client, server = configured
        authed(client)
        stub = OwnerStub(
            {"mission-alpha": (200, owner_envelope("mission-alpha", task_status="blocked"))}
        )
        server.app.state.mission_provider = provider(stub)
        server.app.state.mission_provider_kind = "owner_http_read_only"

        missions = client.get("/api/v1/missions")
        assert missions.status_code == 200
        body = missions.json()
        assert body["available"] is True
        assert body["missions"][0]["mission_id"] == "mission-alpha"
        assert body["commands_available"] is False

        snapshot_view = client.get("/api/v1/missions/mission-alpha/snapshot")
        assert snapshot_view.status_code == 200
        assert snapshot_view.json()["snapshot"]["mission"]["mission_id"] == "mission-alpha"

        needs = client.get("/api/v1/needs-john")
        assert needs.status_code == 200
        assert needs.json()["available"] is True
        assert needs.json()["count"] >= 1  # blocked task derives an item

        bootstrap = client.get("/api/v1/bootstrap")
        assert bootstrap.status_code == 200
        connections = bootstrap.json()["connections"]
        assert connections["mission_control"] is True
        assert connections["mission_provider_kind"] == "owner_http_read_only"
        assert TOKEN not in bootstrap.text

    def test_env_selection_fails_closed_without_token(self, tmp_path, roster, monkeypatch):
        from tests.conftest import _build_client, TEST_TOKEN

        monkeypatch.setenv("FLEET_HUB_MISSION_IDS", "mission-alpha")
        monkeypatch.setenv("FLEET_HUB_MISSION_PROVIDER_URL", OWNER)
        monkeypatch.delenv("FLEET_HUB_MISSION_PROVIDER_TOKEN", raising=False)
        client, server = _build_client(TEST_TOKEN, tmp_path, roster, monkeypatch)
        try:
            assert server.app.state.mission_provider_kind == "unavailable"
            authed(client)
            assert client.get("/api/v1/missions").status_code == 503
        finally:
            client.close()

    def test_env_selection_builds_http_adapter(self, tmp_path, roster, monkeypatch):
        from tests.conftest import _build_client, TEST_TOKEN

        monkeypatch.setenv("FLEET_HUB_MISSION_IDS", "mission-alpha")
        monkeypatch.setenv("FLEET_HUB_MISSION_PROVIDER_URL", OWNER)
        monkeypatch.setenv("FLEET_HUB_MISSION_PROVIDER_TOKEN", TOKEN)
        client, server = _build_client(TEST_TOKEN, tmp_path, roster, monkeypatch)
        try:
            assert server.app.state.mission_provider_kind == "owner_http_read_only"
            assert isinstance(server.app.state.mission_provider, HttpMissionProvider)
            assert server.app.state.mission_provider.configured_mission_ids == (
                "mission-alpha",
            )
        finally:
            client.close()

    def test_env_selection_rejects_bad_url(self, tmp_path, roster, monkeypatch):
        from tests.conftest import _build_client, TEST_TOKEN

        monkeypatch.setenv("FLEET_HUB_MISSION_IDS", "mission-alpha")
        monkeypatch.setenv("FLEET_HUB_MISSION_PROVIDER_URL", "owner-without-scheme")
        monkeypatch.setenv("FLEET_HUB_MISSION_PROVIDER_TOKEN", TOKEN)
        client, server = _build_client(TEST_TOKEN, tmp_path, roster, monkeypatch)
        try:
            assert server.app.state.mission_provider_kind == "unavailable_misconfigured"
        finally:
            client.close()
