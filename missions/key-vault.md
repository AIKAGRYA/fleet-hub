# Mission: Key Vault + Rotation (Ticket #7 — truth amnesty)

**Shape:** graph (read-only sweep shards → merge → adversarial verify → synthesis)
**Ticket:** [#7](https://github.com/AIKAGRYA/fleet-hub/issues/7)
**Absolute rule:** never print, quote, or copy a secret VALUE — report only the
credential NAME, kind, and file:line. Repos only; never read host-side files.

**Where agents work:** Read-only against `/home/user/fleet-hub` and
`/home/user/dharma_swarm`; each writing agent (sweep workers, merge, synthesis)
writes only inside its own isolated scratch directory — no repo edits, shard
files never share a path.

**How results merge:** Each shard worker emits JSON rows (repo, path, line,
credential_name, kind, host_affinity, seat_affinity, classification); a merge
agent dedupes by credential_name + path:line into one master inventory; the
three deliverables derive solely from the verified master inventory.

**Disagreement rule:** Fresh verifiers refute using only the master inventory
and repos. A refuted shard (bogus citation, missed reference, or a leaked value)
is re-swept by a fresh agent — max two rounds; anything still disputed escalates
to John as explicit open questions.

**Small slice first:** 5 agents (shard S1 fleet-hub, shard S2 dharma_swarm
docs/ops + workflows, 1 merge, 2 verifiers). Report missed-reference rate before
scaling to shards S3–S5 (~300 more candidate files).

**Hard gates:** rotation / touching secrets (John only, ships as checklist) ·
commit/merge to main · any host action incl. reading `/etc/dharma/fleet-hub.env`
· revoking archived-seat creds (artifact only) · any broker-ACL/NATS change.

## Prompt

```
Use a workflow to run Ticket #7 truth amnesty for the key vault: a read-only credential-reference inventory across /home/user/fleet-hub and /home/user/dharma_swarm producing three artifacts for John — a per-host rotation checklist, a one-vault-file-per-host plan, and an archived-seat revocation list. ABSOLUTE RULE for every agent: never print, quote, or copy a secret VALUE; report only the credential NAME, kind, and file:line location (never echo the matched line's content — a line like TOKEN=xxxx must appear in reports as name+path+line only), and never read host-side files like /etc/dharma/fleet-hub.env — repos only. SHAPE: this is a graph — fan out sweep workers on independent shards: (S1) all of fleet-hub (known hit surface ~13 files incl. src/server.py, src/hub/auth.py, src/install_on_agni.sh, src/systemd/fleet-hub.service, DEPLOY_AGNI.md, src/tests/); (S2) dharma_swarm docs/ops/** (esp. MODEL_KEY_ROUTING.md, A2A_LIVE_WIRE_RUNBOOK.md, FLEET_FIELD_REGISTRY.yaml) plus .github/workflows/**; (S3) dharma_swarm api/, scripts/, docker-compose.yml, Dockerfile*, run_*.sh; (S4) dharma_swarm/dharma_swarm/** core; (S5) dharma_swarm remainder (inter_agent/, terminal/, hooks/, specs/, tools/, dashboard/) — each worker greps its shard with grep -rniE --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=__pycache__ 'API_KEY|_TOKEN|_SECRET|PASSWORD|CREDENTIAL|\.creds|NKEY|nkeys|BEARER|AUTHORIZATION|OPENROUTER|ANTHROPIC|GITHUB_TOKEN|FLEET_HUB_TOKEN|TAILSCALE|nats://' (expect ~346 candidate files in dharma_swarm, so use -l first then per-file line numbers) and classifies each hit as real-credential-reference vs test-fixture vs doc-mention vs false-positive; every writing agent works in its own isolated scratch directory writing only its own shard file (JSON rows: repo, path, line, credential_name, kind, host_affinity among AGNI 157.245.193.15 / Meghadharma 178.128.87.170 / Rushabdev 167.172.95.184 / GitHub-Actions / provider-cloud, seat_affinity, classification) so parallel work never collides; a merge agent then concatenates and dedupes shards into one master inventory keyed by credential_name. Findings must be adversarially verified: fresh verifier agents, told to REFUTE and given only the master inventory (never workers' self-reports), (V1) spot-check that cited file:line locations actually contain a reference to the named credential, (V2) run an independent differently-patterned sweep (e.g. add 'apikey|auth[_-]?key|private[_-]?key|BEGIN (RSA|OPENSSH)|hex\{2\}|passwd|creds?\b') hunting for references the workers MISSED, and (V3) grep all report and artifact files for anything resembling an actual secret value (long hex/base64 runs, non-placeholder text after '='); if any verifier refutes a shard, one bounded repair round re-sweeps that shard with a fresh agent using the verifier's findings as added patterns — max two rounds total, after which unresolved disagreements are listed as open questions for John, not silently resolved. Only after verification, synthesis agents derive the three artifacts from the master inventory with file:line citations on every claim: (A1) per-host rotation checklist as exact copy-paste Terminus commands John runs himself with a verification step after each (workflow NEVER executes them — no SSH from cloud sessions); (A2) one-vault-file plan proposing a single mode-600 env file per host (pattern: existing /etc/dharma/fleet-hub.env with FLEET_HUB_TOKEN, NATS_URL) enumerating which credential names consolidate into each host's file; (A3) revocation list covering the seven archived roster seats from /home/user/fleet-hub/src/roster.json — dharma-command-node, fleet-state-projector, fable_claude_code, fable_5_cursor, devin-roaming-2987d222, perplexity-computer, fable_composer — naming every credential each seat may still hold and where evidence of that holding appears, plus Rushabdev's stopped bridge exposure. HARD GATES the workflow must not cross: rotation or any touching of secret values (John only), committing/merging anything to main, running any host-side command, spending money, exposing any surface — artifacts are produced files handed to John, nothing more. SMALL SLICE FIRST: the first run is capped at shard S1 (fleet-hub) plus shard S2 (dharma_swarm docs/ops + workflows) with one worker each, one merge agent, and two verifiers (max 5 agents); report cost and inventory quality from that slice, then scale to shards S3–S5 only after the slice's verifiers pass.
```
