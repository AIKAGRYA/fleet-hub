# Deploy Fleet Hub v0.6 on AGNI

Phone-runnable checklist. Every step verifiable from iPhone + one SSH
session (Termius/Blink) to AGNI (`157.245.193.15`). Run all commands as root.

## 1. Mint the token (once)

```bash
mkdir -p /etc/dharma
echo "FLEET_HUB_TOKEN=$(openssl rand -hex 24)" >> /etc/dharma/fleet-hub.env
chmod 600 /etc/dharma/fleet-hub.env
cat /etc/dharma/fleet-hub.env   # copy the token into your password manager
```

## 2. Get the tree onto AGNI

From the repo (branch `claude/fleet-hub-operator-34yfbf`), land `src/` at
`/root/agni/fleet_hub_incoming` — git pull on AGNI, or tarball via
Meghadharma, either works:

```bash
git clone --depth 1 -b claude/fleet-hub-operator-34yfbf <repo-url> /tmp/fh \
  && rm -rf /root/agni/fleet_hub_incoming \
  && cp -a /tmp/fh/src /root/agni/fleet_hub_incoming
```

## 3. Run the installer

```bash
bash /root/agni/fleet_hub_incoming/install_on_agni.sh
```

Must end with `INSTALL PASS`. It backs up the old tree
(`/root/agni/fleet_hub.bak.<UTC timestamp>`), syncs the whole tree, installs
the unit, and smoke-tests: `/healthz` 200, unauthenticated `/api/roster`
rejected (**`FAIL: AUTH IS OPEN` aborts the install**), bearer-token roster
200. If it exits 1 on the token gate, do step 1 first.

## 4. Caddy — no change needed

Confirm the existing block still reads:

```
handle_path /fleet/* {
    reverse_proxy 127.0.0.1:8444
}
```

## 5. iPhone verify

1. Open `https://157.245.193.15/fleet/` — you get the **login gate** (not the
   app: proves fail-closed).
2. Paste the token, log in — the app loads.
3. Talk tab shows **chat history from before this deploy** — that history
   survived the restart, proving JetStream replay works.

## 6. Restart-under-6s test

```bash
time systemctl restart fleet-hub.service
```

Total under ~6s (`TimeoutStopSec=5` kills hung SSE connections). Phone
reconnects on its own.

## 7. Add to Home Screen

Safari → Share → Add to Home Screen. Expect the gold hanko-seal icon, name
"Fleet", standalone launch (no Safari chrome), dark `#0d0e14` splash.

## 8. v0.4 alias check

```bash
curl -s http://127.0.0.1:8444/health
# {"status":"ok","version":"0.6.0","wayfinder":false}
```

Old pollers keep working.

## Rollback

```bash
ls -d /root/agni/fleet_hub.bak.*        # pick the timestamp you want
systemctl stop fleet-hub
rm -rf /root/agni/fleet_hub
cp -a /root/agni/fleet_hub.bak.<TS> /root/agni/fleet_hub
systemctl start fleet-hub
```

(The old unit file, if you need it, is inside the backup at
`systemd/fleet-hub.service` — `cp` it to `/etc/systemd/system/` and
`systemctl daemon-reload`.)

## Key rotation (truth amnesty)

Keys are currently strewn across the 3 VPSes. Consolidate:

1. **Inventory** — on each of AGNI, Meghadharma (`178.128.87.170`),
   Rushabdev (`167.172.95.184`):

   ```bash
   grep -rilE 'nats|token|pass|secret|key' /root/*.env /root/.env* /etc/dharma/ 2>/dev/null
   grep -rl 'nats://' /etc/systemd/system/ 2>/dev/null
   ```

   List every file holding NATS creds or hub tokens before touching anything.
2. **Rotate NATS credentials** — set a new user/pass in the AGNI
   `nats-server` config, restart the broker, then update the two remaining
   live consumers (Meghadharma + Rushabdev Hermes bridges) in the same
   sitting. Anything you don't update goes dark — that's the amnesty working.
3. **Rotate `FLEET_HUB_TOKEN`** — repeat step 1's mint (replace the line, not
   append), `systemctl restart fleet-hub`, re-login on the phone.
4. **One vault file per host** — the `/etc/dharma/` pattern: each service
   reads one env file, `chmod 600`, root-owned, never in git. Delete stray
   copies found in the inventory after their service is migrated.
5. **Revoke archived-seat creds** — the 7 archived seats
   (`dharma-command-node`, `fleet-state-projector`, `fable_claude_code`,
   `fable_5_cursor`, `devin-roaming-2987d222`, `perplexity-computer`,
   `fable_composer`) must not hold working NATS credentials. Old creds die
   with the rotation in step 2 — just don't hand the new ones to any archived
   seat. A seat re-earns creds by shipping a live heartbeat first.
