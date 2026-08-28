"""Read-only Mission Control provider boundary for Fleet Hub.

The provider is intentionally narrower than the owner API.  It exposes only
mission IDs explicitly configured by the operator, reports discovery as
incomplete, and advertises no mutation commands.  Production wiring can
implement :class:`MissionProvider` over an authenticated owner service without
giving the hub filesystem or SQLite access.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import (
    Annotated,
    Any,
    Iterable,
    Literal,
    Mapping,
    Protocol,
    runtime_checkable,
)
from urllib.parse import quote, urlsplit

import httpx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

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
MAX_OWNER_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_OWNER_SOURCE_ERRORS = 32
_identifier_adapter = TypeAdapter(Identifier)
_bearer_token = re.compile(r"[A-Za-z0-9._~+/=-]{1,4096}")

_OwnerText = Annotated[str, StringConstraints(max_length=1_024)]


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
    error_code: (
        Literal["provider_unavailable", "mission_not_configured", "mission_not_found"]
        | None
    ) = None


class _OwnerSourceError(ProjectionModel):
    source: _OwnerText
    error: _OwnerText
    timestamp: _OwnerText = ""


class _OwnerSnapshotProjection(ProjectionModel):
    """Exact owner-service projection accepted across the HTTP boundary."""

    schema_version: Literal["dharma.control_surface.mission_snapshot_projection.v1"]
    mission_id: Identifier
    state: Literal["observed", "unknown", "uninitialized"]
    authority: Literal["TaskBoard+RuntimeStateStore"] = AUTHORITY
    source_mode: Literal["injected_read_only"]
    runtime_projection_mode: Literal[
        "immutable_copy", "owner_supplied_read_only", "unavailable"
    ]
    simulation: Literal[False]
    snapshot: MissionSnapshotWire | None
    proves_executor_liveness: Literal[False] = False

    @model_validator(mode="after")
    def state_matches_snapshot(self) -> "_OwnerSnapshotProjection":
        if self.state == "observed":
            if self.snapshot is None:
                raise ValueError("observed projection must contain a snapshot")
            if self.runtime_projection_mode == "unavailable":
                raise ValueError(
                    "observed projection must name a read-only runtime mode"
                )
        elif self.snapshot is not None:
            raise ValueError("non-observed projection cannot contain a snapshot")
        if (
            self.snapshot is not None
            and self.snapshot.mission.mission_id != self.mission_id
        ):
            raise ValueError("owner projection identity mismatch")
        return self


class _OwnerSnapshotEnvelope(ProjectionModel):
    """Exact Control Surface envelope for the single read endpoint."""

    schema_version: Literal["0.2.0"]
    request_id: Identifier
    generated_at: AwareDatetime
    source_errors: tuple[_OwnerSourceError, ...] = Field(
        default=(), max_length=MAX_OWNER_SOURCE_ERRORS
    )
    freshness_window: Literal[""] = ""
    data: _OwnerSnapshotProjection

    @model_validator(mode="after")
    def errors_match_state(self) -> "_OwnerSnapshotEnvelope":
        if self.data.state == "observed" and self.source_errors:
            raise ValueError("observed projection cannot carry source errors")
        if self.data.state != "observed" and not self.source_errors:
            raise ValueError("non-observed projection must carry a source error")
        return self


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


class _OwnerTransportError(RuntimeError):
    """Private sentinel whose message never crosses the Fleet HTTP API."""


def _validated_owner_base_url(value: str) -> str:
    clean = value.strip().rstrip("/")
    if not clean or len(clean) > 2_048 or any(ord(char) < 0x20 for char in clean):
        raise ValueError("owner base URL is invalid")
    parsed = urlsplit(clean)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("owner base URL must use HTTP(S)")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("owner base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("owner base URL must not contain a query or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("owner base URL port is invalid") from exc
    if parsed.scheme == "http":
        host = parsed.hostname.casefold()
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise ValueError("cleartext owner transport is allowed only on loopback")
    return clean


def _validated_bearer_token(value: str) -> str:
    clean = value.strip()
    if _bearer_token.fullmatch(clean) is None:
        raise ValueError("owner bearer credential is invalid")
    return clean


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON object key")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=object_without_duplicates,
        parse_constant=reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("owner response must be a JSON object")
    return value


class HttpMissionProvider:
    """Bounded authenticated adapter for the owner Mission Control API.

    This adapter can address only the one mission explicitly selected on both
    sides of the boundary.  It never follows redirects, consults proxy
    environment variables, retries, opens owner files, or exposes upstream
    error text.  Any transport or contract failure becomes the same typed
    unavailable result.
    """

    discovery_complete: Literal[False] = False
    commands: tuple[str, ...] = ()

    def __init__(
        self,
        owner_base_url: str,
        bearer_token: str,
        mission_ids: Iterable[str],
        *,
        timeout_s: float = 2.0,
        max_response_bytes: int = MAX_OWNER_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        configured = configured_mission_ids(mission_ids)
        if len(configured) != 1:
            raise ValueError("HTTP owner transport requires exactly one mission ID")
        if not isinstance(timeout_s, (int, float)) or not 0.1 <= timeout_s <= 5.0:
            raise ValueError("owner timeout must be between 0.1 and 5 seconds")
        if not 16 * 1024 <= max_response_bytes <= 8 * 1024 * 1024:
            raise ValueError("owner response limit is outside the safe range")
        self._owner_base_url = _validated_owner_base_url(owner_base_url)
        self._bearer_token = _validated_bearer_token(bearer_token)
        self._mission_ids = configured
        self._timeout_s = float(timeout_s)
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    @property
    def configured_mission_ids(self) -> tuple[str, ...]:
        return self._mission_ids

    async def _request_snapshot(self, mission_id: str) -> _OwnerSnapshotEnvelope:
        encoded_id = quote(mission_id, safe="")
        url = (
            f"{self._owner_base_url}/api/control-surface/missions/{encoded_id}/snapshot"
        )
        timeout = httpx.Timeout(self._timeout_s)
        headers = {
            "Accept": "application/json",
            # The response limit is a security boundary, so do not negotiate a
            # representation that httpx would expand before the bounded read.
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {self._bearer_token}",
            "User-Agent": "fleet-hub-owner-reader/1",
        }
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            transport=self._transport,
            trust_env=False,
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code != 200:
                    raise _OwnerTransportError("owner request was not successful")
                content_type = response.headers.get("content-type", "")
                if (
                    content_type.split(";", 1)[0].strip().casefold()
                    != "application/json"
                ):
                    raise _OwnerTransportError("owner response was not JSON")
                content_encodings = response.headers.get_list("content-encoding")
                if content_encodings and (
                    len(content_encodings) != 1
                    or content_encodings[0].strip().casefold() != "identity"
                ):
                    # Inspect this before iterating: ``aiter_bytes()`` performs
                    # transparent decompression and could otherwise expand a
                    # small wire body beyond the configured memory bound.
                    raise _OwnerTransportError(
                        "owner response content encoding was not supported"
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise _OwnerTransportError(
                            "owner response length was invalid"
                        ) from exc
                    if (
                        declared_length < 0
                        or declared_length > self._max_response_bytes
                    ):
                        raise _OwnerTransportError("owner response was too large")
                body = bytearray()
                # With only absent/identity Content-Encoding accepted,
                # ``aiter_bytes()`` cannot transparently expand the body.
                # Bound the JSON bytes before decoding or schema validation;
                # this also supports already-buffered test transports.
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise _OwnerTransportError("owner response was too large")
        try:
            decoded = _strict_json_object(bytes(body))
            return _OwnerSnapshotEnvelope.model_validate(decoded)
        except Exception as exc:
            raise _OwnerTransportError("owner response contract was invalid") from exc

    async def get_snapshot(self, mission_id: str) -> MissionSnapshotProjection:
        clean_id = _identifier_adapter.validate_python(mission_id)
        if clean_id not in self._mission_ids:
            return MissionSnapshotProjection(
                mission_id=clean_id,
                available=False,
                error_code="mission_not_configured",
            )
        try:
            envelope = await self._request_snapshot(clean_id)
            if envelope.data.mission_id != clean_id:
                raise _OwnerTransportError("owner projection identity mismatch")
            if envelope.data.state != "observed" or envelope.data.snapshot is None:
                raise _OwnerTransportError("owner state was not observed")
            snapshot = validate_owner_snapshot(envelope.data.snapshot)
        except Exception:
            return MissionSnapshotProjection(
                mission_id=clean_id,
                available=False,
                error_code="provider_unavailable",
            )
        return MissionSnapshotProjection(
            mission_id=clean_id,
            available=True,
            snapshot=snapshot,
            source_version=snapshot_version(snapshot),
        )

    async def list_missions(self) -> MissionCatalog:
        mission_id = self._mission_ids[0]
        projection = await self.get_snapshot(mission_id)
        if not projection.available or projection.snapshot is None:
            return MissionCatalog(
                available=False,
                configured_mission_ids=self._mission_ids,
                missions=(),
                error_code="provider_unavailable",
            )
        snapshot = projection.snapshot
        return MissionCatalog(
            available=True,
            configured_mission_ids=self._mission_ids,
            missions=(
                MissionSummary(
                    mission_id=mission_id,
                    title=snapshot.mission.title,
                    goal=snapshot.mission.goal[:1_024],
                    status=snapshot.mission.status,
                    reconciliation=snapshot.reconciliation.value,
                    observed_at=snapshot.observed_at.isoformat(),
                    source_version=projection.source_version
                    or snapshot_version(snapshot),
                ),
            ),
        )


def mission_provider_from_settings(
    *,
    owner_base_url: str | None,
    bearer_token: str | None,
    mission_ids: Iterable[str],
    timeout_s: float = 2.0,
    max_response_bytes: int = MAX_OWNER_RESPONSE_BYTES,
) -> MissionProvider:
    """Construct the explicit HTTP adapter or a typed unavailable provider."""

    try:
        configured = configured_mission_ids(mission_ids)
    except (TypeError, ValueError):
        return DEFAULT_MISSION_PROVIDER
    if not owner_base_url or not bearer_token:
        return UnavailableMissionProvider(configured)
    try:
        return HttpMissionProvider(
            owner_base_url,
            bearer_token,
            configured,
            timeout_s=min(float(timeout_s), 5.0),
            max_response_bytes=max_response_bytes,
        )
    except (TypeError, ValueError):
        return UnavailableMissionProvider(configured)


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
    "HttpMissionProvider",
    "MAX_CONFIGURED_MISSIONS",
    "MAX_OWNER_RESPONSE_BYTES",
    "MissionCatalog",
    "MissionProvider",
    "MissionSnapshotProjection",
    "MissionSummary",
    "UnavailableMissionProvider",
    "configured_mission_ids",
    "mission_provider_from_settings",
]
