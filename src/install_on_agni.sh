#!/bin/bash
# Run ON AGNI as root. Idempotent install of Fleet Hub v0.5.
set -euo pipefail
SRC="${1:-/root/agni/fleet_hub_incoming}"
DEST=/root/agni/fleet_hub
BACKUP="/root/agni/fleet_hub.bak.$(date -u +%Y%m%dT%H%M%SZ)"

if [[ ! -f "$SRC/server.py" ]]; then
  echo "missing $SRC/server.py" >&2
  exit 1
fi
if [[ -d "$DEST" ]]; then
  cp -a "$DEST" "$BACKUP"
  echo "backed up to $BACKUP"
fi
mkdir -p "$DEST/static"
cp -a "$SRC/server.py" "$DEST/server.py"
cp -a "$SRC/static/index.html" "$DEST/static/index.html"
if [[ -f "$SRC/roster.json" ]]; then
  cp -a "$SRC/roster.json" "$DEST/roster.json"
fi
if [[ -f "$SRC/systemd/fleet-hub.service" ]]; then
  cp "$SRC/systemd/fleet-hub.service" /etc/systemd/system/fleet-hub.service
  systemctl daemon-reload
fi
# preserve existing token if present
if [[ ! -f /etc/dharma/fleet-hub.env ]]; then
  echo "WARNING: /etc/dharma/fleet-hub.env missing — create FLEET_HUB_TOKEN before enabling auth" >&2
fi
systemctl enable --now fleet-hub.service
systemctl restart fleet-hub.service
sleep 1
systemctl is-active fleet-hub.service
curl -sS -o /dev/null -w 'local_healthz=%{http_code}\n' http://127.0.0.1:8444/healthz || true
echo "install ok dest=$DEST"
