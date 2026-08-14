# Mission: Bridge Revival + Reply ACL (Ticket #6)

**Shape:** hybrid (two-lane graph + one bounded loop in the code lane)
**Ticket:** [#6](https://github.com/AIKAGRYA/fleet-hub/issues/6)
**Why it matters:** the operator credential is DENIED reply-route subscriptions
and the Rushabdev bridge is stopped — until both are fixed, chat can send but
not hear. This is a bridge problem, surfaced honestly by the hub already.

**Where agents work:** Each writing agent in its own worktree off a fresh
`claude/bridge-revival-ticket6` branch (Lane A: runbook artifact at repo root;
Lane B: `src/hub/natsio.py`, `src/server.py`, `src/tests/`). `/home/user/dharma_swarm`
is read-only source (runbook + ACL conf). No agent touches a VPS.

**How results merge:** One integrator merges both lanes onto the branch,
re-runs the suite on the merged tree, pushes, opens a draft PR (merge is John's
tap). The runbook ships as a committed artifact in the same PR.

**Disagreement rule:** Adversarial verifier evidence (re-run tests, real diffs,
file:line citations against the runbook/ACL sources) outranks worker
self-report. A confirmed refutation returns the lane for ≤2 repair rounds; a
dispute still standing ships flagged UNRESOLVED in the PR body for John.

**Small slice first:** 6 agents (1 runbook writer, 1 code writer, 2 verifiers,
1 integrator, 1 spare); group fan-out first, defer reply-SSE routing to run 2 if
budget tightens.

**Hard gates:** merge to main · any host command (ships as runbook) · minting /
rotating / moving NATS creds — placeholders only, John handles secrets live ·
no new public surface.

## Prompt

```
Use a workflow to execute Fleet Hub Ticket #6 (bridge-revival) in repo /home/user/fleet-hub (GitHub AIKAGRYA/fleet-hub), with the dharma_swarm checkout at /home/user/dharma_swarm treated as read-only source material; first run is capped at 6 agents total (small slice — learn cost before scaling toward the 16-agent ceiling). Read /home/user/fleet-hub/FOREMAN_PROMPT.md and HANDOFF.md first. The shape is honestly a two-lane graph with one bounded inner loop, not a pipeline: Lane A (one writer) authors TICKET6_BRIDGE_REVIVAL_RUNBOOK.md at the fleet-hub repo root — a produced ARTIFACT of exact copy-paste Terminus command blocks that John runs himself, because the workflow NEVER executes host-side commands and there is no SSH from cloud sessions; derive it from /home/user/dharma_swarm/docs/ops/A2A_LIVE_WIRE_RUNBOOK.md and /home/user/dharma_swarm/scripts/ops/agni_hub_acl_ffr_d1.conf, and it must (1) give the broker-ACL change on AGNI (157.245.193.15): a fleet_hub operator user stanza granting subscribe on dharma.a2a.>, dharma.a2a.fleet.reply.>, and _INBOX.> (the operator credential is currently DENIED reply-route subscriptions — this flip is the whole ticket), obeying the conf's wildcard rule (NATS * matches one whole token; per-user JS durable consumer names must be listed exactly, read live via `nats consumer ls DHARMA_A2A`), then `nats-server --config /etc/nats/nats-server.conf -t` and `systemctl reload nats-server`; (2) give the bridge-revival sequence for the STOPPED Rushabdev bridge (167.172.95.184 — stopped after a credential exposure, so it needs fresh creds: minting or moving any secret is a JOHN-ONLY gate, use '<pw>' placeholders, zero real secrets in repo or artifact) and a health-check block for the Meghadharma bridge (178.128.87.170); and (3) end EVERY command block with a VERIFY step showing the exact check command and its EXPECTED output (e.g. `nats --user fleet_hub --password '<pw>' sub 'dharma.a2a.fleet.reply.>' --count 1` — EXPECTED: subscription accepted, was Permissions Violation), because a step without expected output is not done. Lane B (one writer) wires hub-side reply routing in src/hub/natsio.py and src/server.py: make group send (/api/send with no `to`) fan out to every active roster seat's per-agent subject from src/roster.json (dharma.a2a.hermes, dharma.a2a.rushabdev, ...) in addition to cfg.chat_subject, returning honest per-subject ack tiers where PUBLISH_ACCEPTED never renders as delivered, and extend handle_msg so replies arriving on dharma.a2a.fleet.reply.> and per-agent subjects route to the correct SSE channel (chat vs dm via state.bus.publish), with new tests in src/tests/; Lane B is a bounded fix-until-green loop — run `python3 -m pytest src/tests -q` (all 109 existing tests plus new ones) and iterate until green or until two consecutive rounds make no progress, then stop and report honestly. Because agents write in parallel, each writing agent works in its own isolated git worktree off a fresh claude/bridge-revival-ticket6 branch so edits never collide, and results merge afterward via a single integrator agent that re-runs the full suite on the merged tree. Then verify adversarially with two fresh verifier agents (one per lane) instructed to REFUTE, never trusting worker self-reports: the runbook verifier checks every command against the actual source files with file:line citations, confirms every block carries a VERIFY step, and greps the artifact for anything secret-shaped; the code verifier re-runs the test suite itself, diffs the branch to confirm the files actually changed and fan-out plus reply-routing genuinely exist in code. A CONFIRMED refutation sends that lane back to its writer for at most two repair rounds; anything still disputed after that ships flagged UNRESOLVED in the PR body with both sides' evidence for John to judge. Finish by pushing claude/bridge-revival-ticket6 and opening a DRAFT PR only — merging to main, executing anything on a VPS, minting/rotating/moving any credential, and exposing any new public surface are hard gates reserved for John — and end the PR body with a three-line note telling John exactly which Terminus steps in the runbook await his hands.
```
