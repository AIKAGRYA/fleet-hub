# Fleet Hub — Complete Build Spec for Outsourcing

## 1. PROJECT SUMMARY

Build a phone-first web application that serves as the primary fleet command center for a multi-agent AI system. The app must replace Telegram as the primary interface for an operator (John) to interact with, monitor, and steer a fleet of AI agents across 3 VPS nodes connected via NATS/JetStream.

**URL:** https://157.245.193.15/fleet/
**Host:** AGNI VPS (157.245.193.15), Ubuntu, root access available
**Current state:** v0.4 prototype live but non-functional (messages don't reach agents, no agent replies, basic UI)

## 2. THE VISION (from operator, verbatim)

> 1. There should be a fleet group chat where every agent can respond if available as well as collab with each other. I should also be able to click on each agent, open it into its own 'home page' with agent card available, location, nats address, models available etc, its 'meishi' and then be able to converse one on one to each agent in private windows just like now in telegram. Full slack message options as well.
> 2. Full semantic response, just like I'm talking to a low latency LLM directly.
> 3. I mean interject and steer into an ongoing multi agent group chat that I can see. Like the nats jetstream, now I can't see at all, but imagine I issue a long campaign with collaboration between multiple agents. I want to be able to see all of that transparently and then be able to interfere or steer when I need to.
> 4. It should be able to replace telegram but I will still use telegram so not replace completely.

## 3. INFRASTRUCTURE

### 3.1 Three VPS Nodes (all have root SSH)

| Node | Public IP | Tailscale IP | Role |
|---|---|---|---|
| Rushabdev | 167.172.95.184 | 100.113.248.117 | HTTPS gateway, dashboard, operator proxy |
| AGNI | 157.245.193.15 | 100.79.111.89 | NATS hub, Fleet Hub server, SAB |
| Meghadharma | 178.128.87.170 | 100.103.106.70 | Semantic bridge, Global A2A build, one-pane |

### 3.2 NATS/JetStream

- **Hub:** AGNI VPS, `nats://157.245.193.15:4222` (auth required)
- **Stream:** `DHARMA_A2A` — covers subjects `dharma.a2a.>` and `dharma.fleet.chat`
- **Monitoring:** `http://127.0.0.1:8222` (localhost only on AGNI)
- **WSS:** `wss://157.245.193.15:8443` (Caddy-proxied with LE cert)
- **Fleet chat subject:** `dharma.fleet.chat`
- **Per-agent subjects:** `dharma.a2a.<callsign>` (see roster below)

### 3.3 Caddy Reverse Proxy (on AGNI)

```
handle_path /fleet/* {
    reverse_proxy localhost:8444
}
```

Caddy strips the `/fleet/` prefix. So `/fleet/api/roster` → backend receives `/api/roster`. The frontend JS must use `/fleet/` as the API base path for all fetch calls and SSE connections.

### 3.4 Current Fleet Hub Server

- **Location:** `/root/agni/fleet_hub/server.py` on AGNI
- **Static files:** `/root/agni/fleet_hub/static/index.html`
- **Systemd service:** `fleet-hub.service` (port 8444, localhost)
- **Python:** `/usr/bin/python3.12` with `nats-py`, `fastapi`, `uvicorn`, `pydantic`
- **Wayfinder:** `claude-wayfinder v2.0.0` installed via pip
- **Catalog:** `/root/agni/fleet_hub/dispatch-catalog.json`

## 4. AGENT ROSTER (10 agents)

| UID | Callsign | Display Name | NATS Subject | Host | Model | Status |
|---|---|---|---|---|---|---|
| agni-hermes | hermes | AGNI Hermes | dharma.a2a.hermes | AGNI 157.245.193.15 | glm-5.2 (zai) | live |
| rushabdev | rushabdev | Rushabdev Hermes | dharma.a2a.rushabdev | Rushabdev 167.172.95.184 | glm-5.2 (ollama-cloud) | live |
| meghadharma-hermes | fleet.reply.meghadharma_hermes | Meghadharma Hermes | dharma.a2a.fleet.reply.meghadharma_hermes | Meghadharma 178.128.87.170 | kimi-k3 (kimi_code) | live |
| dharma-command-node | fleet | Command Node | dharma.a2a.fleet | Rushabdev 167.172.95.184 | — | live |
| fleet-state-projector | fleet.reply.availability_status | Fleet State Projector | dharma.a2a.fleet.reply.availability_status | Meghadharma 178.128.87.170 | — | live |
| fable_claude_code | claude | Fable Claude Code | dharma.a2a.claude | Mac | claude (anthropic) | offline |
| fable_5_cursor | fable_5_cursor | Fable Cursor | dharma.a2a.fable_5_cursor | Mac | cursor | offline |
| devin-roaming-2987d222 | devin | Devin | dharma.a2a.devin | Devin Cloud | devin (cognition) | offline |
| perplexity-computer | perplexity | Perplexity Computer | dharma.a2a.perplexity | Perplexity Cloud | perplexity | offline |
| fable_composer | fable_composer | Fable Composer | dharma.a2a.fable_composer | Mac | — | offline |

### Agent Card Fields (for home pages)

Each agent needs a full card page showing:
- Display name, role, bio paragraph
- Host IP, Tailscale IP
- NATS subject, callsign
- Model, provider
- Meishi (identity mark name)
- Capabilities (tag pills)
- Live/dead status (from NATS consumer heartbeat age)
- Direct message chat area

### Meishi values:
- agni-hermes: `agni_infrastructure_anchor`
- rushabdev: `rushabdev_revenue_router`
- meghadharma-hermes: `meghadharma_hub_bridge`
- dharma-command-node: `command_node_console`
- fleet-state-projector: `fleet_state_projector`
- fable_claude_code: `fable_claude_code`
- fable_5_cursor: `fable_5_cursor`
- devin-roaming-2987d222: `devin_roaming`
- perplexity-computer: `perplexity_computer`
- fable_composer: `fable_composer`

## 5. WAYFINDER DISPATCH MATCHER

### 5.1 What It Is

`claude-wayfinder v2.0.0` (pip package, by Matt Pocock / glitchwerks on GitHub) is a deterministic, auditable dispatch matcher. Instead of an LLM guessing which agent should handle a task, Wayfinder scores a structured "dispatch context" against a catalog of agents/skills and returns one of 7 typed decisions.

### 5.2 The 7-Decision Contract

| Decision | Meaning |
|---|---|
| `delegate` | Route to the best-matching agent (confidence ≥ 0.5, clear winner) |
| `self_handle` | Router handles it with a skill assist (no clear agent winner) |
| `self_handle_unaided` | No agent or skill matched — handle without help |
| `advisory` | Best agent matched but not conclusively — recommend with alternatives |
| `ask_user` | Reserved (not produced in v0.1) |
| `needs_more_detail` | Feature density < 2 dimensions — not enough context to route |
| `mixed_content` | Multiple intents detected |

### 5.3 Dispatch Context Schema

```json
{
  "task_description": "string (required, tokenized into keywords)",
  "file_paths": ["array of strings"],
  "agent_mentions": ["array of agent names"],
  "tool_mentions": ["array of tool names"],
  "command_prefix": "string or null"
}
```

**Feature density rule:** At least 2 of these fields must be non-empty for the matcher to attempt scoring. Otherwise returns `needs_more_detail`.

### 5.4 Catalog Format (dispatch-catalog.json)

```json
{
  "entries": [
    {
      "name": "agni-hermes",
      "kind": "agent",
      "source": "owned",
      "routable": true,
      "triggers": {
        "command_prefixes": ["/infra", "/agni"],
        "agent_mentions": ["agni", "agni-hermes"],
        "path_globs": ["**/nats*", "**/bridge*", "**/fleet*", "**/a2a*"],
        "keywords": [
          {"term": "infrastructure", "weight": 1.0},
          {"term": "nats", "weight": 1.0},
          {"term": "bridge", "weight": 0.8}
        ],
        "tool_mentions": ["systemctl", "nats"],
        "excludes": []
      }
    }
  ]
}
```

Keywords are objects with `term` (string) and `weight` (0.0-1.0), not plain strings.

### 5.5 CLI Usage

```bash
echo '{"task_description":"check NATS bridge","file_paths":["/root/nats/"],"agent_mentions":[],"tool_mentions":[],"command_prefix":null}' \
  | DISPATCH_CATALOG_PATH=/root/agni/fleet_hub/dispatch-catalog.json \
  python3 -m claude_wayfinder dispatch
```

Returns JSON with: `decision`, `agent`, `confidence`, `rationale`, `alternatives`, `skills`, `disposition_source`.

### 5.6 Integration in Fleet Hub

When operator sends a group chat message:
1. If message has ≥2 routing dimensions (keywords + file paths / agent mentions / tool mentions), run Wayfinder
2. If Wayfinder returns `delegate` → publish to that agent's NATS subject
3. If Wayfinder returns `needs_more_detail` or `self_handle` → broadcast to all live agents
4. Show the dispatch decision on the message bubble in the UI

When operator sends a DM to a specific agent → publish directly to that agent's subject, no Wayfinder needed.

## 6. NATS BRIDGE WIRING (THE CRITICAL MISSING PIECE)

### 6.1 Current Problem

Messages published to `dharma.fleet.chat` or `dharma.a2a.hermes` are NOT being picked up by agent bridges. The bridges exist but:

- **AGNI bridge** (`dharma-a2a-agni-hermes-bridge.service`): Active, drains `dharma.a2a.hermes`, can invoke `hermes` CLI for semantic replies. But the JetStream consumer has `DeliverPolicy.NEW` and may skip messages if not actively polling.
- **Meghadharma bridge** (`dharma-meghadharma-hermes-bridge.service`): Active on Meghadharma, drains `dharma.a2a.fleet.reply.meghadharma_hermes`. Responds in ~36 seconds via HTTPS gateway.
- **Rushabdev bridge**: STOPPED (credential exposure incident). Was crash-looping because `nats-py` missing from Hermes venv.

### 6.2 What Needs to Happen

For agents to respond in the Fleet Hub chat:
1. **Group chat messages** must be published to EACH live agent's individual NATS subject (not just `dharma.fleet.chat`)
2. **Each agent's bridge** must drain its subject, detect chat messages, invoke its model, and publish the semantic reply back to `dharma.a2a.rushabdev` (or the Fleet Hub's SSE channel)
3. **The Fleet Hub SSE listener** must capture these replies and route them to the group chat view and/or the agent's DM view

