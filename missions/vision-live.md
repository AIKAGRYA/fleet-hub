# Mission: Vision-Live Projector (Ticket #13)

**Shape:** graph (design → parallel builders → integrate → adversarial verify) → bounded loop
**Ticket:** [#13](https://github.com/AIKAGRYA/fleet-hub/issues/13)
**Note:** this prompt is the **adversarially-fixed** variant — the first draft
capped "5 agents" while mandating 6; the verifier caught the contradiction and
corrected it to an honest 6 (arbiter spawned only on verifier conflict).

**Where agents work:** Each builder in its own worktree off `claude/vision-live-13`
(Builder A: `src/hub/vision_projector.py`, `src/tests/test_vision_projector.py`,
`src/server.py`; Builder B: `src/static/app.js`, `VERIFY_ON_AGNI.md` — disjoint).
Design agent and verifiers read-only; memos in scratch, never the repo.

**How results merge:** Integrator merges both worktrees onto the branch (disjoint
by contract; a conflict means a contract breach → back to builders), runs the
suite as the merge gate, then hands the merged tree to verifiers.

**Disagreement rule:** A reproducible failing command beats any claim; worker vs
verifier → fix round; verifier vs verifier → one fresh third arbiter; anything
still unresolved ships OPEN in the PR body.

**Small slice first:** 6 agents (1 design, 2 builders, 1 integrator, 2
verifiers; +1 arbiter only on conflict); scope to one venture ("fleet") wired
end-to-end.

**Hard gates:** merge to main · host-side verification (ships as
`VERIFY_ON_AGNI.md`) · token/creds stay on host · if receipts lack a
venture/mission key, changing what agents publish is out of scope → new ticket.

## Prompt

```
Use a workflow to close Fleet Hub ticket #13 (vision-live) in /home/user/fleet-hub: build the projector that derives per-venture "last receipted movement" and mission health from JetStream receipts so /api/vision shows real movement instead of static declarations. Shape: a graph with one bounded fix loop at the end. Node 1 (single design agent, read-only): read FOREMAN_PROMPT.md, src/server.py:419-424 (/api/vision is a bare vision.json passthrough), src/vision.json (fleet_vision.v1, 7 ventures, all missions empty), src/hub/natsio.py (replay() already demonstrates the js.get_last_msg and BY_START_TIME pull-consumer patterns to reuse), src/hub/presence.py plus /api/roster (the existing declared-file-plus-runtime-overlay precedent), and src/static/app.js paintVision (~line 645, currently "no receipted movement yet"); then write a design memo to scratch deciding: (a) the writer question — foreman vs projector; the evidence-backed default is that vision.json stays foreman-written declared intent in git while the projector computes a runtime overlay merged into the /api/vision response as fleet_vision.v2 and NEVER writes vision.json at runtime (record the decision and why); (b) receipt-to-venture mapping — each mission entry in vision.json declares match rules (subject prefixes and/or mission/venture ids expected in A2A_RECEIPTS payloads), with an honest "no receipted movement yet" when nothing matches; (c) health buckets derived from the ACK-tier ladder where only DOMAIN_RECEIPTED counts as receipted movement and PUBLISH_ACCEPTED must never render as delivered. Nodes 2-3 (parallel builders, each in its own isolated worktree so parallel edits never collide, both bound by the memo): builder A writes src/hub/vision_projector.py (pure sync core taking explicit now + payloads, async sampler over A2A_RECEIPTS and DHARMA_A2A, tolerant of malformed/unknown payloads) plus src/tests/test_vision_projector.py in the fakes style of src/tests/conftest.py, and wires it into src/server.py (/api/vision + lifespan task); builder B makes the minimal src/static/app.js paintVision change to render per-venture last-movement timestamp and health from the v2 fields (existing style.css tokens only, honest empty/degraded states, 13px phone floor) and drafts VERIFY_ON_AGNI.md, an ARTIFACT of exact copy-paste Terminus commands for John (nats stream info A2A_RECEIPTS, sample a few receipt payloads, curl -H "Authorization: Bearer $FLEET_HUB_TOKEN" https://157.245.193.15/fleet/api/vision) because cloud sessions have no SSH and the workflow never runs host-side steps itself. Node 4: an integrator merges both worktrees onto branch claude/vision-live-13 and runs cd src && python3 -m pytest tests/ -q (the existing 109 tests must still pass plus the new ones). Node 5: two fresh verifiers adversarially try to REFUTE the merged result using real evidence only — rerun the full pytest suite themselves, git diff to confirm the claimed files actually changed, feed the projector fabricated and malformed receipts, and check standing orders (no new unauthenticated surface, PUBLISH_ACCEPTED never shown as delivered, no secrets in code, page, or docs); worker self-reports count for nothing and every refutation must carry a runnable command plus its output. Then the bounded loop: each confirmed failure returns to the responsible builder's worktree and is re-verified, repeating until green or until two consecutive rounds make no progress — then stop and report the residue honestly. Disagreements resolve by evidence: a reproducible failing command beats any claim; if verifiers conflict, one third fresh verifier arbitrates, and anything still unresolved is listed as OPEN in the PR body, never papered over. Finish by pushing claude/vision-live-13 and opening a draft PR titled "vision-live (#13): receipted movement projector" whose body cites file:line for every claim and attaches VERIFY_ON_AGNI.md; merging to main, executing anything on the VPSes, and touching secrets are hard gates left to John. SMALL SLICE FIRST: cap this first run at 6 agents total (1 design, 2 builders, 1 integrator, 2 verifiers — the third arbiter verifier is spawned only if the two verifiers conflict) and scope it to one venture ("fleet") wired end-to-end — projector core, tests, /api/vision v2 overlay, minimal paint, VERIFY_ON_AGNI.md — leaving the remaining six ventures and frontend polish to a follow-up run once cost is known.
```
