#!/bin/bash
# Run ON AGNI as root. Idempotent install of Fleet Hub v0.6.
#
# Usage:
#   bash install_on_agni.sh                # SRC = directory this script lives in
#   DEST=/some/where bash install_on_agni.sh
#   ALLOW_NO_TOKEN=1 bash install_on_agni.sh   # dev escape hatch ONLY (server still fails closed)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${DEST:-/root/agni/fleet_hub}"
ENV_FILE=/etc/dharma/fleet-hub.env
UNIT_SRC="$SRC/systemd/fleet-hub.service"
UNIT_DEST=/etc/systemd/system/fleet-hub.service
PORT=8444

if [[ ! -f "$SRC/server.py" ]]; then
  echo "missing $SRC/server.py — run this script from inside the fleet-hub src tree" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Fail-closed token gate.
# WHY: v0.4 shipped with no auth and v0.5 failed OPEN when FLEET_HUB_TOKEN was
# unset. The v0.6 server hard-denies without a token, but we also refuse to
# *install* without one so the failure is loud at deploy time, on this
# terminal, instead of silent 403s on John's phone. ALLOW_NO_TOKEN=1 is a dev
# escape hatch only — the server stays locked regardless.
# ---------------------------------------------------------------------------
if [[ "${ALLOW_NO_TOKEN:-0}" != "1" ]]; then
  if [[ ! -f "$ENV_FILE" ]] || ! grep -Eq '^FLEET_HUB_TOKEN=[^[:space:]]' "$ENV_FILE"; then
    cat >&2 <<EOF
FAIL: $ENV_FILE missing or has no non-empty FLEET_HUB_TOKEN= line.

Fix (as root on AGNI):
  mkdir -p /etc/dharma
  echo "FLEET_HUB_TOKEN=\$(openssl rand -hex 24)" >> $ENV_FILE
  chmod 600 $ENV_FILE

Then re-run this installer. (Set ALLOW_NO_TOKEN=1 to bypass for local dev
only — the hub will stay LOCKED until a token exists.)
EOF
    exit 1
  fi
fi

# ---- backup ----------------------------------------------------------------
if [[ -d "$DEST" ]]; then
  BACKUP="${DEST}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -a "$DEST" "$BACKUP"
  echo "backed up to $BACKUP"
fi

# ---- whole-tree sync (no more silently-skipped assets) ---------------------
# Guard --delete: refuse to sync into a populated directory that is not already
# a fleet-hub install. A mis-set DEST (e.g. DEST=/ or a wrong path) must never
# let rsync --delete wipe an unrelated tree.
DEST_ABS="$(cd "$DEST" 2>/dev/null && pwd || echo "$DEST")"
case "$DEST_ABS" in
  ""|/|/root|/etc|/usr|/var|/home|/boot|/bin|/sbin|/lib*)
    echo "FAIL: refusing to install into system path '$DEST_ABS'" >&2; exit 1 ;;
esac
if [[ -d "$DEST" ]] && [[ -n "$(ls -A "$DEST" 2>/dev/null)" ]] && [[ ! -f "$DEST/server.py" ]]; then
  echo "FAIL: '$DEST' is non-empty but has no server.py — not a fleet-hub install." >&2
  echo "      Refusing rsync --delete into it. Set DEST to the real install dir." >&2
  exit 1
fi
mkdir -p "$DEST"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete --exclude .git --exclude tests --exclude __pycache__ "$SRC/" "$DEST/"
else
  echo "rsync not found — falling back to cp -a (no --delete semantics)" >&2
  cp -a "$SRC/." "$DEST/"
  rm -rf "$DEST/tests" "$DEST/.git"
  find "$DEST" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
fi

# ---- systemd unit ----------------------------------------------------------
if [[ -f "$UNIT_SRC" ]]; then
  cp "$UNIT_SRC" "$UNIT_DEST"
  systemctl daemon-reload
fi
systemctl enable --now fleet-hub.service
systemctl restart fleet-hub.service
sleep 2
systemctl is-active fleet-hub.service

# ---- smoke tests -----------------------------------------------------------
PASS=1

# (a) /healthz must answer 200 (unauthenticated liveness probe)
code=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" || echo 000)
if [[ "$code" == "200" ]]; then
  echo "PASS: /healthz -> 200"
else
  echo "FAIL: /healthz -> $code (expected 200)"
  PASS=0
fi

# (b) /api/roster WITHOUT auth must be rejected. If it answers 200 the hub is
# wide open to the internet — that is an install FAILURE, full stop.
code=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/roster" || echo 000)
if [[ "$code" == "401" || "$code" == "403" ]]; then
  echo "PASS: /api/roster unauthenticated -> $code (fail-closed)"
elif [[ "$code" == "200" ]]; then
  echo "FAIL: AUTH IS OPEN"
  echo "FAIL: /api/roster answered 200 with no credentials — refusing to bless this install"
  exit 1
else
  echo "FAIL: /api/roster unauthenticated -> $code (expected 401/403)"
  PASS=0
fi

# (c) With the real token, /api/roster must answer 200.
if [[ -r "$ENV_FILE" ]]; then
  TOKEN=$(grep -E '^FLEET_HUB_TOKEN=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)
  if [[ -n "$TOKEN" ]]; then
    code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/api/roster" || echo 000)
    if [[ "$code" == "200" ]]; then
      echo "PASS: /api/roster with bearer token -> 200"
    else
      echo "FAIL: /api/roster with bearer token -> $code (expected 200)"
      PASS=0
    fi
  else
    echo "SKIP: token check ($ENV_FILE has empty FLEET_HUB_TOKEN)"
  fi
else
  echo "SKIP: token check ($ENV_FILE not readable)"
fi

# ---- summary ---------------------------------------------------------------
echo "----------------------------------------"
if [[ "$PASS" == "1" ]]; then
  echo "INSTALL PASS  dest=$DEST  port=$PORT"
else
  echo "INSTALL FAIL  dest=$DEST  port=$PORT — see FAIL lines above"
  exit 1
fi