### 6.3 Bridge Architecture

Each VPS runs a `hermes_remote_a2a_bridge.py` that:
- Connects to NATS with scoped credentials
- Pull-subscribes to the agent's subject with a durable consumer
- For semantic task messages: invokes `hermes` CLI to get a model response
- Publishes the reply to the sender's reply subject
- Writes delivery and semantic receipts

The Fleet Hub server needs to:
- Subscribe to `dharma.a2a.>` via JetStream (already done)
- Route replies to the correct SSE channel (group or agent DM)
- Show replies in real-time in the chat UI

## 7. UI REQUIREMENTS (Slack-like)

### 7.1 Layout

- **Left sidebar:** Navigation with group chat, per-agent entries (with live/dead dots), raw NATS feed, kanban board
- **Main area:** Changes based on selected view
- **Input bar:** Bottom of main area for typing messages (hidden on read-only views)

### 7.2 Group Chat View

- Real-time message stream via SSE
- Messages show: sender name (color-coded), text, timestamp
- Operator messages right-aligned with green tint
- Agent messages left-aligned with dark card background
- Dispatch info badge on auto-routed messages ("→ dispatched to agni-hermes (100%): delegate")
- Input box with Enter to send, Shift+Enter for newline
- Message history loaded on page open

### 7.3 Agent Home Page (click agent in sidebar)

