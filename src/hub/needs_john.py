"""Pure Needs-John derivation from a validated Mission Control snapshot.

Needs John is not another queue or owner database.  The same snapshot always
produces the same item identities; an item disappears only when a newer owner
projection no longer satisfies its rule.  This D4 boundary is read-only, so all
``allowed_commands`` collections are intentionally empty.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from hub.mission_contract import (
    AUTHORITY,
    MissionSnapshotWire,
    OpaqueVersion,
    ReconciliationState,
    ShortText,
    snapshot_version,
)


RULE_VERSION = "fleet.needs_john.rules.v1"
MAX_EVIDENCE_REFS = 32


class NeedsJohnModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NeedsJohnItem(NeedsJohnModel):
    item_id: str = Field(min_length=1, max_length=64, pattern=r"^needs_john_[0-9a-f]{24}$")
    kind: ShortText
    mission_id: str = Field(min_length=1, max_length=256)
    task_id: str | None = Field(default=None, max_length=256)
    source_authority: Literal["TaskBoard+RuntimeStateStore"] = AUTHORITY
    source_version: OpaqueVersion
    observed_at: AwareDatetime
    requested_action: ShortText
    reason: ShortText
    recommended_default: ShortText
    deadline: AwareDatetime | None = None
    consequence: ShortText
    evidence_refs: tuple[str, ...] = Field(
        default=(), max_length=MAX_EVIDENCE_REFS
    )
    allowed_commands: tuple[()] = ()
    commands_available: Literal[False] = False
    proves_executor_liveness: Literal[False] = False


class NeedsJohnProjection(NeedsJohnModel):
    rule_version: Literal["fleet.needs_john.rules.v1"] = RULE_VERSION
    mission_id: str = Field(min_length=1, max_length=256)
    source_version: OpaqueVersion
    observed_at: AwareDatetime
    source_authority: Literal["TaskBoard+RuntimeStateStore"] = AUTHORITY
    items: tuple[NeedsJohnItem, ...]
    count: int = Field(ge=0)
    commands: tuple[()] = ()
    commands_available: Literal[False] = False
    proves_executor_liveness: Literal[False] = False


@dataclass(frozen=True, slots=True)
class _Rule:
    requested_action: str
    reason: str
    recommended_default: str
    consequence: str


# Include COHERENT explicitly so adding an owner enum state cannot silently
# bypass Needs-John policy.  ``None`` means the reconciled state needs no item.
_RECONCILIATION_RULES: dict[ReconciliationState, _Rule | None] = {
    ReconciliationState.COHERENT: None,
    ReconciliationState.NEEDS_TASK_PROJECTION: _Rule(
        "Inspect the owner projection gap before changing task state",
        "Runtime evidence and TaskBoard state have not converged",
        "Leave the task unchanged until the same fenced lineage is reconciled",
        "The board remains non-coherent and consequential commands stay unavailable",
    ),
    ReconciliationState.MISSING_TERMINAL_RECEIPT: _Rule(
        "Inspect the terminal attempt and locate canonical terminal evidence",
        "A terminal owner state has no matching runtime receipt",
        "Do not treat the attempt result as verified",
        "Terminal outcome remains reported rather than evidenced",
    ),
    ReconciliationState.CONFLICTING_ACTIVE_CLAIMS: _Rule(
        "Resolve which fenced claim, if any, still owns the task",
        "More than one active claim is visible for the same task lineage",
        "Do not reassign or advance the task",
        "Concurrent executors may produce incompatible effects",
    ),
    ReconciliationState.ACTIVE_CLAIM_WITHOUT_RUN: _Rule(
        "Inspect the active claim and its missing delegation run",
        "An active claim has no corresponding owner run",
        "Keep the task blocked and avoid replacement execution",
        "Execution ownership cannot be established",
    ),
    ReconciliationState.EXPIRED_LEASE: _Rule(
        "Choose whether an owner-side recovery should inspect this expired lease",
        "A claim lease expired without a coherent terminal projection",
        "Do not reassign until the fenced lineage is reconciled",
        "The task remains blocked by stale execution ownership",
    ),
    ReconciliationState.EVIDENCE_SCAN_SATURATED: _Rule(
        "Narrow or repair the owner evidence query before relying on this snapshot",
        "The bounded evidence scan saturated before uniqueness could be proven",
        "Treat reconciliation as unknown",
        "Conflicting evidence may exist outside the inspected window",
    ),
    ReconciliationState.FOREIGN_RUNTIME_RECORD: _Rule(
        "Inspect the runtime record whose mission identity does not match",
        "A projected runtime record is outside this mission's authority boundary",
        "Quarantine the foreign record from task decisions",
        "Attribution and ownership remain unresolved",
    ),
    ReconciliationState.CONFLICTING_TERMINAL_EVIDENCE: _Rule(
        "Review the incompatible terminal evidence before accepting an outcome",
        "The same execution lineage has conflicting terminal evidence",
        "Do not promote either terminal claim",
        "The task outcome cannot be verified",
    ),
}

if frozenset(_RECONCILIATION_RULES) != frozenset(ReconciliationState):
    raise RuntimeError("Needs-John reconciliation rules are not exhaustive")


_TASK_RULES: dict[str, tuple[str, _Rule]] = {
    "blocked": (
        "blocked_task",
        _Rule(
            "Inspect the canonical blocker and decide whether operator input is required",
            "TaskBoard reports this task as blocked",
            "Leave the task blocked until the owner records a valid resolution",
            "Dependent work cannot safely advance",
        ),
    ),
    "quarantined": (
        "quarantined_task",
        _Rule(
            "Review the quarantine evidence and owner policy",
            "TaskBoard reports this task as quarantined",
            "Keep the task quarantined",
            "The task cannot re-enter execution without an owner-side decision",
        ),
    ),
    "input-required": (
        "input_required",
        _Rule(
            "Provide the requested operator input through an authorized owner command",
            "The projected task is waiting for operator input",
            "Do not infer or auto-submit John's decision",
            "The task remains paused",
        ),
    ),
    "auth-required": (
        "auth_required",
        _Rule(
            "Restore the required authority or credential outside the projection",
            "The projected task cannot continue without authorization",
            "Keep the operation fail-closed",
            "The task remains paused without expanding Fleet Hub authority",
        ),
    ),
}


def _stable_item_id(kind: str, mission_id: str, entity_id: str) -> str:
    payload = "\x1f".join((RULE_VERSION, kind, mission_id, entity_id)).encode(
        "utf-8"
    )
    return "needs_john_" + hashlib.sha256(payload).hexdigest()[:24]


def _evidence(*refs: str) -> tuple[str, ...]:
    # Callers place mandatory mission/entity references first. Preserve that
    # priority while deduplicating so a large optional evidence scan cannot
    # sort required provenance out of the bounded result.
    return tuple(dict.fromkeys(refs))[:MAX_EVIDENCE_REFS]


def _reconciliation_evidence(snapshot: MissionSnapshotWire) -> tuple[str, ...]:
    state = snapshot.reconciliation
    refs = [
        f"mission:{snapshot.mission.mission_id}",
        f"reconciliation:{state.value}",
    ]
    if state in {
        ReconciliationState.CONFLICTING_ACTIVE_CLAIMS,
        ReconciliationState.ACTIVE_CLAIM_WITHOUT_RUN,
        ReconciliationState.EXPIRED_LEASE,
    }:
        refs.extend(f"claim:{lease.claim_id}" for lease in snapshot.leases)
    if state in {
        ReconciliationState.NEEDS_TASK_PROJECTION,
        ReconciliationState.MISSING_TERMINAL_RECEIPT,
        ReconciliationState.CONFLICTING_TERMINAL_EVIDENCE,
    }:
        refs.extend(f"attempt:{attempt.attempt_id}" for attempt in snapshot.attempts)
        refs.extend(f"receipt:{receipt.receipt_id}" for receipt in snapshot.receipts)
    return _evidence(*refs)


def _item(
    *,
    snapshot: MissionSnapshotWire,
    source_version: str,
    kind: str,
    entity_id: str,
    rule: _Rule,
    task_id: str | None,
    deadline: datetime | None,
    evidence_refs: tuple[str, ...],
) -> NeedsJohnItem:
    return NeedsJohnItem(
        item_id=_stable_item_id(kind, snapshot.mission.mission_id, entity_id),
        kind=kind,
        mission_id=snapshot.mission.mission_id,
        task_id=task_id,
        source_version=source_version,
        observed_at=snapshot.observed_at,
        requested_action=rule.requested_action,
        reason=rule.reason,
        recommended_default=rule.recommended_default,
        deadline=deadline,
        consequence=rule.consequence,
        evidence_refs=evidence_refs,
    )


def derive_needs_john(
    snapshot: MissionSnapshotWire,
    *,
    source_version: str | None = None,
) -> NeedsJohnProjection:
    """Deterministically derive the read-only operator-attention projection."""

    version = source_version or snapshot_version(snapshot)
    items: dict[str, NeedsJohnItem] = {}
    reconciliation_rule = _RECONCILIATION_RULES[snapshot.reconciliation]

    # Expired leases are entity-specific, making the item actionable and its ID
    # stable even if another lease appears.  Preserve a mission-level item when
    # the owner reports EXPIRED_LEASE without exporting the offending record.
    expired = tuple(lease for lease in snapshot.leases if lease.expired)
    expired_rule = _RECONCILIATION_RULES[ReconciliationState.EXPIRED_LEASE]
    assert expired_rule is not None
    for lease in expired:
        lease_refs = [
            f"mission:{snapshot.mission.mission_id}",
            f"task:{lease.task_id}",
            f"claim:{lease.claim_id}",
        ]
        if lease.attempt_id:
            lease_refs.append(f"attempt:{lease.attempt_id}")
        item = _item(
            snapshot=snapshot,
            source_version=version,
            kind="expired_lease",
            entity_id=lease.claim_id,
            rule=expired_rule,
            task_id=lease.task_id,
            deadline=lease.stale_after,
            evidence_refs=_evidence(*lease_refs),
        )
        items[item.item_id] = item

    if reconciliation_rule is not None and not (
        snapshot.reconciliation is ReconciliationState.EXPIRED_LEASE and expired
    ):
        state = snapshot.reconciliation.value
        item = _item(
            snapshot=snapshot,
            source_version=version,
            kind=state,
            entity_id=state,
            rule=reconciliation_rule,
            task_id=None,
            deadline=None,
            evidence_refs=_reconciliation_evidence(snapshot),
        )
        items[item.item_id] = item

    for task in snapshot.tasks:
        task_rule = _TASK_RULES.get(task.status.casefold())
        if task_rule is None:
            continue
        kind, rule = task_rule
        item = _item(
            snapshot=snapshot,
            source_version=version,
            kind=kind,
            entity_id=task.task_id,
            rule=rule,
            task_id=task.task_id,
            deadline=None,
            evidence_refs=_evidence(
                f"mission:{snapshot.mission.mission_id}", f"task:{task.task_id}"
            ),
        )
        items[item.item_id] = item

    ordered = tuple(sorted(items.values(), key=lambda item: (item.kind, item.item_id)))
    return NeedsJohnProjection(
        mission_id=snapshot.mission.mission_id,
        source_version=version,
        observed_at=snapshot.observed_at,
        items=ordered,
        count=len(ordered),
    )


def derive_needs_john_items(
    snapshot: MissionSnapshotWire,
    *,
    source_version: str | None = None,
) -> tuple[NeedsJohnItem, ...]:
    """Convenience view for callers that only need the derived item tuple."""

    return derive_needs_john(snapshot, source_version=source_version).items


__all__ = [
    "MAX_EVIDENCE_REFS",
    "NeedsJohnItem",
    "NeedsJohnProjection",
    "RULE_VERSION",
    "derive_needs_john",
    "derive_needs_john_items",
]
