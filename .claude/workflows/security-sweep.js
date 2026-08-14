export const meta = {
  name: 'security-sweep',
  description: 'Read-only security audit: one agent per surface, adversarially verify each finding, one ranked report',
  phases: [
    { title: 'Audit', detail: 'one auditor per route / render path / auth layer' },
    { title: 'Verify', detail: 'independent refuter reproduces each finding against running code' },
    { title: 'Rank', detail: 'merge confirmed findings into one severity-ranked report' },
  ],
}

// Read-only sweep: no agent writes repo files, so no worktree isolation is
// needed. Each finding still gets a FRESH independent verifier (clean context)
// told to refute it against running code — that is what keeps the graph from
// agreeing with itself. Findings without a reproduction are dropped, not ranked.

const REPO = '/home/user/fleet-hub'

// Independent surfaces. Backend routes + the auth boundary + frontend render
// paths — each audited in isolation so findings decorrelate.
const SURFACES = [
  { key: 'auth-boundary', focus: 'src/server.py auth_middleware + src/hub/auth.py — fail-closed when FLEET_HUB_TOKEN unset, allowlist exact-match vs prefix, no query-param token path, constant-time compares total on non-ASCII' },
  { key: 'route:/login', focus: 'src/server.py /login — throttle keying, 503 when unconfigured, cookie is HMAC not raw token' },
  { key: 'route:/api/send', focus: 'src/server.py /api/send + hub/natsio.send — subject injection via `to`, SSE frame-break via newlines in msg_id/text, input bounds' },
  { key: 'route:/events/stream', focus: 'src/server.py /events/stream — auth enforced, Last-Event-ID garbage tolerated, queue leak on disconnect' },
  { key: 'route:/api/health+broker+status', focus: 'src/server.py /api/health,/api/broker,/api/status,/healthz,/health — does any error path leak a secret value or internal path' },
  { key: 'route:/api/roster+presence+agent+nodes', focus: 'src/server.py /api/roster,/api/presence,/api/agent/{uid},/api/nodes — auth-gated, no injection via uid path param' },
  { key: 'route:/api/chat+dm+vision+kanban', focus: 'src/server.py /api/chat,/api/dm/{uid},/api/vision,/api/kanban — auth-gated, no disclosure' },
  { key: 'render:renderChatMsg', focus: 'src/static/app.js renderChatMsg — chat/dm text + from + subject must reach DOM via textContent, never an innerHTML sink' },
  { key: 'render:paintHealth', focus: 'src/static/app.js paintHealth — NATS/monitor/replay error strings (attacker-influenced) rendered as text only' },
  { key: 'render:paintVision', focus: 'src/static/app.js paintVision — venture names/lines/movement rendered as text only' },
  { key: 'render:paintAgents+paintNodes', focus: 'src/static/app.js paintAgents,paintNodes — display_name, subject, host, tailscale rendered as text; no attribute built from server data' },
  { key: 'render:renderRawLine', focus: 'src/static/app.js renderRawLine — raw subject + preview (attacker-published) rendered as text only' },
]

const FINDING_SCHEMA = {
  type: 'object',
  required: ['surface', 'findings'],
  properties: {
    surface: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'cls', 'location', 'trigger', 'fix'],
        properties: {
          severity: { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] },
          cls: { type: 'string', description: 'auth-bypass | injection | xss | disclosure | dos | other' },
          location: { type: 'string', description: 'file:line' },
          trigger: { type: 'string', description: 'concrete input/scenario that fires it' },
          fix: { type: 'string', description: 'one-line suggested fix' },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  required: ['confirmed', 'evidence'],
  properties: {
    confirmed: { type: 'boolean' },
    evidence: { type: 'string', description: 'the runnable command + observed output, or the source→sink trace, that reproduces it — or why it does not reproduce' },
  },
}

// Phase 1+2 pipelined: each surface's findings verify as soon as that surface
// is audited — no barrier, so a slow auditor never blocks a fast one's verify.
const perSurface = await pipeline(
  SURFACES,
  s => agent(
    `Read-only security audit of ONE surface of Fleet Hub at ${REPO}. Surface: ${s.key}. Focus: ${s.focus}. Read the cited files. Report every real finding as a row {severity, cls, location file:line, trigger (a concrete input/scenario), fix}. Hunt auth bypass, injection (subject injection, SSE frame-break, unbounded input), XSS (attacker data reaching an innerHTML/insertAdjacentHTML/outerHTML/document.write/eval sink instead of textContent), and information disclosure. Return an empty findings array if the surface is clean — that is a good result, do not invent findings.`,
    { label: `audit:${s.key}`, phase: 'Audit', schema: FINDING_SCHEMA }
  ),
  (audit, s) => audit.findings.length === 0
    ? { surface: s.key, confirmed: [] }
    : parallel(audit.findings.map(f => () =>
        agent(
          `You are a FRESH adversarial verifier with no stake in this finding. Try to REFUTE it against the running code at ${REPO}. Finding: ${JSON.stringify(f)} on surface ${s.key}. Reproduce the exact trigger — for an auth/injection finding construct the request/scope and show the actual verdict; for an XSS finding trace the specific value from source to the DOM sink and confirm the sink is unsafe. CONFIRM only with a runnable command + observed output (or a concrete source→sink trace); otherwise REJECT as not reproducible. A finding you cannot reproduce is false.`,
          { label: `verify:${s.key}`, phase: 'Verify', schema: VERDICT_SCHEMA }
        ).then(v => ({ ...f, surface: s.key, confirmed: v.confirmed, evidence: v.evidence }))
      )).then(rows => ({ surface: s.key, confirmed: rows.filter(Boolean).filter(r => r.confirmed) }))
)

const confirmed = perSurface.filter(Boolean).flatMap(r => r.confirmed || [])
const rank = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 }
confirmed.sort((a, b) => rank[a.severity] - rank[b.severity])

log(`Security sweep: ${confirmed.length} confirmed finding(s) across ${SURFACES.length} surfaces`)

// One ranked report, not N opinions. Even with zero findings this returns the
// honest "clean" summary the caller writes to SECURITY_SWEEP_REPORT.md.
const report = await agent(
  `Write the body of SECURITY_SWEEP_REPORT.md for Fleet Hub from these CONFIRMED, de-duplicated, severity-ranked findings: ${JSON.stringify(confirmed)}. For each: surface, severity, file:line, the reproducing command/scenario (evidence), and the suggested fix. Open with a one-line honest summary — "no CRITICAL/HIGH confirmed" is a valid, good result. Do not add unconfirmed findings. Output only the markdown body.`,
  { label: 'rank:report', phase: 'Rank' }
)

return { confirmed_count: confirmed.length, findings: confirmed, report }
