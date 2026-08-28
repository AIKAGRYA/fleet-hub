"""Dependency-free contracts for the Fleet Hub v1 phone shell."""

from __future__ import annotations

import re
from pathlib import Path


STATIC = Path(__file__).parents[1] / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


def test_exact_five_tab_order_and_hash_routes() -> None:
    tabs = re.findall(
        r'<a class="tab" href="#/([^\"]+)" data-view="([^\"]+)">.*?<span>([^<]+)</span>',
        INDEX,
        flags=re.DOTALL,
    )
    assert tabs == [
        ("helm", "helm", "Helm"),
        ("chat", "chat", "Chat"),
        ("board", "board", "Board"),
        ("trace", "trace", "Trace"),
        ("roster", "roster", "Roster"),
    ]
    assert "aria-current" in APP
    assert "document.title" in APP
    assert "hashchange" in APP


def test_needs_john_is_a_persistent_accessible_rail() -> None:
    assert 'id="needs-toggle"' in INDEX
    assert 'aria-controls="needs-rail"' in INDEX
    assert 'id="needs-rail"' in INDEX
    assert 'id="needs-badge"' in INDEX
    assert 'role="dialog"' in INDEX
    assert 'aria-modal="true"' in INDEX
    assert "app.setAttribute('inert', '')" in APP
    assert "trapNeedsFocus" in APP
    assert "Escape" in APP
    assert "owner authority" in INDEX


def test_dynamic_content_uses_safe_dom_and_tokens_are_not_persisted() -> None:
    assert ".innerHTML" not in APP
    assert "insertAdjacentHTML" not in APP
    assert "document.write" not in APP
    assert "localStorage" not in APP
    assert "sessionStorage" not in APP
    assert "textContent" in APP
    assert "createTextNode" in APP


def test_api_client_is_cookie_csrf_idempotent_and_bounded() -> None:
    assert "credentials: 'same-origin'" in APP
    assert "X-CSRF-Token" in APP
    assert "Idempotency-Key" in APP
    assert "If-Match" in APP
    assert "AbortController" in APP
    api_source = APP[APP.index("async function api"):APP.index("async function optionalApi")]
    assert api_source.index("payload = await response.json()") < api_source.index("clearTimeout(timeout)")
    assert "REQUEST_TIMEOUT_MS" in APP
    assert "url.origin !== location.origin" in APP
    for status in (401, 403, 409, 422, 503):
        assert f"status === {status}" in APP or str(status) in APP


def test_board_commands_fail_closed_and_trace_is_bounded_redacted() -> None:
    assert "commands_available !== true" in APP
    assert "Board unavailable" in APP
    assert "Fleet Hub will not synthesize board state" in APP
    assert "TRACE_LIMIT = 200" in APP
    assert "[REDACTED]" in APP
    assert "role: 'log'" in APP  # Chat
    trace_source = APP[APP.index("function viewTrace"):APP.index("function rosterRow")]
    assert "role: 'log'" not in trace_source


def test_chat_keeps_room_drafts_and_exposes_real_retry_control() -> None:
    assert "drafts: new Map()" in APP
    assert "state.drafts.set" in APP
    assert "Retry this message" in APP
    assert "retryMessage" in APP
    assert "one multiplexed events/stream" in APP


def test_chat_identity_styling_requires_a_server_derived_claim() -> None:
    render = APP[APP.index("function renderMessage"):APP.index("function feedController")]
    assert "authenticated_server_derived" in render
    assert "fleet_hub_session" in render
    assert "reported sender · unverified" in render
    assert "message.from === 'operator'" not in render
    assert "optimisticOperatorMessages = new WeakSet()" in APP
    assert "optimisticOperatorMessages.add(message)" in APP


def test_contact_receipts_and_causal_axes_survive_phone_rendering() -> None:
    render = APP[APP.index("function renderMessage"):APP.index("function feedController")]
    trace = APP[APP.index("function pushTrace"):APP.index("function mergePresence")]
    trace_view = APP[APP.index("function receiptRows"):APP.index("function viewRoster")]
    assert "message.tier || message.contact_tier || message.contact_evidence_tier" in render
    assert "executor liveness and effect unproven" in APP
    assert "message.domain_receipt_observed && message.domain_receipt" in render
    assert "original effect unproven" in render
    assert "message.tier !== 'DOMAIN_RECEIPTED'" in APP
    assert "message.tier === 'HANDLER_ACKED'" in APP
    for axis in ("correlation_id", "causation_id", "trace_id"):
        assert f"frame.{axis}" in trace
        assert f"frame.{axis}" in trace_view


