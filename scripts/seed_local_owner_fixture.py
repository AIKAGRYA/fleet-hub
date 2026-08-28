#!/usr/bin/env python3
"""Seed an isolated canonical Mission Control state for local Fleet proof.

This helper refuses normal ``.dharma`` directories and never overwrites an
unmarked directory.  It is for loopback integration evidence only; it does not
select, copy, or mutate a production owner state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from dharma_swarm.mission_control import MissionControl
from dharma_swarm.runtime_state import RuntimeStateStore
from dharma_swarm.task_board import TaskBoard


MARKER = ".fleet-hub-local-fixture.json"


def _safe_fixture_root(value: str, mission_id: str) -> Path:
    root = Path(value).expanduser().resolve(strict=False)
    if not root.name.startswith("fleet-hub-owner-fixture-"):
        raise ValueError("fixture state directory must start with fleet-hub-owner-fixture-")
    if root in {Path("/").resolve(), Path.home().resolve()} or root.name == ".dharma":
        raise ValueError("refusing a broad or production-shaped state directory")
    marker = root / MARKER
    if root.exists() and not marker.exists() and any(root.iterdir()):
        raise ValueError("refusing an existing unmarked non-empty directory")
    root.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("schema") != "fleet_hub.local_owner_fixture.v1":
            raise ValueError("fixture marker schema is not recognized")
        if payload.get("mission_id") != mission_id:
            raise ValueError("fixture marker belongs to another mission")
    else:
        marker.write_text(
            json.dumps(
                {
                    "schema": "fleet_hub.local_owner_fixture.v1",
                    "mission_id": mission_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "production_effect": False,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return root


async def seed(state_dir: Path, mission_id: str) -> dict[str, object]:
    (state_dir / "db").mkdir(parents=True, exist_ok=True)
    (state_dir / "state").mkdir(parents=True, exist_ok=True)
    board = TaskBoard(state_dir / "db" / "tasks.db")
    runtime = RuntimeStateStore(
        state_dir / "state" / "runtime.db", include_memory_plane=False
    )
    await board.init_db()
    await runtime.init_db()
    control = MissionControl(board, runtime)
    existing = await control.get_snapshot(mission_id)
    if existing is not None:
        return {
            "mission_id": mission_id,
            "seeded": False,
            "task_count": len(existing.tasks),
            "production_effect": False,
        }

    await control.create_mission(
        mission_id,
        title="Fleet Hub R10 local integration",
        goal="Prove the phone helm over canonical owner reads without production mutation",
        operator_id="fleet-hub-local-fixture",
        metadata={"evidence_mode": "fixture", "production_effect": False},
    )
    tasks = (
        (
            "Owner read boundary",
            "Fleet consumes one bounded Mission Control snapshot over authenticated HTTP.",
            "high",
        ),
        (
            "Canonical A2A inbox",
            "Direct messages target ratified dharma.agent.<uid>.inbox subjects.",
            "high",
        ),
        (
            "Independent promotion review",
            "Commands and Done remain unavailable until owner CAS and verification exist.",
            "normal",
        ),
    )
    for index, (title, description, priority) in enumerate(tasks, start=1):
        await control.create_task(
            mission_id,
            title=title,
            description=description,
            priority=priority,
            created_by="fleet-hub-local-fixture",
            idempotency_key=f"fleet-hub-r10-fixture-{index}",
            metadata={"evidence_mode": "fixture", "production_effect": False},
        )
    return {
        "mission_id": mission_id,
        "seeded": True,
        "task_count": len(tasks),
        "production_effect": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--mission-id", default="fleet-hub-r10-local")
    args = parser.parse_args()
    state_dir = _safe_fixture_root(args.state_dir, args.mission_id)
    print(json.dumps(asyncio.run(seed(state_dir, args.mission_id)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
