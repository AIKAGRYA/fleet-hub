"""Authenticated, read-only HTTP Mission Control provider.

This is the first real owner adapter behind :class:`hub.mission_provider.MissionProvider`.
It talks to the canonical owner's HTTP projection (``dharma_swarm``
``api/routers/mission_control.py``: ``GET /api/mission-control/missions/{id}/snapshot``)
and hands every response body to :func:`hub.mission_contract.validate_owner_snapshot`.

Trust posture, unchanged from the boundary it implements:

- Fleet Hub never opens an owner database; it receives a JSON document and
  validates the bounded public shape.  A body that fails validation, names a
  different mission, or arrives late is reported as ``provider_unavailable``
  or ``mission_not_found`` — never rendered.
- Discovery is configured-only.  The provider only ever asks the owner about
  mission IDs the operator listed (``FLEET_HUB_MISSION_IDS``); it does not
  enumerate the owner and does not widen scope from anything the owner says.
- The bearer credential lives in process memory only.  It is never logged,
  never placed in a URL, and never included in an error payload.
- Nothing here can mutate owner state: the adapter issues ``GET`` only and
  advertises ``commands=()``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Iterable, Literal

import httpx
from pydantic import ValidationError

from hub.mission_contract import (
    MissionSnapshotWire,
    snapshot_version,
    validate_owner_snapshot,
)
from hub.mission_provider import (
    MissionCatalog,
    MissionSnapshotProjection,
    MissionSummary,
    configured_mission_ids,
)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CONCURRENT_SNAPSHOT_FETCHES = 8
_BASE_URL = re.compile(r"^https?://[^\s/?#]+(?:/[^\s?#]*)?$")


def normalize_base_url(value: str) -> str:
    """Validate an operator-supplied owner base URL (scheme + host, no query)."""

    clean = (value or "").strip().rstrip("/")
    if not _BASE_URL.fullmatch(clean):
        raise ValueError("owner base URL must be an absolute http(s) URL without query")
    return clean


class HttpMissionProvider:
    """Read-only owner adapter over the Mission Control HTTP projection."""

    discovery_complete: Literal[False] = False
    commands: tuple[str, ...] = ()

    def __init__(
        self,
        base_url: str,
        token: str,
        mission_ids: Iterable[str],
        *,
        timeout_s: float = 2.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = normalize_base_url(base_url)
        token = (token or "").strip()
        if not token:
            raise ValueError("owner bearer token is required")
        self._mission_ids = configured_mission_ids(mission_ids)
        self._timeout_s = max(0.1, float(timeout_s))
        self._client = client
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    # --- MissionProvider protocol -------------------------------------------

    @property
    def configured_mission_ids(self) -> tuple[str, ...]:
        return self._mission_ids

    async def list_missions(self) -> MissionCatalog:
        if not self._mission_ids:
            return MissionCatalog(
                available=True, configured_mission_ids=(), missions=()
            )
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_SNAPSHOT_FETCHES)

        async def fetch(mission_id: str) -> MissionSnapshotProjection:
            async with semaphore:
                return await self.get_snapshot(mission_id)

        projections = await asyncio.gather(
            *(fetch(mission_id) for mission_id in self._mission_ids)
        )
        summaries: list[MissionSummary] = []
        unavailable = 0
        for projection in projections:
            if projection.error_code == "provider_unavailable":
                unavailable += 1
                continue
            snapshot = projection.snapshot
            if snapshot is None:
                continue  # mission_not_found: configured, owner does not know it
            summaries.append(
                MissionSummary(
                    mission_id=projection.mission_id,
                    title=snapshot.mission.title,
                    goal=snapshot.mission.goal[:1_024],
                    status=snapshot.mission.status,
                    reconciliation=snapshot.reconciliation.value,
                    observed_at=snapshot.observed_at.isoformat(),
                    source_version=snapshot_version(snapshot),
                )
            )
        if unavailable and not summaries:
            # The owner could not be reached for anything: say so rather than
            # rendering an empty board that looks like "no work".
            return MissionCatalog(
                available=False,
                configured_mission_ids=self._mission_ids,
                missions=(),
                error_code="provider_unavailable",
            )
        return MissionCatalog(
            available=True,
            configured_mission_ids=self._mission_ids,
            missions=tuple(summaries),
        )

    async def get_snapshot(self, mission_id: str) -> MissionSnapshotProjection:
        clean_id = configured_mission_ids([mission_id])[0]
        if clean_id not in self._mission_ids:
            return MissionSnapshotProjection(
                mission_id=clean_id,
                available=False,
                error_code="mission_not_configured",
            )
        try:
            status, body = await self._get_json(
                f"/api/mission-control/missions/{clean_id}/snapshot"
            )
        except Exception:
            return self._unavailable(clean_id)
        if status == 404:
            return MissionSnapshotProjection(
                mission_id=clean_id,
                available=False,
                error_code="mission_not_found",
            )
        if status != 200 or not isinstance(body, dict):
            return self._unavailable(clean_id)
        raw_snapshot = body.get("snapshot")
        try:
            snapshot: MissionSnapshotWire = validate_owner_snapshot(raw_snapshot)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable(clean_id)
        if snapshot.mission.mission_id != clean_id:
            # The owner answered about a different mission.  Fail closed: an
            # identity mismatch is not a rendering decision the phone may make.
            return self._unavailable(clean_id)
        return MissionSnapshotProjection(
            mission_id=clean_id,
            available=True,
            snapshot=snapshot,
            source_version=snapshot_version(snapshot),
        )

    # --- transport ------------------------------------------------------------

    async def _get_json(self, path: str) -> tuple[int, Any]:
        url = self._base_url + path
        if self._client is not None:
            response = await self._client.get(
                url, headers=self._headers, timeout=self._timeout_s
            )
            return self._decode(response)
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            response = await client.get(url, headers=self._headers)
            return self._decode(response)

    @staticmethod
    def _decode(response: httpx.Response) -> tuple[int, Any]:
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ValueError("owner response too large")
        if response.status_code == 404:
            return 404, None
        return response.status_code, response.json()

    @staticmethod
    def _unavailable(mission_id: str) -> MissionSnapshotProjection:
        return MissionSnapshotProjection(
            mission_id=mission_id,
            available=False,
            error_code="provider_unavailable",
        )


__all__ = ["HttpMissionProvider", "MAX_RESPONSE_BYTES", "normalize_base_url"]
