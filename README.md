# Fleet Hub

Fleet Hub is the phone-first operator projection for the Dharma fleet. It does
not own task/runtime state, create another board, or create another bus.

## Current truth — 2026-08-28

- The runnable v1 candidate is on
  [`agent/fleet-hub-working-r10-20260827`](https://github.com/AIKAGRYA/fleet-hub/tree/agent/fleet-hub-working-r10-20260827)
  and review is open in [PR #16](https://github.com/AIKAGRYA/fleet-hub/pull/16).
- An isolated candidate is active on Meghadharma at `127.0.0.1:8872`. It is
  loopback-only, persistently marked **SIMULATION**, and has no production
  owner effect. AGNI, Caddy, systemd, NATS topology/ACLs, and the public
  `/fleet/` deployment were not changed.
- Helm, Chat, Board, Trace, and Roster render and authenticate at 390×844.
  Live browser proof found no page overflow, undersized visible targets, or
  browser errors.
- Mission data crosses an authenticated, bounded HTTP adapter from the
  `TaskBoard+RuntimeStateStore` owner. The active proof uses one isolated
  fixture mission with three tasks because no production mission was put in
  scope. Board commands remain unavailable.
- NATS is live on the existing `DHARMA_A2A` stream. The candidate explicitly
  discloses the borrowed `grok_build` transport principal, observes only the
  authorized `dharma.a2a.>` tier, and uses the existing
  `dharma.a2a.fleet` group subject. Startup replay and broad
  `dharma.agent.>` observation are unavailable for this credential tier.
- Direct messages route only to live-card-backed
  `dharma.agent.<uid>.inbox` subjects. Publish/handler/domain receipts never
  prove executor liveness or effect, and a semantic reply is not promised.

Read [the implementation status](docs/FLEET_HUB_V1_IMPLEMENTATION.md) and the
[build receipt](BUILD_RECEIPT.md) before widening any claim or authority.

## Open the active Meghadharma candidate

Create a tunnel and leave it running:

```bash
ssh -N -L 8872:127.0.0.1:8872 root@178.128.87.170
```

Open <http://127.0.0.1:8872/>. Retrieve the root-only login token in a separate
terminal; do not paste it into logs or commits:

```bash
ssh root@178.128.87.170 \
  'cat /root/fleet-hub-r10-candidate-20260828/runtime/fleet-login-token'
```

To inspect the two processes, allocate a terminal explicitly:

```bash
ssh -t root@178.128.87.170 \
  'tmux -L fleet-r10 attach -t fleet-r10-candidate'
```

## Local development

Python 3.11 and 3.12 are supported and locked with `uv`:

```bash
uv sync --locked --group dev
FLEET_HUB_TOKEN=local-test-only \
FLEET_HUB_INSECURE_COOKIE=1 \
FLEET_HUB_BASE_PATH=/ \
uv run --no-sync uvicorn server:app --app-dir src --host 127.0.0.1 --port 8444
```

Verification:

```bash
uv run --no-sync python -m compileall -q src
uv run --no-sync ruff check src scripts
uv run --no-sync pytest -q
bash -n scripts/launch_meghadharma_loopback.sh scripts/run_hub_from_env.sh
shellcheck scripts/launch_meghadharma_loopback.sh scripts/run_hub_from_env.sh
node --check src/static/app.js
git diff --check
```

## Repository map

- `src/server.py` — authenticated FastAPI HTTP/SSE boundary
- `src/hub/` — owner adapter, NATS routing, presence, and projections
- `src/static/` — zero-build, five-tab phone PWA
- `src/tests/` — contract, security, owner, NATS, and UI tests
- `scripts/launch_meghadharma_loopback.sh` — isolated non-promoting launcher
- `evidence/r10-20260828/` — A2A identity and live phone evidence
- `DEPLOY_AGNI.md` — separately authorized production procedure; not run here

No token, NATS credential, owner database, or production state belongs in this
repository.
