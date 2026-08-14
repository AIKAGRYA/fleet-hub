# Mission Library

Ready-to-paste **dynamic-workflow** mission prompts for the Foreman. Each file
is one mission: paste its `Prompt` block into a Claude Code session (with the
fleet-hub repo, plus dharma_swarm read-only where noted) to launch the workflow.

Every prompt was drafted and **adversarially verified** by a workflow before it
landed here (see the session that produced them). They all obey the discipline
in `../FOREMAN_PROMPT.md`:

- **Shape is honest** — a graph only when steps are genuinely independent;
  otherwise a bounded loop with a stop condition.
- **Verification is adversarial** — fresh agents told to *refute*, judging real
  evidence (a test that actually ran), never a worker's self-report.
- **Writers are isolated** — each writing agent gets its own git worktree;
  results merge afterward. Read-only sweeps need no isolation.
- **Small slice first** — the first run is capped; learn cost in `/workflows`
  before scaling.
- **Hard gates never crossed** — merge to main, spend, secrets, scope, new
  public surface all stop for John; host-side steps ship as copy-paste
  artifacts, never executed by the workflow (no SSH from cloud sessions).

| Mission | Ticket | Shape | First-run cap |
|---|---|---|---|
| `heartbeat-contract.md` | #3 (frontier) | hybrid graph→loop | 6 agents, AGNI only |
| `bridge-revival.md` | #6 | two-lane graph + loop | 6 agents |
| `key-vault.md` | #7 | graph (read-only) | 5 agents, 2 shards |
| `mission-board.md` | #4 | graph→loop | contract + Builder A + verifier |
| `vision-live.md` | #13 | graph→loop | 6 agents, 1 venture |
| `security-sweep.md` | recurring | graph (read-only) | ≤20 surfaces |
| `fix-until-green.md` | recurring | loop | runner, then ≤2 fixers |

`security-sweep` and `fix-until-green` are also saved as reusable commands in
`../.claude/workflows/` — run `/security-sweep` and `/fix-until-green` directly.

Recommended order mirrors the build sequence in `../FOREMAN_PROMPT.md`:
heartbeat → bridge-revival → key-vault → mission-board → vision-live, with
security-sweep and fix-until-green run on demand any time.
