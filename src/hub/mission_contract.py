"""Fail-closed wire contract for the Mission Control read projection.

This module deliberately does not import :mod:`dharma_swarm` or open an owner
database.  Fleet Hub receives an owner-produced JSON document, validates the
small public shape below, drops unknown top-level fields, and recursively
redacts/bounds the two intentionally open JSON fields (``metadata`` and
``payload``).

The contract is a projection, not evidence that an executor is running.  That
claim is represented as the literal value ``False`` so an upstream ``true``
value fails validation instead of being relayed to the phone.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)


AUTHORITY = "TaskBoard+RuntimeStateStore"
REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
UNSUPPORTED = "[UNSUPPORTED]"

# Limits are enforced independently for each open JSON field.  The node budget
# is important: depth and per-container limits alone still permit exponential
# expansion.
MAX_OPEN_DEPTH = 6
MAX_OPEN_NODES = 512
MAX_OPEN_ITEMS = 64
MAX_OPEN_KEY_CHARS = 128
MAX_OPEN_STRING_CHARS = 2_048
MAX_SNAPSHOT_RECORDS = 500


JsonScalar: TypeAlias = None | bool | int | float | str
# Pydantic's named recursive JSON alias avoids Python 3.11's implicit-recursive
# TypeAlias expansion loop while still validating every sanitized leaf.
SafeJson: TypeAlias = JsonValue

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]
ShortText = Annotated[str, StringConstraints(max_length=1_024)]
LongText = Annotated[str, StringConstraints(max_length=8_192)]
OpaqueVersion = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
]


class ReconciliationState(StrEnum):
    """All states exported by the canonical Mission Control v1 owner."""

    COHERENT = "coherent"
    NEEDS_TASK_PROJECTION = "needs_task_projection"
    MISSING_TERMINAL_RECEIPT = "missing_terminal_receipt"
    CONFLICTING_ACTIVE_CLAIMS = "conflicting_active_claims"
    ACTIVE_CLAIM_WITHOUT_RUN = "active_claim_without_run"
    EXPIRED_LEASE = "expired_lease"
    EVIDENCE_SCAN_SATURATED = "evidence_scan_saturated"
    FOREIGN_RUNTIME_RECORD = "foreign_runtime_record"
    CONFLICTING_TERMINAL_EVIDENCE = "conflicting_terminal_evidence"


_SENSITIVE_PARTS = frozenset(
    {
        "apikey",
        "authorization",
        "bearer",
        "clientsecret",
        "cookie",
        "credential",
        "passwd",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "sessioncookie",
        "token",
    }
)


def _sensitive_key(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", key.casefold())
    parts = tuple(part for part in re.split(r"[^a-z0-9]+", key.casefold()) if part)
    sensitive_suffixes = (
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "password",
        "privatekey",
        "secret",
        "token",
    )
    return (
        compact in _SENSITIVE_PARTS
        or any(part in _SENSITIVE_PARTS for part in parts)
        or any(compact.endswith(suffix) for suffix in sensitive_suffixes)
    )


def redact_bounded_json(value: Any) -> SafeJson:
    """Return a JSON-safe, recursively redacted and resource-bounded value.

    The function never stringifies arbitrary objects: doing so can both invoke
    attacker-controlled code and leak an object's repr.  Containers retain at
    most ``MAX_OPEN_ITEMS`` entries and the whole traversal retains at most
    ``MAX_OPEN_NODES`` nodes.
    """

    budget = {"nodes": 0}

    def visit(current: Any, depth: int, *, key: str | None = None) -> SafeJson:
        budget["nodes"] += 1
        if budget["nodes"] > MAX_OPEN_NODES:
            return TRUNCATED
        if key is not None and _sensitive_key(key):
            return REDACTED
        if depth > MAX_OPEN_DEPTH:
            return TRUNCATED
        if current is None or isinstance(current, bool):
            return current
        if isinstance(current, int):
            return current
        if isinstance(current, float):
            return current if math.isfinite(current) else UNSUPPORTED
        if isinstance(current, str):
            if len(current) <= MAX_OPEN_STRING_CHARS:
                return current
            return current[:MAX_OPEN_STRING_CHARS] + TRUNCATED
        if isinstance(current, (list, tuple)):
            return [visit(item, depth + 1) for item in current[:MAX_OPEN_ITEMS]]
        if isinstance(current, dict):
            # JSON keys must be strings.  Iterating only the first bounded
            # window avoids doing unbounded sorting/work at this trust boundary.
            selected: list[tuple[str, Any]] = []
            for raw_key, raw_value in current.items():
                if len(selected) >= MAX_OPEN_ITEMS:
                    break
                if not isinstance(raw_key, str):
                    continue
                selected.append((raw_key, raw_value))
            selected.sort(key=lambda pair: pair[0])
            result: dict[str, SafeJson] = {}
            for raw_key, raw_value in selected:
                clean_key = raw_key[:MAX_OPEN_KEY_CHARS]
                if clean_key in result:
                    # Clipping two different keys to one name must not silently
                    # replace the earlier value.
                    continue
                result[clean_key] = visit(raw_value, depth + 1, key=raw_key)
            return result
        return UNSUPPORTED

    return visit(value, 0)


def _safe_object(value: Any) -> dict[str, SafeJson]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("must be a JSON object")
    sanitized = redact_bounded_json(value)
    if not isinstance(sanitized, dict):  # defensive; root dict is guaranteed
        raise ValueError("must be a JSON object")
    return sanitized


class WireModel(BaseModel):
    """Base for allowlisted owner-wire data transfer objects."""

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        populate_by_name=False,
        # Provider adapters are an untrusted process boundary.  Pydantic
        # normally accepts an existing model instance without replaying its
        # validators, which would let ``model_construct()`` bypass this wire
        # contract.  Always revalidate, including nested owner DTOs.
        revalidate_instances="always",
        validate_default=True,
    )


class MissionWire(WireModel):
    mission_id: Identifier
    session_id: Identifier
    title: ShortText = ""
    goal: LongText = ""
    operator_id: ShortText = ""
    status: ShortText
    metadata: dict[str, SafeJson] = Field(default_factory=dict)
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None

    _redact_metadata = field_validator("metadata", mode="before")(_safe_object)


class TaskWire(WireModel):
    task_id: Identifier
    mission_id: Identifier
    title: ShortText = ""
    description: LongText = ""
    status: ShortText
    priority: ShortText = "normal"
    assigned_to: ShortText = ""
    result: LongText = ""
    metadata: dict[str, SafeJson] = Field(default_factory=dict)
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None

    _redact_metadata = field_validator("metadata", mode="before")(_safe_object)


class AttemptWire(WireModel):
    attempt_id: Identifier
    mission_id: Identifier
    session_id: Identifier
    task_id: Identifier
    claim_id: Identifier
    assigned_to: ShortText = ""
    assigned_by: ShortText = ""
    status: ShortText
    failure_code: ShortText = ""
    idempotency_key: ShortText = ""
    metadata: dict[str, SafeJson] = Field(default_factory=dict)
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None

    _redact_metadata = field_validator("metadata", mode="before")(_safe_object)


class AgentLeaseWire(WireModel):
    claim_id: Identifier
    mission_id: Identifier
    session_id: Identifier
    task_id: Identifier
    agent_id: Identifier
    # Empty is meaningful for ``active_claim_without_run`` reconciliation.
    attempt_id: ShortText = ""
    status: ShortText
    active: bool
    expired: bool
    heartbeat_at: AwareDatetime | None = None
    stale_after: AwareDatetime | None = None
    metadata: dict[str, SafeJson] = Field(default_factory=dict)

    _redact_metadata = field_validator("metadata", mode="before")(_safe_object)


class ReceiptWire(WireModel):
    receipt_id: Identifier
    mission_id: Identifier
    task_id: Identifier
    attempt_id: Identifier
    agent_id: Identifier
    receipt_type: ShortText
    status: ShortText
    idempotency_key: ShortText = ""
    payload: dict[str, SafeJson] = Field(default_factory=dict)
    created_at: AwareDatetime | None = None

    _redact_payload = field_validator("payload", mode="before")(_safe_object)


class MissionSnapshotWire(WireModel):
    """Validated public projection of one canonical MissionSnapshot."""

    mission: MissionWire
    tasks: tuple[TaskWire, ...] = Field(default=(), max_length=MAX_SNAPSHOT_RECORDS)
    attempts: tuple[AttemptWire, ...] = Field(
        default=(), max_length=MAX_SNAPSHOT_RECORDS
    )
    leases: tuple[AgentLeaseWire, ...] = Field(
        default=(), max_length=MAX_SNAPSHOT_RECORDS
    )
    receipts: tuple[ReceiptWire, ...] = Field(
        default=(), max_length=MAX_SNAPSHOT_RECORDS
    )
    reconciliation: ReconciliationState
    observed_at: AwareDatetime
    authority: Literal["TaskBoard+RuntimeStateStore"] = AUTHORITY
    proves_executor_liveness: Literal[False] = False

    @model_validator(mode="after")
    def records_belong_to_snapshot_mission(self) -> "MissionSnapshotWire":
        """Reject owner documents that splice records across missions.

        ``foreign_runtime_record`` may report that the owner detected foreign
        evidence, but the foreign record itself must not cross this public
        projection boundary.
        """

        expected = self.mission.mission_id
        collections = (self.tasks, self.attempts, self.leases, self.receipts)
        if any(
            record.mission_id != expected
            for records in collections
            for record in records
        ):
            raise ValueError("snapshot contains a cross-mission record")
        identity_axes = (
            (self.tasks, "task_id"),
            (self.attempts, "attempt_id"),
            (self.leases, "claim_id"),
            (self.receipts, "receipt_id"),
        )
        for records, attribute in identity_axes:
            values = [getattr(record, attribute) for record in records]
            if len(values) != len(set(values)):
                raise ValueError(f"snapshot contains duplicate {attribute}")
        return self


def validate_owner_snapshot(value: Any) -> MissionSnapshotWire:
    """Validate an untrusted owner-wire object and return the safe DTO."""

    return MissionSnapshotWire.model_validate(value)


def snapshot_version(snapshot: MissionSnapshotWire) -> str:
    """Compute a stable opaque state version for a validated projection.

    ``observed_at`` is deliberately excluded: polling unchanged owner state
    must not create a new source version or a new Needs-John identity.
    """

    body = snapshot.model_dump(mode="json", exclude={"observed_at"})
    encoded = json.dumps(
        body, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = [
    "AUTHORITY",
    "AgentLeaseWire",
    "AttemptWire",
    "Identifier",
    "LongText",
    "MAX_OPEN_DEPTH",
    "MAX_OPEN_ITEMS",
    "MAX_OPEN_KEY_CHARS",
    "MAX_OPEN_NODES",
    "MAX_OPEN_STRING_CHARS",
    "MAX_SNAPSHOT_RECORDS",
    "MissionSnapshotWire",
    "MissionWire",
    "OpaqueVersion",
    "REDACTED",
    "ReceiptWire",
    "ReconciliationState",
    "SafeJson",
    "ShortText",
    "TRUNCATED",
    "TaskWire",
    "UNSUPPORTED",
    "redact_bounded_json",
    "snapshot_version",
    "validate_owner_snapshot",
]
