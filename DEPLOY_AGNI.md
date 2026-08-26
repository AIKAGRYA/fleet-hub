# Fleet Hub candidate — AGNI qualification and deployment

This is a rollback-ready procedure, not standing authorization. Use it only for
a named, reviewed commit after the operator separately approves a production
release. Code review or a green CI run does not itself authorize installation,
service restart, ACL changes, or a live canary.

## Preconditions

- The exact commit is reviewed in `AIKAGRYA/fleet-hub` and CI is green.
- `BUILD_RECEIPT.md` identifies the commit, artifact digest, tests, and known
  unavailable capabilities.
- AGNI has Python 3.12 and `uv`; the installer builds each release environment
  from the reviewed `uv.lock`.
- The reverse proxy rejects request bodies above Fleet Hub's configured
  `FLEET_HUB_MAX_BODY_BYTES`; the application limit is defense in depth, not a
  substitute for an edge limit.
- `/etc/dharma/fleet-hub.env` exists with a non-empty token and any approved
  NATS credentials. It is root-owned, group `fleet-hub`, mode `0640`, and has
  never been copied into the release tree or a command log.
- Caddy continues to terminate TLS and reverse-proxy `/fleet/*` to loopback
  `127.0.0.1:8444`. NATS monitoring port 8222 and owner databases remain private.
- A release ID and expected source digest are recorded before transfer.

The v1 owner adapter is unavailable unless a separately reviewed authenticated
transport is configured. Do not work around that gate by copying or opening an
owner SQLite database on AGNI.

## Stage and verify

Transfer the complete reviewed repository (including `pyproject.toml`, `uv.lock`,
and `src/`) to a new staging directory. Do not overwrite the current release.
On AGNI, compare the staged tree or archive digest with the digest in the review
receipt before running any installer.

The installer rejects unsafe release IDs and existing release directories. A
normal authorized invocation is:

```bash
RELEASE_ID=<reviewed-commit-or-release-id> \
  bash <verified-staging-directory>/src/install_on_agni.sh
```

It performs these bounded mutations:

1. ensures the unprivileged `fleet-hub` service identity exists;
2. copies the staged tree into a new immutable directory under
   `/opt/dharma/fleet-hub/releases/`;
3. preserves the prior `current` target as the `previous` symlink;
4. atomically switches `current` to the new release;
5. installs the hardened systemd unit and restarts only `fleet-hub.service`;
6. checks service activity, health, the unauthenticated gate, and one
   authenticated loopback read without printing the credential.

It never deletes an existing release and never runs from tests or application
startup. The systemd unit invokes the selected release's `.venv`; it does not
use mutable host-global site packages.

## Post-install qualification

Record each result against the exact release ID:

- `/healthz` reports the expected version and `candidate-unqualified` build
  status until all promotion gates are actually complete.
- An unauthenticated application read is rejected; login creates an HttpOnly
  session; logout revokes it.
- The five destinations appear in order at 390x844: Helm, Chat, Board, Trace,
  Roster. Needs John is a rail/badge, not a sixth tab.
- Mission/Board shows `UNAVAILABLE` when the owner adapter is absent; it never
  paints a reassuring empty board.
- A staged chat test reports only the transport tier evidenced. Do not call a
  broker acceptance delivered, handled, replied, or effective.
- Background/foreground, offline shell, reconnect/reset/refetch, standalone
  mode, keyboard open, 320px reflow, reduced motion, and VoiceOver are checked
  on the approved device matrix.
- Service logs contain no token, cookie, NATS credential, raw exception, or
  unredacted payload.

A real fleet DM or group responder test is a separate approved canary. Never use
the qualification checklist as implicit permission to publish one.

## Rollback

The installer preserves `/opt/dharma/fleet-hub/previous`. If the new release
fails qualification, atomically point `current` back to that already verified
release, restart only `fleet-hub.service`, and repeat the health/auth smoke tests.
Do not delete the failed or previous release during the incident; preserve both
for comparison and receipt review.

After rollback, record the failed release ID, observed symptom, rollback target,
service timestamps, and smoke-test results. A rollback receipt proves the switch
and checks named in it; it does not prove fleet-wide semantic health.

## Promotion

Removing `candidate-unqualified` requires evidence for the owner-backed Mission
projection, atomic command semantics if enabled, least-privilege live routing,
approved semantic canary, real-device/accessibility matrix, load bounds, upgrade
behavior, and rollback rehearsal. Production presence alone is not that proof.
