# Fleet Hub phone prototype — local evidence

Status: **candidate-unqualified · fixture-backed local integration**
Observed: **2026-08-26**
Base commit: `94a14929a5711995b05cf587cb0b0c5e39d84320`

This packet demonstrates the repository-local phone prototype. It does not
authorize or evidence deployment, a production adapter, a live canary, owner
mutation authority, or Q1–Q10 promotion.

## What changed

- Provider-returned Pydantic instances are always revalidated at the Fleet
  boundary. A provider can no longer use `model_construct()` to smuggle a
  forged authority, command capability, discovery claim, or executor-liveness
  claim through either the projection envelope or a nested owner snapshot.
- The fixture browser build carries a persistent `SIMULATION` banner sourced
  from typed bootstrap provenance. It cannot visually present fixture state as
  production state.
- Helm now leads with one mission/decision/honest-state hero and keeps Browser,
  Hub, NATS, Owner, and Mission as separate evidence dimensions.
- Board renders the versioned `fleet.board.lanes.taskboard.v1` read projection.
  Every lane exists at zero, completed work remains Review without independent
  verification, and mutation controls remain unavailable.
- The 320px and 390px shells use a 15px root type floor, 44px interactive
  targets, safe-area ownership, and no page-level horizontal overflow. Board's
  horizontal overflow is intentionally contained inside its lane scroller.

## Browser evidence

- `evidence/prototype-20260826/fleet-hub-helm-390x844.png`
  (`e9dd0c29d68ec346b36b3c2229f5d34eef3429a9d39eb6f79349b37142ae0ae0`)
- `evidence/prototype-20260826/fleet-hub-board-running-320x844.png`
  (`99edaad8da6032242265d5afd5ddd1da7b17069b36d4d980c07f16aedd7172c5`)
- `evidence/prototype-20260826/browser-geometry.json` contains the exact viewport,
  overflow, target-size, tab-order, provenance, connection, lane, and command
  assertions.

The browser server was loopback-only at `127.0.0.1`, used
`tests.browser_demo:app`, and ran with ASGI lifespan disabled. It made no NATS,
owner, deployment, ACL, credential, or production-service contact.

## Verification

```text
uv sync --locked --group dev
  Resolved 25 packages; checked 23 packages

uv run --no-sync pytest -q
  226 passed; 1 Starlette/httpx deprecation warning

uv run --no-sync python -m compileall -q src
node --check src/static/app.js
git diff --check
  pass
```

Browser assertions:

- 390×844 Helm: 390px document width, no horizontal overflow, 15px root,
  8 visible interactive targets and none below 44px.
- 320×844 Board: 320px document width, no page overflow, 15px root,
  6 visible interactive targets and none below 44px; nine owner lanes rendered;
  one Running fixture card; commands unavailable.

## Remaining promotion limits

Mission enumeration/cut, the authenticated owner adapter, Conversation Log
Authority, live UID canary, roster observation owners, command CAS, independent
verifier trust root, real iPhone/VoiceOver witness, deployment, and rollback
rehearsal remain unproved or owner-blocked exactly as documented in the
canonical upgrades spec.
