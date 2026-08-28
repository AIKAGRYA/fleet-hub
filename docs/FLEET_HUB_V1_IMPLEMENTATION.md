# Fleet Hub v1 candidate — implementation status

Status: **candidate-unqualified**
Evidence date: 2026-08-28
Fleet review: [AIKAGRYA/fleet-hub#16](https://github.com/AIKAGRYA/fleet-hub/pull/16)
Owner review: [AIKAGRYA/dharma_swarm#1471](https://github.com/AIKAGRYA/dharma_swarm/pull/1471)

This is a working, loopback-only integration candidate. It is not the public
AGNI deployment and it is not evidence that production owner data, mutation
authority, or semantic agent replies exist.

## Implemented boundary

| Surface | Implemented fact |
|---|---|
| Phone shell | Exactly Helm, Chat, Board, Trace, and Roster, plus a persistent Needs-John rail |
| Authentication | Expiring server-side session, login/logout, CSRF, same-origin checks, and unauthenticated API rejection |
| Owner read | Bearer-authenticated bounded HTTP adapter; HTTPS except loopback; no redirects, retries, proxies, duplicate JSON keys, or unbounded bodies |
| Owner authority | One configured mission projection from the owner's exact `TaskBoard` and `RuntimeStateStore`; mismatch and partial initialization fail closed |
| Board | Nine versioned read-only lanes; three fixture tasks render; commands require owner-advertised authority and are currently unavailable |
| Needs John | Deterministic evidence-linked projection; empty is claimed only after a successful bounded owner read |
| NATS | Existing `DHARMA_A2A` stream only; authorized `dharma.a2a.>` observation; existing `dharma.a2a.fleet` group subject |
| Direct A2A | Outbound routing only for live-card-backed `dharma.agent.<uid>.inbox` bindings with card SHA/evidence |
| Receipts | PubAck, handler ACK, and typed domain receipt are distinct tiers; none implies executor liveness or effect |
| SSE/Trace | One bounded process-local stream, explicit reset semantics, bounded/redacted frames, causal IDs preserved |
| Roster | Stable configured identities; heard/addressed TTL signals are distinct; payload sender remains reported-unverified |
| PWA | Root and `/fleet/` base-path support, shell-only cache, authenticated/mutation traffic network-only |

## Active integration proof

The launcher runs two isolated processes on Meghadharma:

```text
127.0.0.1:8871  owner fixture API
127.0.0.1:8872  Fleet Hub candidate
tmux socket      fleet-r10
tmux session     fleet-r10-candidate
```

The strict launch gate proved:

- Fleet health `ok=true`, authentication configured, and unauthenticated
  `/api/health` returns `401`;
- owner connection available with authority
  `TaskBoard+RuntimeStateStore`;
- one coherent fixture mission and three owner tasks;
- NATS connected to `DHARMA_A2A`, stream information readable, and no broker
  error after the bounded subscriptions were established;
- Chat advertised, mutation commands absent, durable resume false;
- fixture provenance and `no production effect` visible in every tab.

The 390×844 browser run opened all five tabs and the mission board. Every tab
had `scrollWidth == innerWidth`, no visible control below 44px in either
dimension, and no console, page, or request errors. See
[`evidence/r10-20260828/fleet-r10-live-browser-proof.json`](../evidence/r10-20260828/fleet-r10-live-browser-proof.json)
and the adjacent screenshots.

## NATS authority boundary

No NATS user, stream, ACL, credential, or server configuration was created or
changed. The isolated candidate reads the existing root-owned
`grok-build-a2a.env` and labels the resulting connection:

```text
transport_principal = grok_build
transport_authority = borrowed_existing_transport_only
agent_observation_subject = null
chat_subject = dharma.a2a.fleet
startup_backfill = disabled_by_transport_tier
```

Read-only ACL inspection proved that this principal can publish
`dharma.a2a.>` and `dharma.agent.*.inbox`, subscribe to `dharma.a2a.>` and
`dharma.fleet.>`, and read stream information. It cannot subscribe to broad
`dharma.agent.>` or arbitrary target ACK/reply subjects. Fleet therefore does
not request those scopes, does not show presence from them, and does not
promise semantic replies. No live publish canary was sent during this build.

The transport principal is an authorization fact, not Fleet identity.
Application envelopes retain the authenticated operator sender and their own
correlation/causation/trace axes.

## Owner boundary

The companion owner change exposes only:

```text
GET /api/control-surface/missions/{mission_id}/snapshot
```

It accepts one configured mission ID, caps the returned task set, redacts the
projection, and advertises no mutation or liveness claim. The active proof
seeds a separate state directory and never opens the production owner store.

The inspected live owner state did not provide a configured production
Mission Control mission for this build. Substituting its unscoped/stale tasks
would have manufactured authority, so the Board remains visibly fixture-backed
until a real mission is named and authorized.

## Deliberate limits

- No public deployment, AGNI/Caddy/systemd change, or release promotion.
- Board steer/assign/claim/approve/retry/move/Done controls are unavailable
  until the owner supplies atomic expected-version semantics and independent
  verification.
- Startup JetStream replay and broad agent presence are unavailable under the
  borrowed transport tier; the live process window is labeled accordingly.
- SSE resume, DM correlation, Chat history, and Trace frames are process-local.
- A stream PubAck or handler ACK is contact evidence, not proof that an agent
  was live, understood the request, or changed owner state.
- Real-device VoiceOver/background/upgrade/rollback and a separately approved
  semantic reply canary remain promotion gates.

## Builder path to production

1. Review and merge both PRs with their CI green.
2. Configure a named production Mission Control mission and the merged owner
   read endpoint; re-run the Board proof against that bounded mission.
3. Issue a least-privilege Fleet transport identity rather than borrowing
   `grok_build`; authorize only required group, inbox, receipt, and reply tiers.
4. Add owner-side atomic CAS plus independent-verifier evidence before exposing
   any Board mutation.
5. Run real-phone accessibility, reconnect, background, load, and rollback
   qualification.
6. Authorize and execute the immutable AGNI release procedure separately.

Until those gates are evidenced, the code and UI retain
`candidate-unqualified`; the active candidate remains useful without claiming
authority it does not have.
