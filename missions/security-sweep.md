# Mission: Security Sweep (recurring)

**Shape:** graph (read-only) — one agent per surface, adversarial verify each finding, merge into one ranked list
**Saved command:** `/security-sweep` (see `../.claude/workflows/security-sweep.js`)

Hand-written to the same standard as the verified missions (its drafter hit a
structured-output cap). This is the classic **read-only sweep**: because no
agent writes repo files, no worktree isolation is needed — but each finding
still gets an independent adversarial verifier before it reaches the report.

**Where agents work:** Read-only against the merged tree. Surface agents own one
route or one render path each (routes in `src/server.py`; render paths
`renderChatMsg`, `paintHealth`, `paintVision`, `paintAgents`, `paintNodes`,
`renderRawLine` in `src/static/app.js`; the auth middleware + `src/hub/auth.py`).
Verifiers and the ranker are separate fresh agents.

**How results merge:** Each surface agent emits findings as JSON rows
(surface, severity, class, file:line, trigger, suggested fix); a ranker agent
dedupes and produces ONE severity-ranked list — not N separate opinions.

**Disagreement rule:** Every finding is checked by an independent verifier
told to REFUTE it against running code (does the auth bypass actually reach
the handler? does attacker data actually reach an innerHTML sink?) — a finding
without a reproduction is dropped, not reported. The verifier shares no context
with the finder.

**Small slice first:** cap the first run at ≤20 surfaces (there are ~15 routes +
6 render paths + auth), which covers the whole current surface; on a larger
codebase, one directory first.

**Hard gates:** none crossed — read-only, produces a report artifact only. If a
CONFIRMED vulnerability warrants a fix, that fix is a *separate* mission (likely
`/fix-until-green` after) so the sweep stays a pure audit.

## Prompt

```
Use a workflow to run a read-only security sweep of the deployed Fleet Hub surface in /home/user/fleet-hub — this is a graph (one agent per independent surface, no writes so no worktree isolation needed), and the first run is capped small-slice at every current surface (~15 routes + 6 render paths + the auth layer, well under the 20-surface cap). Fan out one auditor agent per surface: for the backend, one agent per route in src/server.py (/login, /logout, /api/session, /healthz, /health, /api/status, /api/health, /api/broker, /api/presence, /api/roster, /api/agent/{uid}, /api/nodes, /api/chat, /api/dm/{uid}, /api/vision, /api/kanban, /api/send, /events/stream, / and /index.html) plus one agent on the auth boundary (src/server.py auth_middleware + src/hub/auth.py), each hunting the same classes: auth bypass (does the middleware allowlist let this path reach a handler without a valid token? is fail-closed intact when FLEET_HUB_TOKEN is unset? any query-param token path?), injection (subject injection via /api/send `to`, SSE frame-breaking via embedded newlines in msg_id/text, unbounded input), and information disclosure (does an error path leak a secret value or an internal path?). For the frontend, one agent per render path in src/static/app.js — renderChatMsg, paintHealth, paintVision, paintAgents, paintNodes, renderRawLine — each checking whether any attacker-influenced value (agent display_name, chat/dm text, subject, NATS/monitor error strings, raw previews) can reach an innerHTML/insertAdjacentHTML/outerHTML/document.write/eval sink instead of textContent, and whether any href/attribute is built from server data. Every auditor emits findings as structured rows: {surface, severity CRITICAL|HIGH|MEDIUM|LOW, class, file:line, trigger (a concrete input/scenario), suggested_one_line_fix}. Then each finding is handed to a FRESH independent verifier agent — sharing no context with the finder — instructed to REFUTE it against running code: reproduce the exact trigger (e.g. actually construct the request/scope and show the middleware verdict, or trace the specific value from source to the DOM sink) and either CONFIRM with the runnable command + observed output or REJECT as not reproducible; a finding without a reproduction is dropped, never reported. Finally a ranker agent merges all CONFIRMED findings into ONE severity-ranked list with duplicates removed — not twenty separate opinions — and writes SECURITY_SWEEP_REPORT.md at repo root with, per finding, the surface, severity, file:line, the reproducing command/scenario, and the suggested fix, plus a one-line honest summary ("no CRITICAL/HIGH confirmed" is a valid and good result). Hard gates: this workflow only READS and produces the report artifact — it does not edit code, does not commit, does not touch secrets, does not run anything host-side; any confirmed vulnerability that warrants a fix becomes a separate mission, so the audit stays a pure audit.
```
