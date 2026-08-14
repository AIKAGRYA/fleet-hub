# Fleet Hub — Claude

You are working the **Fleet Hub frontend**, not Dharma Swarm and not the Megha Command dashboard.

1. Read `HANDOFF.md`.
2. Edit `src/static/index.html` unless the UI needs a new API in `src/server.py`.
3. P0 from `OPERATOR_25.md`: token gate, live last-seen (not hardcoded), health panel, phone layout, optional notifications.
4. All fetch/SSE paths under `/fleet`. Caddy strips the prefix.
5. No secrets in the repo or the page. Token comes from `FLEET_HUB_TOKEN` on the host.
6. Agent chat silence is a bridge problem — still show send + SSE + empty/error honestly.

Done: iPhone can open, log in, see real last-seen, send a group message, watch SSE.
