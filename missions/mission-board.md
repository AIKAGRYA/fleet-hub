# Mission: Mission Schema + Board (Ticket #4)

**Shape:** graph (contract → parallel builders → adversarial verify) → bounded fix loop
**Ticket:** [#4](https://github.com/AIKAGRYA/fleet-hub/issues/4)
**Principle:** the board is a projection of receipts, never a hand-edited list —
so it can never drift from truth (ADR-011).

**Where agents work:** Each builder in its own worktree; ownership is
contract-bound and disjoint — Builder A: `src/hub/missions.py` + `src/missions.json`
+ `src/tests/test_missions.py`; Builder B: `src/server.py` +
`src/tests/test_missions_routes.py`; Builder C: `src/static/` (app.js, style.css,
index.html). Contract author and verifiers are read-only.

**How results merge:** Worktrees merge onto `claude/mission-board-ticket-4`;
disjoint ownership makes merges conflict-free — any edit outside a builder's
owned files is reverted. Verify + fix loop run on the merged branch.

**Disagreement rule:** Fresh verifiers judge only evidence they produced (pytest
they ran, diffs they read). Verifier evidence beats builder self-report; if the
contract and code disagree, running code + FOREMAN_PROMPT.md ground truth wins.

**Small slice first:** contract author → 1 adversarial contract reviewer →
Builder A only → 1 verifier who independently runs the suite; Builders B and C
launch only after that verifier fails to refute.

**Hard gates:** merge to main · host-side deploy of new endpoints (ships in
`NEEDS_JOHN.md`) · if receipts projection needs broker/ACL changes, that's out
of scope → Needs-John, not code.

## Prompt

```
Use a workflow to resolve Fleet Hub ticket #4 (mission schema + board as receipts-projection) in /home/user/fleet-hub, shaped as a graph of contract -> parallel builders -> adversarial verification, ending in a bounded fix loop. First read FOREMAN_PROMPT.md, HANDOFF.md, src/server.py, src/hub/natsio.py, src/vision.json, and src/static/app.js (the paintVision/needs-john section) so every agent works from code truth, not prose. Step 1 (single contract author): write MISSION_CONTRACT.md at repo root defining (a) missions.json schema fleet_missions.v1 with fields id, venture (must be a venture id present in src/vision.json: fleet, darshan, trading, sab, tam, rsi, mech-interp), intent, status enum (proposed|active|blocked|done|abandoned), gates[] (id, kind restricted to the five hard gates: merge-main, spend, secrets, scope, public-surface; question, state open|approved|denied, raised_ts), agents[] (roster uids from src/roster.json), receipts[] (projection pointers only: stream, seq, ts, ack_tier from NO_CONTACT->PUBLISH_ACCEPTED->DELIVERED_TO_CONSUMER->HANDLER_ACKED->DOMAIN_RECEIPTED — PUBLISH_ACCEPTED must never render as delivered); (b) read-only endpoint signatures GET /api/missions (missions joined with vision.json ventures), GET /api/missions/{id}, GET /api/needs-john (open gates queue) — all behind the existing auth_middleware (they are /api/* so the token gate already covers them; no new public surface), no endpoint that mutates mission state by prose since boards project receipts; and (c) a file-ownership map making builders disjoint: Builder A owns src/hub/missions.py (projection: fold receipt envelopes into mission state) + src/missions.json seed fixture + src/tests/test_missions.py; Builder B owns src/server.py wiring + src/tests/test_missions_routes.py; Builder C owns src/static/app.js + src/static/style.css + src/static/index.html (Now-screen Needs-John queue in botan pink via a style.css token, phone floor 13px, honest empty state when no missions exist). Step 2: a fresh agent adversarially reviews the contract, told to REFUTE it against src/server.py and FOREMAN_PROMPT.md ground truth (real streams are DHARMA_A2A/A2A_INBOX/A2A_TASKS/A2A_DLQ/A2A_RECEIPTS; DS_* do not exist). SMALL SLICE FIRST: on this first run, launch only Builder A after the contract passes review, then one adversarial verifier — a fresh agent that must try to refute Builder A's claim by independently running cd src && python3 -m pytest tests/ -q and checking git diff shows the owned files actually changed; only if refutation fails, fan out Builders B and C in parallel. Each builder that writes files works in its own isolated git worktree (copy of the repo) so parallel edits never collide, and results merge afterward onto a single claude/mission-board-ticket-4 branch; merge conflicts are resolved by the contract's ownership map (an edit outside a builder's owned files is reverted, not merged). After merge, two fresh adversarial verifiers work in parallel: one re-runs the full suite (cd src && python3 -m pytest tests/ -q, previously 109 tests — all pre-existing tests must still pass) and greps the diff for secrets/tokens; the other refutes UI honesty claims (empty/degraded states designed, PUBLISH_ACCEPTED never shown as delivered, all fetch paths under /fleet). Verifiers judge only real evidence — test output they ran themselves, actual file diffs — never a worker's self-report; where a verifier and a builder disagree, the verifier's reproduced evidence wins and the item goes back as a defect. Then a bounded fix loop, honestly a loop: fix failing tests or confirmed defects and re-verify, until the suite is green and both verifiers fail to refute, or two consecutive rounds make no progress — then stop and report the residue truthfully. Hard gates are never taken inside the workflow: do not merge to main, do not touch secrets, do not expose new surfaces; instead produce ARTIFACTS — the pushed claude/mission-board-ticket-4 branch, a draft-PR body citing file:line evidence and test counts, and a NEEDS_JOHN.md checklist of exact taps and copy-paste Terminus commands (if any) for John. Finish by drafting the one-line decision entry for wayfinder issue #2.
```
