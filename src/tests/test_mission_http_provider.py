"""Authenticated HTTP boundary tests for the Mission Control owner adapter."""

from __future__ import annotations

import json

import httpx
import pytest

from hub.mission_contract import AUTHORITY
from hub.mission_provider import (
    HttpMissionProvider,
    UnavailableMissionProvider,
    mission_provider_from_settings,
)


MISSION_ID = "fleet-advancement-20260826"
OWNER_TOKEN = "test"


class _TrackingAsyncStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.read_started = False

    async def __aiter__(self):
        self.read_started = True
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def _snapshot(mission_id: str = MISSION_ID) -> dict:
    return {
        "mission": {
            "mission_id": mission_id,
            "session_id": f"mission:{mission_id}",
            "title": "Fleet advancement",
            "goal": "Ship a truthful phone helm",
            "operator_id": "john",
            "status": "active",
            "metadata": {},
            "created_at": "2026-08-26T01:00:00Z",
            "updated_at": "2026-08-27T01:00:00Z",
        },
        "tasks": [],
        "attempts": [],
        "leases": [],
        "receipts": [],
        "reconciliation": "coherent",
        "observed_at": "2026-08-27T01:00:00Z",
        "authority": AUTHORITY,
        "proves_executor_liveness": False,
    }


def _envelope(
    *,
    state: str = "observed",
    snapshot: dict | None = None,
    mission_id: str = MISSION_ID,
) -> dict:
    observed = state == "observed"
    return {
        "schema_version": "0.2.0",
        "request_id": "123e4567-e89b-12d3-a456-426614174000",
        "generated_at": "2026-08-27T01:00:00Z",
        "source_errors": []
        if observed
        else [
            {
                "source": "mission_snapshot",
                "error": "canonical state was not observed for this mission",
                "timestamp": "",
            }
        ],
        "freshness_window": "",
        "data": {
            "schema_version": "dharma.control_surface.mission_snapshot_projection.v1",
            "mission_id": mission_id,
            "state": state,
            "authority": AUTHORITY,
            "source_mode": "injected_read_only",
            "runtime_projection_mode": "immutable_copy" if observed else "unavailable",
            "simulation": False,
            "snapshot": (_snapshot(mission_id) if snapshot is None else snapshot)
            if observed
            else None,
            "proves_executor_liveness": False,
        },
    }


def _provider(handler) -> HttpMissionProvider:
    return HttpMissionProvider(
        "http://127.0.0.1:8765",
        OWNER_TOKEN,
        [MISSION_ID],
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_http_provider_sends_exact_bearer_and_projects_valid_owner_snapshot():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_envelope())

    provider = _provider(handler)
    catalog = await provider.list_missions()
    projected = await provider.get_snapshot(MISSION_ID)

    assert catalog.available is True
    assert catalog.configured_mission_ids == (MISSION_ID,)
    assert catalog.missions[0].mission_id == MISSION_ID
    assert projected.available is True
    assert projected.snapshot is not None
    assert projected.snapshot.authority == AUTHORITY
    assert projected.proves_executor_liveness is False
    assert len(requests) == 2
    assert requests[0].url.path == (
        f"/api/control-surface/missions/{MISSION_ID}/snapshot"
    )
    assert requests[0].headers["authorization"] == f"Bearer {OWNER_TOKEN}"
    assert requests[0].headers["accept-encoding"] == "identity"
    assert OWNER_TOKEN not in repr(provider)


@pytest.mark.asyncio
async def test_non_observed_owner_state_fails_closed_as_unavailable():
    provider = _provider(
        lambda request: httpx.Response(200, json=_envelope(state="unknown"))
    )

    projected = await provider.get_snapshot(MISSION_ID)
    catalog = await provider.list_missions()

    assert projected.available is False
    assert projected.error_code == "provider_unavailable"
    assert catalog.available is False
    assert catalog.error_code == "provider_unavailable"
    assert "canonical state" not in projected.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["extra_field", "wrong_identity", "liveness"])
async def test_invalid_owner_contract_never_crosses_boundary(mutation: str):
    body = _envelope()
    if mutation == "extra_field":
        body["unexpected"] = "future-or-forged-field"
    elif mutation == "wrong_identity":
        body["data"]["mission_id"] = "another-mission"
    else:
        body["data"]["proves_executor_liveness"] = True

    provider = _provider(lambda request: httpx.Response(200, json=body))
    projected = await provider.get_snapshot(MISSION_ID)

    assert projected.available is False
    assert projected.error_code == "provider_unavailable"


