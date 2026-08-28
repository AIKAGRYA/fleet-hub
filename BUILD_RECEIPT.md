# Fleet Hub R10 build receipt

Stamp: **2026-08-28 19:48 JST**
Place: **Sapporo**
Signature: **雷影 · Codex implementation lane**
Disposition: **working isolated candidate; candidate-unqualified; not deployed publicly**

## Result

Fleet Hub is running on Meghadharma as a loopback-only, authenticated
two-process integration. Helm, Chat, Board, Trace, and Roster passed a live
390×844 browser run. The owner fixture projects one mission and three tasks;
Board mutation remains fail-closed. NATS is connected on the existing bus with
its borrowed transport authority visibly constrained.

## Source receipts

- Fleet implementation branch:
  `agent/fleet-hub-working-r10-20260827`
- Implementation commit chain:
  `e1e844b` through `769324e`
- Fleet review: <https://github.com/AIKAGRYA/fleet-hub/pull/16>
- Owner implementation commits before PR closeout: `155e28825`, `149d359ca`
- Owner review: <https://github.com/AIKAGRYA/dharma_swarm/pull/1471>
- Canonical researched spec, left in its existing Meghadharma workspace:
  `/root/fleet-hub-v1/FLEET_HUB_V1_UPGRADES_SPEC.md`
- Spec SHA-256:
  `c60fc4fe54b165ed14ec73e230224f91fbdf4a4101aa809510e800936055f571`

Owner forks remain review inputs; none was silently selected as canonical.

## Runtime receipt

```text
host                    meghadharma-cloud (178.128.87.170)
scope                   loopback only
owner                   127.0.0.1:8871
fleet                   127.0.0.1:8872
tmux socket/session     fleet-r10 / fleet-r10-candidate
evidence_mode           fixture
source_instance         meghadharma-loopback-r10
production_effect       false
mission                 fleet-hub-r10-local
owner tasks             3
mission commands        unavailable
NATS stream             DHARMA_A2A
stream message count    200000 (bounded stream state at proof time)
chat subject            dharma.a2a.fleet
transport principal     grok_build
transport authority     borrowed_existing_transport_only
agent observation       unavailable (no dharma.agent.> subscription)
startup replay          disabled_by_transport_tier
semantic reply promise  false
```

Post-launch health returned `ok=true`, `broker.connected=true`, readable stream
state, and `broker.error=null`. An unauthenticated health request returned
`401`. The root-only token and curl configuration were not printed or
committed.

No Caddy, systemd, AGNI, NATS server/stream/ACL, or credential file was changed.
No live NATS publish canary was sent.

Read-only AGNI service inspection found the active v0.6 unit and its environment
and broker configuration paths, but did not establish its effective NATS
principal or ACLs. Credential portability and production Fleet transport
authority therefore remain **unknown**.

## Verification receipt

- `uv run --no-sync pytest -q` — **271 passed**, one upstream Starlette
  deprecation warning.
- `uv run --no-sync ruff check src scripts` — pass.
- Python compile, JavaScript syntax, Bash syntax, ShellCheck, `git diff --check`,
  and staged gitleaks — pass.
- Fleet PR CI run
  <https://github.com/AIKAGRYA/fleet-hub/actions/runs/33164426070> — Python 3.11
  and 3.12 pass.
- Live browser JSON — five tabs, 390×844, no horizontal page overflow, no
  undersized visible target, no browser error.

## Evidence SHA-256

```text
c42ad91b9d3354c6819f64ae21ab3ba3056557db93325ee49b06e6b008784546  fleet-r10-live-board-390x844.png
461b7a9ce8dcb3fce353e9d689ea23568a8b6d129470475f3b33cf13c7283b2f  fleet-r10-live-browser-proof.json
ef433be428080f866299536f303320d69cc4d93fd44373f3d7e258efe3626941  fleet-r10-live-chat-390x844.png
ed1aac23dbeb1450fdc89fb66e2bd34856209f709a627726c4e4679223439b42  fleet-r10-live-helm-390x844.png
3a0f3e2f250d7af064b5559ecb8b18bd0b2cd942c47712235598dc78d642c341  fleet-r10-live-roster-390x844.png
0fa8198ac921a94df0857cc768186dae359942c8e91c6da5e7da7a20016e6b7c  fleet-r10-live-trace-390x844.png
47852955d19c8c988815aefbff8e99e41fe0e3df2de0ea8ca4deda425ed7fd21  a2a-identity-receipt.json
```

All listed Fleet evidence lives under `evidence/r10-20260828/`.

## Claims intentionally not made

- The fixture Board is not production fleet work.
- NATS connection, PubAck, handler ACK, a card, a receipt, or a heartbeat is not
  executor liveness or task effect.
- An empty Needs-John projection is scoped to the successful configured owner
  read, not fleet-wide discovery.
- PR/CI success is review evidence, not deployment authority.
- A working borrowed transport on Meghadharma is not evidence of authorization
  to reuse AGNI's production identity or credentials.
- This receipt does not qualify the candidate as production or world-class by
  self-assertion; it records challengeable code, runtime, and pixel evidence.
