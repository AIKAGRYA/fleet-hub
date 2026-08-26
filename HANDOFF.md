# Fleet Hub v1 candidate — implementation handoff

Canonical repository: <https://github.com/AIKAGRYA/fleet-hub>

## Start here

The maintained implementation is in `src/`. It is a modular FastAPI service
and zero-build PWA, not the older single-file AGNI deployment and not a
`dharma_swarm` dashboard.

Read in this order:

1. `docs/FLEET_HUB_V1_IMPLEMENTATION.md` — current implementation and blockers
2. `CLAUDE.md` — authority, security, and product invariants
3. `src/README.md` — runtime/API details
4. `DEPLOY_AGNI.md` — qualification procedure; not standing deploy authority

## Authority boundary

| Concern | Owner | Fleet Hub role |
|---|---|---|
| Task state | TaskBoard | Read projection; commands only when owner contracts exist |
| Attempts, leases, runtime receipts | RuntimeStateStore | Read through Mission Control |
| Joined mission truth | Mission Control | Validate, redact, render, and label freshness |
| Message storage/transport | governed NATS/JetStream fabric | Canonical chat intent ingress and bounded projection |
| Roster identity | canonical roster/config owner | Render stable UID and provenance |
| Needs John | deterministic query over owner state | Derive; never persist as a second queue |
| UI route and drafts | browser memory | Ephemeral client state only |

The production owner-service transport is intentionally not guessed. With no
adapter, mission reads return a typed unavailable response rather than an empty
board. Board controls remain disabled because the owner lacks an atomic
expected-version transition.

Fleet Hub's current `source_version` is a deterministic projection digest. It
is useful for pagination and invalidation, but it is not an owner CAS/version
and must never authorize a mutation.

## What another implementer may safely continue

- Add an authenticated, read-only Mission Provider transport without granting
  filesystem/database access to Fleet Hub.
- Add a canonical owner command only after its authorization, atomic versioning,
  idempotency, receipt, and error semantics are reviewed together.
- Strengthen bounded trace, topology, and route projections from allowlisted
  owner APIs.
- Run the real iPhone/Safari, VoiceOver, reconnect, background, and standalone
  qualification matrix.

Do not invent a local kanban, infer liveness from receipts, flatten group
transcript publication into responder fan-out, or display a command as executed
because an ingress layer accepted it.

## Local gate

```bash
uv sync --locked --group dev
uv run --no-sync python -m compileall -q src
uv run --no-sync pytest -q
git diff --check
```

The PWA can then be exercised locally with a disposable test token as documented
in `README.md`. Production installation is a later, named operator action.
