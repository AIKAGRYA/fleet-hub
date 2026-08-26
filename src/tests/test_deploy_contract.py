from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_service_runs_unprivileged_and_hardened() -> None:
    unit = (ROOT / "systemd" / "fleet-hub.service").read_text(encoding="utf-8")
    assert "User=fleet-hub" in unit
    assert "Group=fleet-hub" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "User=root" not in unit


def test_installer_uses_immutable_release_pointer() -> None:
    script = (ROOT / "install_on_agni.sh").read_text(encoding="utf-8")
    assert "APP_ROOT=/opt/dharma/fleet-hub" in script
    assert 'RELEASE_DIR="$RELEASE_ROOT/$RELEASE_ID"' in script
    assert 'mv -Tf "$APP_ROOT/current.next" "$CURRENT_LINK"' in script
    assert "rm -rf" not in script
    assert "rsync -a --delete" not in script
    assert "FLEET_HUB_TOKEN" in script
    assert 'uv sync --project "$REPO_ROOT" --locked --no-dev' in script
    assert 'UV_PROJECT_ENVIRONMENT="$RELEASE_DIR/.venv"' in script


def test_service_uses_release_bound_locked_environment() -> None:
    unit = (ROOT / "systemd" / "fleet-hub.service").read_text(encoding="utf-8")
    assert "current/.venv/bin/python -m uvicorn" in unit
    assert "ExecStart=/usr/bin/python" not in unit


def test_installer_never_prints_token() -> None:
    script = (ROOT / "install_on_agni.sh").read_text(encoding="utf-8")
    assert 'echo "$fleet_token"' not in script
    assert "unset fleet_token" in script
    assert "Authorization: Bearer $fleet_token" not in script
    assert '-H "@$curl_header_file"' in script
    assert "mktemp /run/fleet-hub-curl-header" in script
