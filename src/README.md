# Fleet Hub v0.6

Phone-first operator console for the three-VPS fleet.

Live URL after install: `https://157.245.193.15/fleet/`

## What shipped vs v0.5

- **Fail-closed auth** — no `FLEET_HUB_TOKEN` on the host means the hub is
  LOCKED (v0.5 failed open). Bearer header or HMAC session cookie only; no
  query-param tokens anywhere.
- **Two-signal presence** — `last_heard` (agent spoke) vs `last_addressed`
  (traffic sent to it), with honest freshness buckets. No more hardcoded
  `live`.
- **JetStream replay** — chat history and presence survive restarts (48h
  window by default).
- **Truth-amnesty roster v2** — only provably-live agents hold `seat:
  "active"`; the rest are archived until they re-earn a seat with a
  heartbeat.
- **Single multiplexed SSE stream** with `Last-Event-ID` resume, msg-id echo
  suppression, honest ack tiers (`PUBLISH_ACCEPTED` is never "delivered").
- **PWA** — installable, standalone, manifest + icons.

## Layout

```
src/
  server.py                  # thin FastAPI wiring
  hub/                       # auth, state, presence, natsio, monitor modules
  static/
    index.html style.css app.js       # zero-build frontend
    manifest.webmanifest icons/*.png  # PWA assets
  roster.json                # fleet_roster.v2 (seat: active|archived)
  vision.json                # fleet_vision.v1 (ventures, served at /api/vision)
  systemd/fleet-hub.service
  install_on_agni.sh
  tests/                     # dev-only; excluded from deploy sync
```

## Environment (read from `/etc/dharma/fleet-hub.env` via systemd)

| Var | Default | Notes |
| --- | --- | --- |
| `FLEET_HUB_TOKEN` | *(empty)* | **Mandatory.** Empty ⇒ hub LOCKED, never open. |
| `FLEET_HUB_ROOT` | dir of server.py | Data root for roster/vision. |
| `FLEET_HUB_ROSTER` | `$ROOT/roster.json` | |
| `FLEET_HUB_VISION` | `$ROOT/vision.json` | |
| `FLEET_HUB_INSECURE_COOKIE` | *(unset)* | `1` ⇒ omit `Secure` flag (local dev only). |
| `NATS_URL` | `nats://127.0.0.1:4222` | |
| `NATS_USER` / `NATS_PASS` | *(unset)* | `NATS_PASSWORD` accepted as fallback. |
| `NATS_STREAM` | `DHARMA_A2A` | |
| `NATS_CHAT_SUBJECT` | `dharma.fleet.chat` | |
| `NATS_MONITOR_URL` | `http://127.0.0.1:8222` | varz proxy for broker health. |
| `FLEET_LIVE_WINDOW_S` | `300` | fresh bucket. |
| `FLEET_RECENT_WINDOW_S` | `7200` | recent bucket. |
| `FLEET_REPLAY_HOURS` | `48` | JetStream replay window. |
| `FLEET_REPLAY_STREAMS` | `DHARMA_A2A` | comma-separated. |

## Install on AGNI (paths unchanged from v0.5)

1. Copy this tree to AGNI (e.g. `/root/agni/fleet_hub_incoming`)
2. Write `/etc/dharma/fleet-hub.env` with `FLEET_HUB_TOKEN=...` (mode 600) —
   the installer **exits 1** without it
3. `bash /root/agni/fleet_hub_incoming/install_on_agni.sh` — syncs the whole
   tree to `/root/agni/fleet_hub`, installs the unit, runs smoke tests
   (healthz 200, unauthenticated `/api/roster` must be rejected, bearer 200)
4. Confirm Caddy still reverse-proxies `/fleet/*` → `127.0.0.1:8444`

Full phone-verifiable checklist: `DEPLOY_AGNI.md` at the repo root.
