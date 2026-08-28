"""Loopback-only owner app for Fleet's isolated integration fixture.

Run with the Dharma repository and this Fleet repository on ``PYTHONPATH``.
The module refuses to import an unmarked state or a write-capable Swarm boot.
It is not a deployment entrypoint.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


if not _enabled("DHARMA_READ_ONLY_BOOT"):
    raise RuntimeError("owner fixture app requires DHARMA_READ_ONLY_BOOT=1")

_raw_state_dir = os.environ.get("FLEET_HUB_OWNER_FIXTURE_STATE_DIR", "")
_state_dir = Path(_raw_state_dir).expanduser().resolve(strict=False)
_marker = _state_dir / ".fleet-hub-local-fixture.json"
if not _state_dir.name.startswith("fleet-hub-owner-fixture-") or not _marker.is_file():
    raise RuntimeError("owner fixture state is absent or unmarked")
_marker_payload = json.loads(_marker.read_text(encoding="utf-8"))
if (
    _marker_payload.get("schema") != "fleet_hub.local_owner_fixture.v1"
    or _marker_payload.get("production_effect") is not False
):
    raise RuntimeError("owner fixture marker is invalid")

from api import main as api_main  # noqa: E402
from dharma_swarm.swarm import SwarmManager  # noqa: E402

# Importing api.main performs no owner write. Before its lifespan starts, bind
# both the Swarm singleton and operator PID receipt to the isolated fixture.
api_main._OPERATOR_STATE_DIR = _state_dir
api_main._OPERATOR_PID_FILE = _state_dir / "operator.pid"
api_main._state["swarm"] = SwarmManager(state_dir=_state_dir)

app = api_main.app
