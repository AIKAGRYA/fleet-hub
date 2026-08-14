# Fleet Hub v0.5

Phone-first operator console for the three-VPS fleet.

Live URL after install: `https://157.245.193.15/fleet/`

## What shipped vs v0.4

- Token gate (`FLEET_HUB_TOKEN`) — login page, Bearer header, cookie
- Presence from last NATS activity (not hardcoded `live`)
- Health panel (broker + last-seen)
- Nodes map (AGNI / Meghadharma / Rushabdev)
- Mobile bottom tabs + drawer
- Optional browser notifications

## Install on AGNI

1. Copy this tree to AGNI as `/root/agni/fleet_hub_incoming`
2. Write `/etc/dharma/fleet-hub.env` with `FLEET_HUB_TOKEN=...` (mode 600)
3. `bash /root/agni/fleet_hub_incoming/install_on_agni.sh`
4. Confirm Caddy still reverse-proxies `/fleet/*` → `127.0.0.1:8444`

Meghadharma stages the same tree at `/root/fleet_hub_v05` and can serve a tarball for AGNI to pull.
