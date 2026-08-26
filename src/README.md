# Fleet Hub v1 candidate runtime

This tree contains the FastAPI service and five-tab phone PWA. The application
version is `1.0.0-dev` and its build status is `candidate-unqualified` until the
owner adapters, live canary, device matrix, and deployment gate are complete.

## Runtime invariants

- Auth fails closed when `FLEET_HUB_TOKEN` is absent.
- A successful login creates a random, server-side, expiring and revocable
  session. The browser receives only an HttpOnly cookie plus an in-memory CSRF
  token.
- Cookie-authenticated mutations require the session-bound CSRF token and
  same-origin request metadata. Bearer authentication is non-ambient and is
  retained for scoped automation compatibility.
- Chat ingress uses stable identifiers and a caller-bound, process-local
  idempotency registry plus the broker's finite dedupe window. Broker acceptance
  is labeled `PUBLISH_ACCEPTED`; a duplicate acknowledgement is labeled
  separately and neither is called delivery, processing, reply, or effect.
- The multiplexed SSE ring is process-local. An unprovable resume gap produces
  `reset_required`, and clients refetch versioned reads.
- Mission Control data is validated against a bounded, redacted wire contract.
  The default provider is unavailable and exposes no commands.
- Presence signals retain source, observation time, and TTL. Sender values from
  untrusted payloads remain reported identity, not authenticated identity.

## Layout

```text
src/
  server.py
  hub/
    auth.py                 # random expiring/revocable sessions and CSRF
    mission_contract.py     # bounded owner-wire DTOs and reconciliation enum
    mission_provider.py     # read-only configured-mission adapter boundary
    needs_john.py           # pure deterministic derived projection
    natsio.py state.py presence.py monitor.py
  static/
    index.html style.css app.js sw.js
    manifest.webmanifest icons/
  roster.json vision.json
  systemd/fleet-hub.service
  install_on_agni.sh
  tests/
```

## Environment

| Variable | Default | Contract |
|---|---|---|
| `FLEET_HUB_TOKEN` | empty | Mandatory; empty keeps application APIs locked |
| `FLEET_HUB_ROOT` | directory containing `server.py` | Read-only runtime assets |
| `FLEET_HUB_ROSTER` | `$FLEET_HUB_ROOT/roster.json` | Canonical roster projection input |
| `FLEET_HUB_VISION` | `$FLEET_HUB_ROOT/vision.json` | Legacy read projection input |
| `FLEET_HUB_INSECURE_COOKIE` | unset | `1` only for local HTTP development |
| `FLEET_HUB_BASE_PATH` | `/fleet/` | Service-worker allowance; set `/` only for root-mounted local development |
| `FLEET_HUB_MAX_BODY_BYTES` | bounded server default | Pre-parse HTTP request-body ceiling; proxy ceiling must be no larger |
| `NATS_URL` | `nats://127.0.0.1:4222` | Governed existing bus; no second bus |
| `NATS_USER` / `NATS_PASS` | unset | Credentials remain host-side only |
| `NATS_STREAM` | `DHARMA_A2A` | Compatibility stream configuration |
| `NATS_CHAT_SUBJECT` | `dharma.fleet.chat` | Group transcript, not implicit responder fan-out |
| `NATS_MONITOR_URL` | `http://127.0.0.1:8222` | Private aggregate source; never exposed raw |
| `FLEET_LIVE_WINDOW_S` | `300` | Fresh presence TTL window |
| `FLEET_RECENT_WINDOW_S` | `7200` | Recent presence window |
| `FLEET_REPLAY_HOURS` | `48` | Bounded startup compatibility replay |

Exact versioned routes and capabilities are advertised by
`GET /api/v1/bootstrap`. A missing owner adapter is a typed unavailable state,
not an empty task list.

Mission `source_version` values are Fleet Hub projection digests, not an atomic
TaskBoard expected-version primitive. They support bounded reads and client
invalidation only.

## Install boundary

`install_on_agni.sh` is an explicit operator tool. It installs a new immutable
release under `/opt/dharma/fleet-hub/releases`, atomically switches `current`,
and preserves a `previous` rollback pointer. The service runs as the
unprivileged `fleet-hub` user and binds loopback behind the existing reverse
proxy. Tests and imports never invoke the installer.

See `../DEPLOY_AGNI.md`. Its presence is not deploy authorization.
