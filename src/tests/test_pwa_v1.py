"""Static PWA safety contracts; no browser or third-party dependency needed."""

from __future__ import annotations

import json
from pathlib import Path


STATIC = Path(__file__).parents[1] / "static"
MANIFEST = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
WORKER = (STATIC / "sw.js").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")


def test_manifest_has_stable_identity_scope_start_and_maskable_icons() -> None:
    assert MANIFEST["id"] == "/fleet/"
    assert MANIFEST["scope"] == "/fleet/"
    assert MANIFEST["start_url"].startswith("/fleet/")
    assert MANIFEST["display"] == "standalone"
    assert {icon["sizes"] for icon in MANIFEST["icons"]} >= {"192x192", "512x512"}
    assert all("maskable" in icon["purpose"] for icon in MANIFEST["icons"])


def test_worker_registration_uses_base_scoped_root_route() -> None:
    assert "serviceWorker.register(BASE + 'sw.js', { scope: BASE })" in APP
    assert "static/sw.js" not in APP


def test_worker_precaches_only_a_versioned_public_shell() -> None:
    assert "fleet-hub-shell-v1-" in WORKER
    assert "SHELL_PATHS" in WORKER
    assert "./static/app.js" in WORKER
    assert "./static/style.css" in WORKER
    assert "cache.addAll(SHELL_PATHS)" in WORKER
    assert "SHELL_PATHS.includes(relative)" in WORKER
    assert "if (response.ok) await cache.put(request, response.clone())" in WORKER


def test_worker_never_caches_private_or_mutating_surfaces() -> None:
    for fragment in ("/api", "/events", "/login", "/logout", "/commands"):
        assert fragment in WORKER
    assert "request.method !== 'GET'" in WORKER
    assert "event.respondWith(fetch(request))" in WORKER
    assert "Background Sync" in WORKER
    assert "addEventListener('sync'" not in WORKER


def test_offline_navigation_falls_back_to_shell_without_command_replay() -> None:
    assert "request.mode === 'navigate'" in WORKER
    assert "fetch(request).catch(() => caches.match('./'))" in WORKER
    assert "addEventListener('sync'" not in WORKER
    assert "indexedDB" not in WORKER


def test_allowlisted_shell_assets_revalidate_before_cached_fallback() -> None:
    fetch_handler = WORKER[WORKER.index("self.addEventListener('fetch'") :]
    asset_branch = fetch_handler[fetch_handler.index("const relative = relativeShellPath") :]
    assert asset_branch.index("await fetch(request)") < asset_branch.index("cache.match(request)")
    assert "return await cache.match(request) || Response.error()" in asset_branch


def test_install_metadata_is_explicitly_fixed_to_fleet_prefix() -> None:
    assert MANIFEST["id"] == MANIFEST["scope"] == "/fleet/"
    assert "Install metadata is fixed at" in APP
