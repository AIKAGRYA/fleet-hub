#!/usr/bin/env bash
# Run explicitly on AGNI as root after a reviewed commit is authorized.
# Installs an immutable release and atomically switches `current`; it never
# deletes an existing release. This script is not invoked by tests or import.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SRC/.." && pwd)"
APP_ROOT=/opt/dharma/fleet-hub
RELEASE_ROOT="$APP_ROOT/releases"
CURRENT_LINK="$APP_ROOT/current"
PREVIOUS_LINK="$APP_ROOT/previous"
ENV_FILE=/etc/dharma/fleet-hub.env
UNIT_SRC="$SRC/systemd/fleet-hub.service"
UNIT_DEST=/etc/systemd/system/fleet-hub.service
PORT=8444
RELEASE_ID="${RELEASE_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "FAIL: installer must run as root on AGNI" >&2
  exit 1
fi
if [[ ! "$RELEASE_ID" =~ ^[A-Za-z0-9._-]{8,80}$ ]]; then
  echo "FAIL: RELEASE_ID must be 8-80 safe characters" >&2
  exit 1
fi
if [[ ! -f "$SRC/server.py" || ! -f "$UNIT_SRC" || ! -f "$REPO_ROOT/pyproject.toml" || ! -f "$REPO_ROOT/uv.lock" ]]; then
  echo "FAIL: run from a complete reviewed Fleet Hub repository" >&2
  exit 1
fi
if ! command -v uv >/dev/null 2>&1 || ! command -v python3.12 >/dev/null 2>&1; then
  echo "FAIL: uv and python3.12 are required to build the locked release environment" >&2
  exit 1
fi
if [[ ! -f "$ENV_FILE" ]] || ! grep -Eq '^FLEET_HUB_TOKEN=[^[:space:]]' "$ENV_FILE"; then
  echo "FAIL: $ENV_FILE must contain a non-empty FLEET_HUB_TOKEN" >&2
  exit 1
fi

RELEASE_DIR="$RELEASE_ROOT/$RELEASE_ID"
if [[ -e "$RELEASE_DIR" ]]; then
  echo "FAIL: release already exists: $RELEASE_DIR" >&2
  exit 1
fi

if ! id -u fleet-hub >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin fleet-hub
fi
install -d -o root -g fleet-hub -m 0750 /etc/dharma "$APP_ROOT" "$RELEASE_ROOT"
chown root:fleet-hub "$ENV_FILE"
chmod 0640 "$ENV_FILE"
install -d -o root -g fleet-hub -m 0750 "$RELEASE_DIR"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --exclude .git --exclude tests --exclude __pycache__ --exclude '*.pyc' "$SRC/" "$RELEASE_DIR/"
else
  cp -a "$SRC/." "$RELEASE_DIR/"
  find "$RELEASE_DIR" -type f -name '*.pyc' -delete
fi

# Build this release's interpreter environment from the reviewed lock. The
# service never depends on a mutable host-global site-packages directory.
UV_PROJECT_ENVIRONMENT="$RELEASE_DIR/.venv" \
  uv sync --project "$REPO_ROOT" --locked --no-dev

chown -R root:fleet-hub "$RELEASE_DIR"
find "$RELEASE_DIR" -type d -exec chmod 0750 {} +
find "$RELEASE_DIR" -type f -exec chmod 0640 {} +
find "$RELEASE_DIR/.venv/bin" -type f -exec chmod 0750 {} +
chmod 0750 "$RELEASE_DIR/install_on_agni.sh"

if [[ -L "$CURRENT_LINK" ]]; then
  old_release="$(readlink -f "$CURRENT_LINK")"
  case "$old_release" in
    "$RELEASE_ROOT"/*) ln -sfn "$old_release" "$PREVIOUS_LINK" ;;
    *) echo "FAIL: current link escapes release root" >&2; exit 1 ;;
  esac
fi
ln -s "$RELEASE_DIR" "$APP_ROOT/current.next"
mv -Tf "$APP_ROOT/current.next" "$CURRENT_LINK"

install -o root -g root -m 0644 "$UNIT_SRC" "$UNIT_DEST"
systemctl daemon-reload
systemctl enable fleet-hub.service
systemctl restart fleet-hub.service

for _attempt in 1 2 3 4 5; do
  if systemctl is-active --quiet fleet-hub.service; then
    break
  fi
  sleep 1
done
systemctl is-active --quiet fleet-hub.service

health_code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" || true)"
anon_code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/roster" || true)"
if [[ "$health_code" != 200 ]]; then
  echo "FAIL: /healthz -> $health_code" >&2
  exit 1
fi
if [[ "$anon_code" != 401 && "$anon_code" != 403 ]]; then
  echo "FAIL: unauthenticated /api/roster -> $anon_code" >&2
  exit 1
fi

# Read the credential without printing it. Keep it out of curl's argv as well:
# process listings are not a credential-safe transport even on loopback.
fleet_token="$(grep -E '^FLEET_HUB_TOKEN=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)"
if [[ ! "$fleet_token" =~ ^[A-Za-z0-9._~+/=-]{20,512}$ ]]; then
  unset fleet_token
  echo "FAIL: FLEET_HUB_TOKEN must be a 20-512 character opaque token" >&2
  exit 1
fi
curl_header_file="$(mktemp /run/fleet-hub-curl-header.XXXXXX)"
chmod 0600 "$curl_header_file"
cleanup_curl_header() {
  unset fleet_token
  rm -f -- "$curl_header_file"
}
trap cleanup_curl_header EXIT
printf 'Authorization: Bearer %s\n' "$fleet_token" >"$curl_header_file"
unset fleet_token
auth_code="$(curl -sS -o /dev/null -w '%{http_code}' -H "@$curl_header_file" "http://127.0.0.1:$PORT/api/roster" || true)"
rm -f -- "$curl_header_file"
trap - EXIT
if [[ "$auth_code" != 200 ]]; then
  echo "FAIL: authenticated /api/roster -> $auth_code" >&2
  exit 1
fi

echo "INSTALL PASS release=$RELEASE_ID health=$health_code auth_gate=$anon_code authenticated=$auth_code"
echo "Rollback pointer: $PREVIOUS_LINK"