- Full agent card: name, role, host, Tailscale, NATS subject, model, provider, meishi, callsign
- Bio paragraph
- Capabilities as tag pills
- Live/dead status indicator
- DM chat area below the card
- Input box for 1:1 messages

### 7.4 Raw NATS Feed View

- Monospace scrolling list of every NATS message
- Each line: sequence number, subject, sender, message preview (truncated)
- Real-time via SSE, no input box (read-only monitoring)
- This is the "transparent campaign monitoring" view

### 7.5 Kanban View

- Task cards with ID, title, status pill
- Fetches from `/api/kanban`

### 7.6 Design Requirements

- **Dark theme** (background #0d0e14, cards #13151c, text #d4cfc4)
- **Phone-first** — must work well on iPhone Safari
- **Mobile-responsive** — sidebar collapses to icons on small screens
- **No page reloads** — all updates via SSE
- **Connection indicator** — green/red dot showing SSE status
- **Self-signed cert warning** — users must accept the cert to access the page (this is expected on nip.io domains)

## 8. API SPECIFICATION

### 8.1 SSE
```
GET /events/{channel}
channel = "group" | "raw" | "agent:<uid>"
Returns: text/event-stream
```

### 8.2 Send Message
```
POST /api/send
Body: { "from_": "operator", "text": "message", "to": null|"agent_uid"|"wayfinder" }
to=null → group chat (broadcast to all live agents)
to="agent_uid" → DM to specific agent
to="wayfinder" → auto-route through Wayfinder
Returns: { "ok": true, "seq": N, "subject": "...", "dispatch": {...} }
```

### 8.3 Roster
```
GET /api/roster
Returns: { "agents": { uid: { callsign, display_name, subject, host, ... } }, "count": N }
```

### 8.4 Agent Detail
```
GET /api/agent/{uid}
Returns: { callsign, display_name, subject, host, tailscale, role, model, provider, meishi, capabilities, status, bio }
```

### 8.5 Chat History
```
GET /api/chat
Returns: { "messages": [ { "from": "...", "text": "...", "ts": "..." } ] }
```

### 8.6 Dispatch Test
```
GET /api/dispatch?task=...
Returns: Wayfinder decision JSON
```

### 8.7 Kanban
```
GET /api/kanban
Returns: { "tasks": [ ... ] }
```

### 8.8 Health
```
GET /health
Returns: { "status": "ok", "version": "0.4.0", "wayfinder": true }
```

## 9. WAYFINDER CATALOG (current)

See file: `/root/agni/fleet_hub/dispatch-catalog.json` on AGNI

3 routable agents:
- **agni-hermes**: keywords (infrastructure, nats, jetstream, bridge, systemd, credential, hub, agni, broker, acl), path_globs (nats*, bridge*, fleet*, a2a*), tools (systemctl, nats)
- **rushabdev**: keywords (revenue, x402, bounty, moltbook, dashboard, operator, rushabdev, payment, cash), path_globs (revenue*, x402*, bounty*, moltbook*, dashboard*), tools (curl, git, hermes)
- **meghadharma-hermes**: keywords (semantic, meghadharma, megha, global, a2a, projection, litestream, backup, grok, codex, one-pane, staging), path_globs (global-a2a*, meghadharma*, onepane*, grok*, staging*), tools (nats, docker, python)

## 10. KNOWN ISSUES TO FIX

1. **Agents don't respond** — bridges not draining chat messages. Must wire group chat to publish to each agent's individual subject AND ensure bridges process and reply.
2. **SSE path issue** — frontend must use `/fleet/events/...` not `/events/...` because Caddy strips the prefix.
3. **Service restart hangs** — old SSE connections prevent clean restart. Need `KillSignal=SIGKILL` in systemd unit or shorter timeout.
4. **No message persistence** — chat history is a flat file tail. Should use SQLite or JetStream replay.
5. **No agent-to-agent chat** — agents can't see each other's replies in the group chat. Need to wire reply subjects back to `dharma.fleet.chat`.
6. **No campaign monitoring** — the raw NATS feed exists but doesn't filter or format multi-agent conversations well.
7. **No Slack features** — no threading, reactions, mentions, file sharing, search.
8. **Wayfinder feature density** — group chat messages often have only 1 dimension (task_description) and get `needs_more_detail`. Need to auto-enrich context or use a different routing strategy for chat.

## 11. REFERENCES

- **Wayfinder repo:** https://github.com/glitchwerks/claude-wayfinder
- **Wayfinder docs:** `docs/schema.md` (dispatch context schema), `docs/design.md` (rationale), `docs/dispatch-authoring-guide.md` (trigger authoring)
- **Dharma Swarm repo:** https://github.com/AmitabhainArunachala/dharma_swarm
- **NATS bridge code:** `/root/.dharma/nats/bridges/hermes_remote_a2a_bridge.py` on each VPS
- **Global A2A build:** `/root/global-a2a/` on Meghadharma (57 tests, production pending)
- **Field registry:** `docs/ops/FLEET_FIELD_REGISTRY.yaml` in dharma_swarm repo

## 12. ACCEPTANCE CRITERIA

1. **Phone test:** Open https://157.245.193.15/fleet/ on iPhone → see group chat with message history → type message → see it appear → see agent reply within 60 seconds
2. **Agent home page:** Click any agent in sidebar → see full agent card with meishi, model, NATS address → type DM → get semantic reply
3. **Raw feed:** Click Raw NATS → see live message stream with sequence numbers and subjects
4. **Interject:** Watch agents talking in raw feed → type into group chat → agents acknowledge and adjust
5. **No laptop required:** All functionality accessible from phone browser

---

*Generated 2026-08-07 by Rushabdev Hermes. All infrastructure details verified via root SSH access to all 3 VPS nodes.*