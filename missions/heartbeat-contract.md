# Mission: Heartbeat Contract (Ticket #3 — the frontier)

**Shape:** hybrid (short graph → bounded fix-until-green loop)
**Ticket:** [#3](https://github.com/AIKAGRYA/fleet-hub/issues/3)
**Why it's first:** until agents publish liveness, every presence dot is
inferred from traffic, not evidenced. This is the highest-leverage change.

**Where agents work:** Writing agents each get their own isolated git worktree
branched off `claude/heartbeat-contract` (Builder A owns `src/heartbeat/`,
`src/systemd/fleet-heartbeat.service`, `JOHN_RUNBOOK_HEARTBEAT.md`; Builder B
owns `src/hub/natsio.py`, `src/hub/presence.py`, `src/tests/`). Designer and
verifiers are read-only against the merged tree.

**How results merge:** One integrator merges both worktrees onto
`claude/heartbeat-contract` (file sets disjoint by construction), then the
fix-until-green pytest loop runs only on the merged tree; verifiers examine only
the merged tree.

**Disagreement rule:** A verifier refutation backed by a runnable command +
observed output outranks any builder self-report; unverifiable claims are
treated as false. Builders get one repair round, then anything still disputed is
reported to John as an open item — never argued into green.

**Small slice first:** 6 agents (1 designer, 2 builders, 1 integrator/loop
runner, 2 verifiers); AGNI-only runbook; defer multi-host copies and UI polish.

**Hard gates:** merge to main (John's tap) · host install (ships as
`JOHN_RUNBOOK_HEARTBEAT.md`) · secrets (creds only host-side env, verifiers grep
for leaks) · no new public surface.

## Prompt

```
Use a workflow to build Ticket #3 (heartbeat contract) in /home/user/fleet-hub on branch claude/heartbeat-contract — hard gates the workflow must NEVER cross: no merge to main, no SSH to any VPS, no secrets in repo or page, no new public surfaces; anything host-side becomes a copy-paste artifact for John. The shape is hybrid: a short graph feeding a bounded fix-until-green loop. Step 1, one design agent reads FOREMAN_PROMPT.md, src/hub/natsio.py, src/hub/presence.py, src/hub/state.py, src/roster.json, and src/tests/, then writes a one-page contract file docs/HEARTBEAT_CONTRACT.md: subject dharma.fleet.heartbeat, JSON payload {uid, state, task, ts, host:{load1, mem_pct, disk_pct}}, ~30s cadence, and the hub rule that a valid heartbeat sets real last_heard so a dead agent goes red within one interval — heartbeat-bearing agents get a tight freshness window (~90s = 3 missed beats) while non-heartbeat agents keep the existing 300s/7200s traffic-inference behavior in presence.freshness unchanged. Step 2 fans out two builders, each writing agent working in its own isolated git worktree so parallel edits never collide: Builder A writes the standalone publisher src/heartbeat/heartbeat_publisher.py (~20 lines, stdlib + nats-py only; uid and creds only from env FLEET_UID/NATS_URL/NATS_USER/NATS_PASSWORD — zero secrets in the file), a systemd unit src/systemd/fleet-heartbeat.service, and JOHN_RUNBOOK_HEARTBEAT.md with exact Terminus copy-paste install + verify commands (verify = nats sub 'dharma.fleet.heartbeat' on AGNI, then curl the hub /fleet/api/agents and see last_heard move); Builder B extends src/hub/natsio.py (subscribe dharma.fleet.heartbeat in nats_loop; route it through handle_msg or a new handle_heartbeat that updates state.presence and publishes the existing SSE presence event) and src/hub/presence.py per the contract, plus tests: new src/tests/test_heartbeat.py and updates to test_presence.py/test_natsio.py in the existing injected-clock, no-real-network style. Step 3, one integrator merges both worktrees onto claude/heartbeat-contract (file sets are disjoint), then runs the honest loop: python3 -m pytest src/tests -q (109 tests pass today; all old + new must pass), fix, repeat until green or until two consecutive rounds make no progress — then stop and report the stuck state truthfully instead of polishing prose. Step 4, two fresh verifier agents adversarially try to REFUTE the result using only the merged tree, never the builders' self-reports: re-run pytest themselves and paste the tail, count the publisher's lines and grep it for any hardcoded credential, drive a simulated heartbeat then silence through the injected clock and prove freshness flips to stale/red within one interval, and prove a non-heartbeat agent's presence behavior is byte-identical to before; any claim lacking a runnable command plus observed output is treated as false, verifier refutation outranks builder claims, and one repair round is allowed before a final re-verify. First run is capped small-slice: at most 6 agents total, and Builder A ships the runbook for AGNI only (the other two Hermes hosts are a copy-edit in a later run) — report tokens/cost and outcome before scaling. Final deliverables: green suite on claude/heartbeat-contract, docs/HEARTBEAT_CONTRACT.md, JOHN_RUNBOOK_HEARTBEAT.md, and a phone-length summary naming the two taps John owns (merge to main; running the runbook on the hosts).
```
