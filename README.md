# Fleet Hub

Fleet Hub is the phone-first operator projection for the Dharma fleet. It is a
separate product from Dharma Command and it does not own task state, runtime
state, or a second message bus.

## Current truth

- This repository is the canonical code home: <https://github.com/AIKAGRYA/fleet-hub>.
- The branch built from the 2026-08-26 v1 candidate is a
  **candidate-unqualified** implementation. It is not a production deployment
  or a claim that the fleet approved the candidate.
- The last researched AGNI deployment reported v0.5.1. This repository does not
  claim that the new build is installed there.
- Mission and Needs-John data are read-only projections of
  `TaskBoard+RuntimeStateStore`. The default production adapter is unavailable
  until an authenticated owner-service route is configured.
- Board mutations are unavailable. The inspected owner does not yet provide an
  atomic expected-version transition, so Fleet Hub will not emulate one locally
  or with raw NATS.

Read [docs/FLEET_HUB_V1_IMPLEMENTATION.md](docs/FLEET_HUB_V1_IMPLEMENTATION.md)
for the implemented boundary and remaining qualification gates.

## Local development

Python 3.11 and 3.12 are supported. Dependencies and tests are locked with
`uv`:

```bash
uv sync --locked --group dev
FLEET_HUB_TOKEN=local-test-only \
FLEET_HUB_INSECURE_COOKIE=1 \
FLEET_HUB_BASE_PATH=/ \
uv run --no-sync uvicorn server:app --app-dir src --host 127.0.0.1 --port 8444
```

Open <http://127.0.0.1:8444/>. The deployed reverse proxy mounts the same app at
`/fleet/`. `FLEET_HUB_BASE_PATH=/` makes the local service-worker allowance
match the root URL; installability itself is qualified at the manifest's
canonical `/fleet/` production scope.

Run the verification gate with:

```bash
uv run --no-sync python -m compileall -q src
uv run --no-sync pytest -q
git diff --check
```

## Repository map

- `src/server.py` — FastAPI wiring and versioned HTTP/SSE boundary
- `src/hub/` — sessions, projections, presence, NATS ingress, and owner adapters
- `src/static/` — zero-build five-tab PWA
- `src/tests/` — unit, contract, security, and static/PWA tests
- `src/install_on_agni.sh` — explicit, versioned release installer; never invoked
  by imports or tests
- `DEPLOY_AGNI.md` — separately authorized qualification/deployment procedure

No token, NATS credential, owner database, or production state belongs in this
repository.