@pytest.mark.asyncio
async def test_duplicate_json_keys_and_oversize_bodies_fail_closed():
    duplicate = b'{"schema_version":"0.2.0","schema_version":"forged"}'
    duplicate_provider = _provider(
        lambda request: httpx.Response(
            200, content=duplicate, headers={"content-type": "application/json"}
        )
    )
    assert (await duplicate_provider.get_snapshot(MISSION_ID)).available is False

    oversized_provider = HttpMissionProvider(
        "http://localhost:8765",
        OWNER_TOKEN,
        [MISSION_ID],
        max_response_bytes=16 * 1024,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"{" + b"x" * (16 * 1024) + b"}",
                headers={"content-type": "application/json"},
            )
        ),
    )
    assert (await oversized_provider.get_snapshot(MISSION_ID)).available is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_encoding",
    ["gzip", "deflate", "br", "zstd", "gzip, identity", "identity, identity", ""],
)
async def test_encoded_owner_responses_fail_before_body_read(
    content_encoding: str,
):
    stream = _TrackingAsyncStream([b"compressed body must never be read"])
    provider = _provider(
        lambda request: httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-encoding": content_encoding,
            },
            stream=stream,
        )
    )

    projected = await provider.get_snapshot(MISSION_ID)

    assert projected.available is False
    assert projected.error_code == "provider_unavailable"
    assert stream.read_started is False


@pytest.mark.asyncio
async def test_identity_or_unencoded_stream_is_bounded_at_exact_json_byte_limit():
    limit = 16 * 1024
    encoded = json.dumps(_envelope(), separators=(",", ":")).encode("utf-8")
    exact_body = encoded + (b" " * (limit - len(encoded)))

    def provider_for(body: bytes, *, content_encoding: str | None = None):
        headers = {"content-type": "application/json"}
        if content_encoding is not None:
            headers["content-encoding"] = content_encoding
        stream = _TrackingAsyncStream([body[:limit], body[limit:]])
        provider = HttpMissionProvider(
            "http://localhost:8765",
            OWNER_TOKEN,
            [MISSION_ID],
            max_response_bytes=limit,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers=headers, stream=stream)
            ),
        )
        return provider, stream

    exact_provider, exact_stream = provider_for(exact_body, content_encoding="Identity")
    oversized_provider, oversized_stream = provider_for(exact_body + b" ")

    exact = await exact_provider.get_snapshot(MISSION_ID)
    oversized = await oversized_provider.get_snapshot(MISSION_ID)

    assert exact.available is True
    assert exact.snapshot is not None
    assert exact_stream.read_started is True
    assert oversized.available is False
    assert oversized.error_code == "provider_unavailable"
    assert oversized_stream.read_started is True


@pytest.mark.asyncio
async def test_http_errors_redirects_and_unconfigured_ids_do_not_retry():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"location": "https://attacker.invalid"})

    provider = _provider(handler)
    outside = await provider.get_snapshot("another-mission")
    configured = await provider.get_snapshot(MISSION_ID)

    assert outside.error_code == "mission_not_configured"
    assert configured.error_code == "provider_unavailable"
    assert calls == 1


def test_settings_require_one_mission_paired_credentials_and_secure_transport():
    missing_token = mission_provider_from_settings(
        owner_base_url="https://owner.example",
        bearer_token=None,
        mission_ids=[MISSION_ID],
    )
    multiple = mission_provider_from_settings(
        owner_base_url="https://owner.example",
        bearer_token=OWNER_TOKEN,
        mission_ids=[MISSION_ID, "another-mission"],
    )
    cleartext_remote = mission_provider_from_settings(
        owner_base_url="http://owner.example",
        bearer_token=OWNER_TOKEN,
        mission_ids=[MISSION_ID],
    )
    malformed_id = mission_provider_from_settings(
        owner_base_url="https://owner.example",
        bearer_token=OWNER_TOKEN,
        mission_ids=["not/a/mission"],
    )

    assert isinstance(missing_token, UnavailableMissionProvider)
    assert isinstance(multiple, UnavailableMissionProvider)
    assert isinstance(cleartext_remote, UnavailableMissionProvider)
    assert isinstance(malformed_id, UnavailableMissionProvider)
    with pytest.raises(ValueError):
        HttpMissionProvider(
            "https://user:password@owner.example",
            OWNER_TOKEN,
            [MISSION_ID],
        )


def test_server_routes_consume_http_provider_without_owner_file_access(configured):
    client, server = configured
    client.headers["Authorization"] = "Bearer testtoken"
    server.app.state.mission_provider = _provider(
        lambda request: httpx.Response(200, json=_envelope())
    )

    catalog = client.get("/api/v1/missions")
    snapshot = client.get(f"/api/v1/missions/{MISSION_ID}/snapshot")

    assert catalog.status_code == 200
    assert catalog.json()["missions"][0]["mission_id"] == MISSION_ID
    assert snapshot.status_code == 200
    assert snapshot.json()["snapshot"]["authority"] == AUTHORITY
    assert snapshot.json()["proves_executor_liveness"] is False
