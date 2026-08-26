"""Read-only Mission Control provider boundary for Fleet Hub.

The provider is intentionally narrower than the owner API.  It exposes only
mission IDs explicitly configured by the operator, reports discovery as
incomplete, and advertises no mutation commands.  Production wiring can
implement :class:`MissionProvider` over an authenticated owner service without
giving the hub filesystem or SQLite access.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal, Mapping, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from hub.mission_contract import (
    AUTHORITY,
    Identifier,
    MissionSnapshotWire,
    OpaqueVersion,
    ShortText,
    snapshot_version,
    validate_owner_snapshot,
)


MAX_CONFIGURED_MISSIONS = 128
_identifier_adapter = TypeAdapter(Identifier)


class ProjectionModel(BaseModel):
    # A provider can return an already-instantiated Pydantic model.  Re-run the
    # complete schema in that case so ``model_construct()`` cannot smuggle a
    # stronger authority, discovery, or command claim across this boundary.
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )


class MissionSummary(ProjectionModel):
    mission_id: Identifier
    title: ShortText = ""
    goal: ShortText = ""
    status: ShortText
    reconciliation: ShortText
    observed_at: str
    source_version: OpaqueVersion
    authority: Literal["TaskBoard+RuntimeStateStore"] = AUTHORITY
    proves_executor_liveness: Literal[False] = False


class MissionCatalog(ProjectionModel):
    """Configured-only list projection; never claims global discovery."""

    available: bool
    configured_mission_ids: tuple[Identifier, ...] = Field(
        default=(), max_length=MAX_CONFIGURED_MISSIONS
    )
    missions: tuple[MissionSummary, ...] = Field(
        default=(), max_length=MAX_CONFIGURED_MISSIONS
    )
    discovery_complete: Literal[False] = False
    commands: tuple[()] = ()
    commands_available: Literal[False] = False
    authority: Literal["TaskBoard+RuntimeStateStore"] = AUTHORITY
    error_code: Literal["provider_unavailable"] | None = None


class MissionSnapshotProjection(ProjectionModel):
    """Honest result envelope for one configured mission lookup."""

    mission_id: Identifier
    available: bool
    snapshot: MissionSnapshotWire | None = None
    source_version: OpaqueVersion | None = None
    discovery_complete: Literal[False] = False
    commands: tuple[()] = ()
    commands_available: Literal[False] = False
    authority: Literal["TaskBoard+RuntimeStateStore"] = AUTHORITY
    proves_executor_liveness: Literal[False] = False
    error_code: Literal[
        "provider_unavailable", "mission_not_configured", "mission_not_found"
    ] | None = None


@runtime_checkable
class MissionProvider(Protocol):
    """Async, read-only boundary implemented by owner-service adapters."""

    @property
    def configured_mission_ids(self) -> tuple[str, ...]: ...

    @property
    def discovery_complete(self) -> Literal[False]: ...

    @property
    def commands(self) -> tuple[str, ...]: ...

    async def list_missions(self) -> MissionCatalog: ...

    async def get_snapshot(self, mission_id: str) -> MissionSnapshotProjection: ...


def configured_mission_ids(values: Iterable[str]) -> tuple[str, ...]:
    """Validate, deduplicate, and deterministically order configured IDs."""

    result: set[str] = set()
    for value in values:
        result.add(_identifier_adapter.validate_python(value))
        if len(result) > MAX_CONFIGURED_MISSIONS:
            raise ValueError(
                f"at most {MAX_CONFIGURED_MISSIONS} missions may be configured"
            )
    return tuple(sorted(result))


class UnavailableMissionProvider:
    """Safe default when no authenticated owner adapter is configured."""

    discovery_complete: Literal[False] = False
    commands: tuple[str, ...] = ()

    def __init__(self, mission_ids: Iterable[str] = ()) -> None:
        self._mission_ids = configured_mission_ids(mission_ids)

    @property
    def configured_mission_ids(self) -> tuple[str, ...]:
        return self._mission_ids

    async def list_missions(self) -> MissionCatalog:
        return MissionCatalog(
            available=False,
            configured_mission_ids=self._mission_ids,
            missions=(),
            error_code="provider_unavailable",
        )

    async def get_snapshot(self, mission_id: str) -> MissionSnapshotProjection:
        clean_id = _identifier_adapter.validate_python(mission_id)
        error = (
            "mission_not_configured"
            if clean_id not in self._mission_ids
            else "provider_unavailable"
        )
        return MissionSnapshotProjection(
            mission_id=clean_id,
            available=False,
            error_code=error,
        )


class FakeMissionProvider:
    """Deterministic validated provider for route and projection tests.

    It intentionally has no mutation methods.  Supplying a snapshot for an ID
    outside ``mission_ids`` is an error instead of silently expanding the
    configured discovery scope.
    """

    discovery_complete: Literal[False] = False
    commands: tuple[str, ...] = ()

    def __init__(
        self,
        snapshots: Mapping[str, MissionSnapshotWire | Mapping[str, Any]] | None = None,
        *,
        mission_ids: Iterable[str] | None = None,
    ) -> None:
        # Copy caller data; the fake must not change if the test later mutates
        # its source mapping.
        validated: dict[str, MissionSnapshotWire] = {}
        for raw_id, raw_snapshot in (snapshots or {}).items():
            clean_id = _identifier_adapter.validate_python(raw_id)
            snapshot = validate_owner_snapshot(raw_snapshot)
            if snapshot.mission.mission_id != clean_id:
                raise ValueError("snapshot key must match snapshot mission_id")
            validated[clean_id] = snapshot

        configured = configured_mission_ids(
            validated if mission_ids is None else mission_ids
        )
        outside_scope = sorted(set(validated) - set(configured))
        if outside_scope:
            raise ValueError(
                "snapshot IDs are not configured: " + ", ".join(outside_scope)
            )
        self._mission_ids = configured
        self._snapshots = validated

    @property
    def configured_mission_ids(self) -> tuple[str, ...]:
        return self._mission_ids

    async def list_missions(self) -> MissionCatalog:
        summaries: list[MissionSummary] = []
        for mission_id in self._mission_ids:
            snapshot = self._snapshots.get(mission_id)
            if snapshot is None:
                continue
            summaries.append(
                MissionSummary(
                    mission_id=mission_id,
                    title=snapshot.mission.title,
                    goal=snapshot.mission.goal[:1_024],
                    status=snapshot.mission.status,
                    reconciliation=snapshot.reconciliation.value,
                    observed_at=snapshot.observed_at.isoformat(),
                    source_version=snapshot_version(snapshot),
                )
            )
        return MissionCatalog(
            available=True,
            configured_mission_ids=self._mission_ids,
            missions=tuple(summaries),
        )

    async def get_snapshot(self, mission_id: str) -> MissionSnapshotProjection:
        clean_id = _identifier_adapter.validate_python(mission_id)
        if clean_id not in self._mission_ids:
            return MissionSnapshotProjection(
                mission_id=clean_id,
                available=False,
                error_code="mission_not_configured",
            )
        snapshot = self._snapshots.get(clean_id)
        if snapshot is None:
            return MissionSnapshotProjection(
                mission_id=clean_id,
                available=False,
                error_code="mission_not_found",
            )
        return MissionSnapshotProjection(
            mission_id=clean_id,
            available=True,
            snapshot=snapshot,
            source_version=snapshot_version(snapshot),
        )


DEFAULT_MISSION_PROVIDER: MissionProvider = UnavailableMissionProvider()


__all__ = [
    "DEFAULT_MISSION_PROVIDER",
    "FakeMissionProvider",
    "MAX_CONFIGURED_MISSIONS",
    "MissionCatalog",
    "MissionProvider",
    "MissionSnapshotProjection",
    "MissionSummary",
    "UnavailableMissionProvider",
    "configured_mission_ids",
]
