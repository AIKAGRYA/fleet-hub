export const meta = {
  name: 'fix-until-green',
  description: 'Bounded fix-until-green loop on the Fleet Hub test suite — honest loop, not a graph',
  phases: [
    { title: 'Run', detail: 'run the suite, capture the failing set' },
    { title: 'Fix', detail: 'one fixer per failing test-file group, isolated worktrees' },
    { title: 'Verify', detail: 'fresh agent re-runs the suite and refutes test-weakening' },
  ],
}

// This is honestly a LOOP: round N depends on round N-1's failures, so there is
// nothing to parallelize across rounds. Within a round, fixers on disjoint
// test-file groups run in parallel in isolated worktrees so edits never collide.
// Nothing merges to main — the final artifact hands John the merge command.

const SRC = '/home/user/fleet-hub/src'
const BRANCH = 'claude/fix-until-green'
const MAX_ROUNDS = 4
const TEST_FILES = ['test_auth.py', 'test_natsio.py', 'test_presence.py', 'test_server_routes.py', 'test_state_bus.py']

const RUN_SCHEMA = {
  type: 'object',
  required: ['green', 'summary', 'failing'],
  properties: {
    green: { type: 'boolean', description: 'true iff pytest exit code was 0' },
    summary: { type: 'string', description: 'the exact pytest tail summary line' },
    failing: { type: 'array', items: { type: 'string' }, description: 'failing test ids, grouped-able by file' },
  },
}

const VERIFY_SCHEMA = {
  type: 'object',
  required: ['green', 'failing_count', 'weakening_detected', 'notes'],
  properties: {
    green: { type: 'boolean' },
    failing_count: { type: 'integer' },
    weakening_detected: { type: 'boolean', description: 'true if the diff adds skip/xfail, deletes an assert, or broadens an except to go green' },
    notes: { type: 'string' },
  },
}

function runRound(round) {
  return agent(
    `Round ${round} runner for the Fleet Hub fix-until-green loop. Run exactly: cd ${SRC} && python3 -m pytest tests/ -q . Report green=true iff exit code 0, the exact tail summary line, and the list of failing test ids. Do not fix anything — you only measure. (The suite was 109 passing at v0.6 merge, so instant-green is a real and good outcome.)`,
    { label: `run:round${round}`, phase: 'Run', schema: RUN_SCHEMA }
  )
}

let baseline = null   // prior verifier-confirmed failing count, for the no-progress stop
let noProgress = 0
let last = null

for (let round = 1; round <= MAX_ROUNDS; round++) {
  const run = await runRound(round)
  last = run
  if (run.green) {
    log(`Round ${round}: green — ${run.summary}`)
    break
  }
  log(`Round ${round}: red — ${run.summary}`)

  // Round 1 small-slice: cap at 2 fixers / 3 tests to learn cost. Later rounds
  // widen to one fixer per failing test-file group (still bounded).
  const groups = TEST_FILES.filter(tf => run.failing.some(id => id.includes(tf)))
  const cappedGroups = round === 1 ? groups.slice(0, 2) : groups

  await parallel(cappedGroups.map(tf => () =>
    agent(
      `Fixer for round ${round}, failure group ${tf}, in your OWN isolated git worktree off ${BRANCH} at /home/user/fleet-hub so parallel edits never collide. Fix the failing tests in tests/${tf}: fix real code in src/hub/, src/server.py, or src/static/, OR fix a genuinely wrong test. You must NEVER delete, skip, xfail, or weaken an assertion to go green, never loosen auth semantics, never touch secrets or files outside the repo. If a failure needs a live NATS broker / systemd / Caddy on a VPS, do NOT attempt it — report it as environment-blocked with the exact Terminus command John would run. Merge your worktree's diff onto ${BRANCH} when done.`,
      { label: `fix:round${round}:${tf}`, phase: 'Fix', isolation: 'worktree' }
    )
  ))

  // Fresh adversarial verifier: re-runs the suite itself and inspects the diff
  // for test-weakening. Only its measured count advances the baseline.
  const v = await agent(
    `Fresh adversarial verifier for round ${round}. On branch ${BRANCH} at /home/user/fleet-hub, re-run cd ${SRC} && python3 -m pytest tests/ -q yourself and report green + the failing count from YOUR run (never the fixers' self-report). Then run git diff against the round-start state and set weakening_detected=true if any fix added a skip/xfail, deleted an assertion, or broadened an except to pass. Note any environment-blocked tests separately.`,
    { label: `verify:round${round}`, phase: 'Verify', schema: VERIFY_SCHEMA }
  )
  last = { green: v.green, summary: `verifier: ${v.failing_count} failing${v.weakening_detected ? ' (WEAKENING DETECTED — reverted)' : ''}`, failing: [] }

  if (v.green && !v.weakening_detected) {
    log(`Round ${round}: verifier confirms green`)
    break
  }
  // No-progress stop: two consecutive rounds without the failing count dropping.
  if (baseline !== null && v.failing_count >= baseline) {
    noProgress += 1
    log(`Round ${round}: no progress (${v.failing_count} >= ${baseline}), streak ${noProgress}`)
    if (noProgress >= 2) { log('Stopping: two rounds with no progress.'); break }
  } else {
    noProgress = 0
  }
  baseline = v.failing_count
}

// Artifact only — merging to main is John's tap.
return {
  final: last?.summary || 'no run',
  green: !!last?.green,
  branch: BRANCH,
  merge_command: `git -C /home/user/fleet-hub checkout main && git merge --no-ff ${BRANCH}`,
  note: 'Merging to main is John’s tap. Any environment-blocked test ships as a Terminus command in the round report.',
}
