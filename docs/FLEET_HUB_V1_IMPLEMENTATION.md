# Fleet Hub v1 candidate — implementation status

Status: **candidate-unqualified**
Source plan date: 2026-08-26
Implementation base: canonical `AIKAGRYA/fleet-hub` main at `94a1492`

This drop turns the researched candidate into a reviewable implementation
without manufacturing the production authorities that the research found
missing. It is useful code, but it is not yet the plan's definition of v1
complete.

## Implemented boundary

| Area | Implemented claim |
|---|---|
| Reproducibility | Python 3.11/3.12 lockfile, clean-environment test command, CI |
| Phone shell | Exactly Helm, Chat, Board, Trace, Roster; Needs-John rail/badge |
| Prototype provenance | Fixture-backed browser runs carry a persistent SIMULATION banner; local evidence cannot present as production |
| Provider trust boundary | Existing Pydantic instances and nested owner DTOs are revalidated; `model_construct()` bypasses fail closed |
| Session security | Random server-side sessions, expiry/revocation, logout, CSRF and same-origin mutation checks |
| PWA safety | Scoped manifest/service worker; shell-only cache; authenticated and command traffic network-only |
| Chat ingress | Typed group/DM intent, stable IDs, process-local caller idempotency, explicit transcript-versus-recipient route plan, namespaced `Nats-Msg-Id` within the broker dedupe window |
| SSE | One multiplexed stream, bounded replay, explicit reset/refetch when continuity is not provable |
| Mission reads | Bounded/redacted Mission Control DTO covering all nine reconciliation states |
| Needs John | Pure, deterministic, evidence-linked derivation from an owner snapshot |
| Board | Read-only capability-gated projection; unavailable is not rendered as empty |
| Trace | Bounded/redacted evidence view with transport claims kept distinct from task/effect/authority |
| Roster | Stable UID plus separately labeled, sourced, and expiring observations |
| Host posture | Loopback systemd service under an unprivileged user; immutable release switch and rollback pointer |

The exact evidence for this branch is recorded in `BUILD_RECEIPT.md` after the
integration gate completes.

The latest non-promoting phone prototype evidence is recorded in
`docs/FLEET_HUB_V1_PROTOTYPE_EVIDENCE.md`, including 390×844 and 320×844
screenshots, geometry assertions, and the complete local test result.

## Deliberately unavailable

- The authenticated read-only owner adapter now exists in code
  (`src/hub/mission_http_provider.py`, selected by
  `FLEET_HUB_MISSION_PROVIDER_URL` + `_TOKEN` + `FLEET_HUB_MISSION_IDS`; owner
  side: `dharma_swarm` `api/routers/mission_control.py`). It has been proven
  only in a local integration run (`docs/FLEET_HUB_OWNER_ADAPTER_EVIDENCE.md`).
  No production host has it configured; on AGNI the default provider still
  fails closed until the operator sets the three values in
  `/etc/dharma/fleet-hub.env` against a running owner.
- The canonical TaskBoard owner has no verified atomic expected-version
  transition. Consequently steer, assign, claim, approve, retry, move, and
  arbitrary task transition controls are disabled.
- Group transcript publication does not imply a responder cohort. Until routing
  policy and ACLs are approved, the UI reports only what was actually addressed.
- A real semantic DM canary, handler acknowledgement, owner-backed Board data,
  durable owner cursor, and production topology/presence contracts require
  external authority or owner work beyond this repository.
- The real-iPhone/standalone/VoiceOver/background matrix and production rollback
  rehearsal have not been run by source tests.

## Claim discipline

Fleet Hub models authority and evidence in its API shapes instead of relying on
copy. A snapshot identifies `TaskBoard+RuntimeStateStore` as its authority and
hard-codes `proves_executor_liveness=false`. Needs-John items carry the owner
version and evidence references but expose no command unless the owner contract
advertises one. A broker acknowledgement may establish
`contact=PUBLISH_ACCEPTED`; it cannot promote task, effect, or execution claims.

The value currently named `source_version` is a Fleet Hub projection digest,
not an owner CAS/version. It is valid for cursor invalidation and comparison;
it cannot satisfy a work-state command precondition.

This is the small type-level contribution carried into the build: an unavailable
authority remains unavailable in both API and UI, so a receipt or green screen
cannot silently widen what the system is entitled to claim.

## Promotion gates remaining

1. Review and merge this candidate branch with CI green.
2. ~~Specify and implement the authenticated read-only owner adapter.~~
   Implemented and locally evidenced (see `docs/FLEET_HUB_OWNER_ADAPTER_EVIDENCE.md`);
   production configuration against a live owner remains an operator act.
3. Add atomic TaskBoard expected-version semantics before enabling commands.
4. Obtain a least-privilege A2A/ACK/reply ACL and approve a named canary.
5. Run the real-device, accessibility, reconnect, upgrade, load, and rollback
   matrix.
6. Authorize a named immutable release separately from code approval.

Until all applicable gates are evidenced, UI and health responses retain
`candidate-unqualified` rather than claiming production-ready v1.
