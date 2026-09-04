# Fleet Hub owner adapter — local integration evidence

Status: **local integration, not production.** This record proves one thing:
the read path from the canonical owner databases to the phone routes exists
in code and works end to end on one machine. It does not prove AGNI is
configured, that a live executor exists, or that any command works
(commands remain disabled by contract).

## Claim locus

| Field | Value |
|---|---|
| fleet-hub base | `320032b` on branch `claude/dharma-swarm-fusion-review-voer57` (this change applied on top) |
| dharma_swarm base | `d2b2f40` on branch `claude/dharma-swarm-fusion-review-voer57` (owner router applied on top) |
| host | Claude Code cloud sandbox (Linux 6.18, Python 3.11.15, uv 0.8.17) |
| date | 2026-09-04 |
| broker | local `nats-server v2.11.4 -js` on 127.0.0.1:4222, empty `DHARMA_A2A` |
| evidence mode reported by hub | `local_integration` |

## What ran

1. Seeded one mission, one task, one active attempt through
   `dharma_swarm.mission_control.MissionControl` into a throwaway
   `DHARMA_STATE_DIR` (`state/runtime.db` + `db/tasks.db`).
2. Started the owner: `uvicorn api.main:app` on 127.0.0.1:8420 with
   `DASHBOARD_API_KEY` set and `DASHBOARD_API_MODE=production`.
3. Started Fleet Hub: `uvicorn server:app --app-dir src` on 127.0.0.1:8444 with
   `FLEET_HUB_MISSION_IDS=fleet-fusion-e2e`,
   `FLEET_HUB_MISSION_PROVIDER_URL=http://127.0.0.1:8420`,
   `FLEET_HUB_MISSION_PROVIDER_TOKEN=<owner key>`.
4. Logged in with the hub token, then read the four phone routes with the
   session cookie, CSRF token, and same-origin fetch metadata.

## Observed

Owner, direct, authenticated (`GET /api/mission-control/missions`):

```
{"ok": true, "count": 1, "authority": "TaskBoard+RuntimeStateStore", "proves_executor_liveness": false} ['fleet-fusion-e2e']
```

Owner, direct, anonymous: `HTTP 401`.

Hub `GET /api/v1/bootstrap` (excerpt):

```
"evidence_mode": "local_integration",
"connections": {"hub": true, "nats": true, "mission_control": true,
                "mission_provider_kind": "owner_http_read_only", ...},
"mission_read": true, "selected": "fleet-fusion-e2e", "needs_john_count": 0
```

Hub `GET /api/v1/missions`:

```
{"available": true, "count": 1,
 "missions": [{"mission_id": "fleet-fusion-e2e", "title": "Fleet fusion e2e",
               "status": "active", "reconciliation": "coherent"}],
 "commands_available": false}
```

Hub `GET /api/v1/missions/fleet-fusion-e2e/snapshot`:

```
{"available": true, "authority": "TaskBoard+RuntimeStateStore",
 "proves_executor_liveness": false, "reconciliation": "coherent",
 "tasks": [["48eabccf34a5", "assigned"]], "leases": 1, "attempts": 1}
```

Hub `GET /api/v1/needs-john`: `{"available": true, "count": 0, "items": []}`
(a coherent snapshot derives no attention item, per `hub/needs_john.py`).

Owner credential leakage check over the bootstrap body: `0` occurrences.

## Test evidence

| Suite | Command | Result |
|---|---|---|
| fleet-hub | `uv run --no-sync pytest -q` | 256 passed (226 before this change, 30 new in `src/tests/test_mission_http_provider.py`) |
| dharma_swarm owner router | `python -m pytest tests/test_api_mission_control.py -q` | 11 passed |
| dharma_swarm neighbours | `python -m pytest tests/test_api_auth.py tests/test_api_main_bootstrap.py -q` | 53 passed |

Reproduce: `/tmp` scratch script is not committed; the steps above are the
complete procedure and every value is derivable from the two repos at the
loci named.

## What this does not prove

- Nothing about AGNI, rushabdev, meghadharma, or any Hermes seat. No
  production host has `FLEET_HUB_MISSION_PROVIDER_*` set.
- Nothing about executor liveness. The owner literal `proves_executor_liveness`
  is `False` on every object, and Fleet Hub rejects any other value.
- Nothing about commands. `source_version` is still a projection digest, not an
  owner compare-and-swap token; steer/assign/claim stay disabled.
- Nothing about presence. `connections.nats: true` here means the hub reached
  an empty local broker.
