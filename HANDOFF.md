# Fleet Hub frontend — handoff

**GitHub (what a remote Claude agent should clone):** https://github.com/AIKAGRYA/fleet-hub

On-box copy: `/root/fleet_hub_frontend_handoff`

This is the current Fleet Hub UI codebase. It is **not** `dharma_swarm` and **not** the Megha Command dashboard (`command.178-128-87-170.nip.io`). Those are different products.

## What to edit

| File | Role |
|---|---|
| `src/static/index.html` | **The frontend.** One HTML file: CSS + markup + JS. ~323 lines. No React/Next/Vite. |
| `src/server.py` | FastAPI backend the UI already talks to. Change only if the UI needs a new endpoint. |
| `src/roster.json` | Static agent cards. v0.5 already overlays last-seen from NATS. |

Live production is still **v0.4** at https://157.245.193.15/fleet/ (snapshot in `live_v04/`). v0.5 in `src/` is staged on Meghadharma and **not installed on AGNI yet**. Build against `src/` so we don't regress the token gate / presence / mobile tabs already written.

## Read first

1. `FLEET_HUB_BUILD_SPEC.md` — operator vision, API, Slack-like UX, acceptance (phone).
2. `OPERATOR_25.md` — John's 25-item punch list. P0 first.
3. `src/README.md` — what v0.5 already added vs 0.4.
4. `live_v04/` — what the operator actually sees today.

## Constraints

- Base path is `/fleet`. Caddy strips it. All `fetch`/`EventSource` must use `/fleet/...`.
- Phone-first (iPhone Safari). Operator is on the phone ~90% of the time.
- Dark theme in the spec (`#0d0e14` / `#13151c` / `#d4cfc4`). Light toggle is P3.
- Do **not** invent a second bus. Talk to the existing FastAPI + NATS SSE.
- Do **not** put tokens, NATS passwords, or SSH keys in the repo or the page.
- AGNI SSH from Meghadharma is still denied. Ship a drop-in `src/` tree; install is a later step (`src/install_on_agni.sh`).
- Agent replies in chat are a **bridge** problem, not a CSS problem. UI must still show send + SSE + empty/error honestly.

## P0 for this agent

1. Token gate that actually works against `src/server.py` (`/login`, Bearer, cookie).
2. Live/dead + last-seen from API, not hardcoded `live`.
3. Health panel (broker + last-seen) visible on phone.
4. Mobile layout that doesn't require a laptop.
5. Optional browser notifications for new messages.

Then P1/P2 from `OPERATOR_25.md`. Slack-like threading/search is in the spec; don't block P0 on it.

## Local run (Meghadharma)

```bash
cd /root/fleet_hub_frontend_handoff/src
python3 server.py   # or uvicorn; binds like fleet-hub (8444)
# UI expects to be served under /fleet/
```

Live check of current production (read-only): `https://157.245.193.15/fleet/`

## Done when

iPhone can open the hub, log in, see real agent last-seen, send a group message, and watch SSE without a laptop. Receipt: screenshots + which of the 25 items closed.
