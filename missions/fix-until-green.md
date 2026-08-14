# Mission: Fix Until Green (recurring)

**Shape:** loop (honest — each round depends on the last round's test results)
**Saved command:** `/fix-until-green` (see `../.claude/workflows/fix-until-green.js`)

This is the canonical **loop, not graph**: there is nothing to parallelize
across rounds because round N needs round N−1's failures. Within a round,
fixers on disjoint test-file groups can run in parallel.

**Where agents work:** Fixer agents each get an isolated worktree (one per
failure group, grouped by test file); runner and verifier are read-only against
the round's merged tree.

**How results merge:** Fixer diffs merge onto `claude/fix-until-green`; the
verifier re-runs the suite on the merged tree, and only verifier-confirmed state
becomes the next round's baseline. Nothing merges to main.

**Disagreement rule:** The fresh verifier's own pytest run is authoritative over
any fixer self-report; a fix that shows test-weakening (skip/xfail/deleted
assert/broadened except) is reverted and counts toward the no-progress stop.

**Small slice first:** round 1 spawns only the runner (stop if already green);
if red, ≤2 fixers on ≤3 tests, then a cost report before widening (≤6 agents/
round, ≤4 rounds).

**Hard gates:** merge to main (ships the exact merge command as artifact) ·
host/VPS action (any failure needing live NATS/systemd/Caddy → Terminus artifact,
excluded from no-progress math) · secrets (never read/written/moved).

## Prompt

```
Use a workflow to run a bounded fix-until-green loop on the Fleet Hub repo at /home/user/fleet-hub — this is honestly a loop, not a graph, because every round depends on the previous round's test results. Round structure: (1) a runner agent executes `cd /home/user/fleet-hub/src && python3 -m pytest tests/ -q` and records the exact tail summary (pass/fail counts plus the failing test ids) as the round's baseline evidence; if the exit code is 0 the loop stops immediately and reports green (the suite was 109 passing at v0.6 merge, so an instant-green round 1 is a real possible outcome, not a failure of the workflow). (2) If red, group failures by test file (src/tests/test_auth.py, test_natsio.py, test_presence.py, test_server_routes.py, test_state_bus.py) and dispatch fixer agents, each in its own isolated git worktree so parallel edits never collide, one worktree per failure group, scope limited to src/hub/, src/server.py, src/static/, and src/tests/; fixers must fix code or fix a genuinely wrong test — never delete, skip, xfail, or weaken an assertion to go green, never loosen auth semantics, never touch secrets or files outside the repo. (3) Merge the fixer worktrees onto one branch `claude/fix-until-green`, then a fresh verifier agent — instructed adversarially to REFUTE the claim that the round improved anything — re-runs the full suite itself on the merged tree, diffs the failing-test set against the round-start baseline, and inspects the actual git diff for test-weakening (new skips, deleted asserts, broadened excepts); only the verifier's own pytest exit code and summary count as evidence, never a fixer's self-report, and if verifier and fixer disagree the verifier wins and the disputed fix is reverted before the next round. Stop conditions, whichever comes first: suite green, OR two consecutive rounds where the verifier-measured failure count does not decrease, OR 4 rounds total. Small slice first: round 1 spawns only the single runner (stop if green), and if red caps at 2 fixer agents addressing at most 3 failing tests, then reports observed cost/time before widening to at most 6 agents per round. Hard gates the workflow must never cross: do not merge to main — leave all work on `claude/fix-until-green` and finish by producing an artifact, a phone-length report containing the final pytest summary, `git diff --stat`, and the exact merge command for John (merging is his tap); if any failure requires host-side action on AGNI/Meghadharma/Rushabdev (e.g. a live NATS broker), do not attempt it — mark those tests environment-blocked in the report with exact Terminus copy-paste commands for John, and exclude them from the no-progress calculation so the loop cannot spin on unfixable failures.
```