def test_owner_reads_are_fenced_and_paginated_without_completeness_overclaim() -> None:
    assert "snapshotRevision" in APP
    assert "invalidateSnapshots" in APP
    assert "pending.controller.abort()" in APP
    assert "requestedSnapshotRevision !== state.snapshotRevision" in APP
    assert "reset_required" in APP
    assert "source.addEventListener('mission'" in APP
    assert "visibilitychange" in APP
    assert "Load more missions" in APP
    assert "Load more decisions" in APP
    assert "total_configured_visible" in APP
    assert "next_cursor" in APP
    assert "does not claim fleet-wide discovery" in APP


def test_needs_john_binds_the_exact_evidence_contract() -> None:
    for field in (
        "reason",
        "recommended_default",
        "evidence_refs",
        "source_authority",
        "source_version",
        "allowed_commands",
    ):
        assert f"item.{field}" in APP
    assert "permissible_actions" not in APP


def test_session_end_clears_all_private_in_memory_state() -> None:
    reset = APP[APP.index("function resetPrivateState"):APP.index("function showGate")]
    for fragment in (
        "abortActiveRequests()",
        "invalidateSnapshots()",
        "chatIndex.clear()",
        "dmIndexes.clear()",
        "state.dms.clear()",
        "state.trace = []",
        "state.drafts.clear()",
        "state.bootstrap = null",
        "state.roster = []",
    ):
        assert fragment in reset


def test_hash_parser_catches_bad_encoding_and_bounds_ids() -> None:
    parser = APP[APP.index("function safeHashId"):APP.index("function mountView")]
    assert "decodeURIComponent" in parser
    assert "catch { return null; }" in parser
    assert "HASH_ID_LIMIT" in parser
    assert "location.hash.length > 512" in parser


def test_mobile_accessibility_contracts_are_present() -> None:
    assert "viewport-fit=cover" in INDEX
    assert "Skip to content" in INDEX
    assert ":focus-visible" in CSS
    assert "min-height: 44px" in CSS
    assert "100vh" in CSS and "100dvh" in CSS
    assert "safe-area-inset-top" in CSS
    assert "safe-area-inset-bottom" in CSS
    assert "@media (max-width: 359px)" in CSS
    assert "prefers-reduced-motion: reduce" in CSS
    assert "grid-template-columns: repeat(5" in CSS
    assert "html { font-family: var(--sans); font-size: 15px" in CSS


def test_fixture_provenance_is_persistent_and_cannot_hide_as_live() -> None:
    assert 'id="mode-banner"' in INDEX
    assert 'id="mode-banner-copy"' in INDEX
    assert "payload.generated_by_fixture === true" in APP
    assert "payload.evidence_mode === 'fixture'" in APP
    assert "no production effect" in APP
    assert "paintEvidenceMode(null)" in APP


def test_helm_keeps_five_connection_dimensions_separate() -> None:
    helm = APP[APP.index("function truthDimension"):APP.index("// ---------------------------------------------------------------- Chat")]
    for label in ("Browser", "Hub", "NATS", "Owner", "Mission"):
        assert f"'{label}'" in helm
    assert "browser ↔ hub stream" in helm
    assert "transport only" in helm
    assert "owner reconciliation" in helm


def test_board_uses_versioned_read_only_lanes_without_verified_done_theater() -> None:
    assert "fleet.board.lanes.taskboard.v1" in APP
    for lane in (
        "Queue",
        "Assigned",
        "Running",
        "Review",
        "Done",
        "Failed",
        "Cancelled",
        "Quarantined",
        "Unmapped",
    ):
        assert f"label: '{lane}'" in APP
    assert "independent verification not projected" in APP


def test_roster_keeps_heard_and_addressed_signals_distinct() -> None:
    assert "Heard" in APP
    assert "Addressed" in APP
    assert "last_heard" in APP
    assert "last_addressed" in APP
    assert "projected.ttl_s" in APP
    assert "projected.source" in APP
    assert "projected.verification" in APP
    assert "update.signals" in APP
    assert "Neither proves a running process" in APP
