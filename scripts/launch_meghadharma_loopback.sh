#!/usr/bin/env bash
set -euo pipefail

# Launch an isolated, non-promoted Fleet candidate on Meghadharma. The source
# trees and the existing NATS credential file are read-only; Caddy, systemd, ACLs,
# and the live Dharma owner state are never touched.

umask 077

if [[ $(id -u) -ne 0 ]]; then
  echo "This isolated launcher must run as root on Meghadharma." >&2
  exit 1
fi

candidate_root=${FLEET_R10_ROOT:-/root/fleet-hub-r10-candidate-20260828}
case "$candidate_root" in
  /root/fleet-hub-r10-candidate-*) ;;
  *)
    echo "Refusing candidate root outside /root/fleet-hub-r10-candidate-*" >&2
    exit 1
    ;;
esac

owner_source="$candidate_root/dharma_swarm"
fleet_source="$candidate_root/fleet-hub"
runtime_dir="$candidate_root/runtime"
state_root="$candidate_root/state"
hub_venv="$runtime_dir/fleet-venv"
fixture_name=fleet-hub-owner-fixture-r10
mission_id=fleet-hub-r10-local
owner_port=${FLEET_R10_OWNER_PORT:-8871}
fleet_port=${FLEET_R10_HUB_PORT:-8872}
socket_name=fleet-r10
session_name=fleet-r10-candidate
owner_container=fleet-r10-owner
seed_container=fleet-r10-seed
runtime_image=${FLEET_R10_IMAGE:-dharma_swarm-swarm:0d83431a}
nats_env_file=${FLEET_R10_NATS_ENV:-/etc/dharma/grok-build-a2a.env}
nats_principal=${FLEET_R10_NATS_PRINCIPAL:-grok_build}
launch_complete=0
session_created=0
owner_launch_requested=0
seed_launch_requested=0

cleanup_partial_launch() {
  local status=$?
  trap - EXIT
  if ((status != 0 && launch_complete == 0)); then
    if ((session_created == 1)); then
      tmux -L "$socket_name" kill-session -t "$session_name" >/dev/null 2>&1 || true
    fi
    if ((owner_launch_requested == 1)); then
      docker container rm --force "$owner_container" >/dev/null 2>&1 || true
    fi
    if ((seed_launch_requested == 1)); then
      docker container rm --force "$seed_container" >/dev/null 2>&1 || true
    fi
  fi
  exit "$status"
}
trap cleanup_partial_launch EXIT

for port in "$owner_port" "$fleet_port"; do
  if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1024 || port > 65535)); then
    echo "Invalid loopback port: $port" >&2
    exit 1
  fi
done
if [[ "$owner_port" == "$fleet_port" ]]; then
  echo "Owner and Fleet ports must differ." >&2
  exit 1
fi

for command_name in curl docker jq openssl tmux uv; do
  command -v "$command_name" >/dev/null || {
    echo "Missing required command: $command_name" >&2
    exit 1
  }
done

[[ -f "$owner_source/api/main.py" ]] || {
  echo "Owner source is absent: $owner_source" >&2
  exit 1
}
[[ -f "$fleet_source/src/server.py" ]] || {
  echo "Fleet source is absent: $fleet_source" >&2
  exit 1
}
[[ -x "$fleet_source/scripts/run_hub_from_env.sh" ]] || {
  echo "Fleet host runner is absent or not executable." >&2
  exit 1
}
[[ -f "$nats_env_file" ]] || {
  echo "Expected read-only NATS credential file is absent." >&2
  exit 1
}
docker image inspect "$runtime_image" >/dev/null

if tmux -L "$socket_name" has-session -t "$session_name" 2>/dev/null; then
  echo "Session already exists: $session_name" >&2
  exit 1
fi
for container_name in "$seed_container" "$owner_container"; do
  if docker container inspect "$container_name" >/dev/null 2>&1; then
    echo "Container name already exists: $container_name" >&2
    exit 1
  fi
done

mkdir -p "$runtime_dir" "$state_root/home/.dharma/traces"
chmod 700 \
  "$candidate_root" \
  "$runtime_dir" \
  "$state_root" \
  "$state_root/home" \
  "$state_root/home/.dharma" \
  "$state_root/home/.dharma/traces"

# The pinned owner image intentionally remains unchanged. Fleet gets its own
# lockfile-resolved host environment under the isolated runtime directory so
# nats-py is present without mutating the source tree or any live image.
UV_PROJECT_ENVIRONMENT="$hub_venv" \
  UV_PYTHON=python3 \
  uv sync \
    --project "$fleet_source" \
    --locked \
    --no-dev \
    --no-python-downloads \
    --no-progress >/dev/null
"$hub_venv/bin/python" -c 'import fastapi, nats, uvicorn' >/dev/null

owner_token_file="$runtime_dir/owner-token"
fleet_token_file="$runtime_dir/fleet-login-token"
if [[ ! -s "$owner_token_file" ]]; then
  openssl rand -hex 32 >"$owner_token_file"
fi
if [[ ! -s "$fleet_token_file" ]]; then
  openssl rand -hex 32 >"$fleet_token_file"
fi
chmod 600 "$owner_token_file" "$fleet_token_file"
owner_token=$(tr -d '\r\n' <"$owner_token_file")
fleet_token=$(tr -d '\r\n' <"$fleet_token_file")

owner_curl_config="$runtime_dir/owner-curl.conf"
owner_snapshot_file="$runtime_dir/owner-snapshot.json"
hub_curl_config="$runtime_dir/hub-curl.conf"
hub_bootstrap_file="$runtime_dir/hub-bootstrap.json"
{
  printf 'fail\n'
  printf 'silent\n'
  printf 'max-time = 2\n'
  printf 'header = "Authorization: Bearer %s"\n' "$owner_token"
} >"$owner_curl_config"
{
  printf 'fail\n'
  printf 'silent\n'
  printf 'max-time = 2\n'
  printf 'header = "Authorization: Bearer %s"\n' "$fleet_token"
} >"$hub_curl_config"
chmod 600 "$owner_curl_config" "$hub_curl_config"

# This root-owned infrastructure input is read literally, not sourced. That
# preserves shell metacharacters in credentials and admits only the three
# expected keys. Values are never printed.
nats_url=
nats_user=
nats_pass=
while IFS='=' read -r key value || [[ -n "$key" ]]; do
  case "$key" in
    ""|'#'*) ;;
    NATS_URL) nats_url=$value ;;
    NATS_USER) nats_user=$value ;;
    NATS_PASSWORD) nats_pass=$value ;;
    *)
      echo "NATS credential input has an unexpected key." >&2
      exit 1
      ;;
  esac
done <"$nats_env_file"
if [[ -z "$nats_url" || -z "$nats_user" || -z "$nats_pass" ]]; then
  echo "NATS credential inputs are incomplete." >&2
  exit 1
fi
if [[ "$nats_user" != "$nats_principal" ]]; then
  echo "NATS credential principal does not match the declared transport tier." >&2
  exit 1
fi

owner_env="$runtime_dir/owner.env"
hub_env="$runtime_dir/hub.env"
{
  printf 'PYTHONDONTWRITEBYTECODE=1\n'
  printf 'PYTHONPATH=/app:/fleet\n'
  printf 'HOME=/state/home\n'
  printf 'DHARMA_HOME=/state/home/.dharma\n'
  printf 'DHARMA_STATE_DIR=/state/home/.dharma\n'
  printf 'DHARMA_TRACES_DIR=/state/home/.dharma/traces\n'
  printf 'DHARMA_READ_ONLY_BOOT=1\n'
  printf 'DHARMA_FAST_BOOT=1\n'
  printf 'DHARMA_API_MODE=production\n'
  printf 'DASHBOARD_API_KEY=%s\n' "$owner_token"
  printf 'FLEET_HUB_MISSION_ID=%s\n' "$mission_id"
  printf 'FLEET_HUB_OWNER_FIXTURE_STATE_DIR=/state/%s\n' "$fixture_name"
  printf 'DHARMA_SWARM_INIT_TIMEOUT_SECONDS=30\n'
} >"$owner_env"
{
  printf 'PYTHONDONTWRITEBYTECODE=1\n'
  printf 'PYTHONPATH=/fleet/src\n'
  printf 'FLEET_HUB_TOKEN=%s\n' "$fleet_token"
  printf 'FLEET_HUB_INSECURE_COOKIE=1\n'
  printf 'FLEET_HUB_BASE_PATH=/\n'
  printf 'FLEET_HUB_MISSION_CONTROL_URL=http://127.0.0.1:%s\n' "$owner_port"
  printf 'FLEET_HUB_MISSION_CONTROL_TOKEN=%s\n' "$owner_token"
  printf 'FLEET_HUB_MISSION_IDS=%s\n' "$mission_id"
  printf 'FLEET_HUB_EVIDENCE_MODE=fixture\n'
  printf 'FLEET_HUB_SOURCE_INSTANCE=meghadharma-loopback-r10\n'
  printf 'FLEET_HUB_GENERATED_BY_FIXTURE=1\n'
  printf 'FLEET_HUB_DEPLOYMENT_NAMESPACE=meghadharma-loopback-r10\n'
  printf 'FLEET_HUB_AGENT_UID=operator\n'
  printf 'FLEET_HUB_NATS_AGENT_OBSERVATION_SUBJECT=\n'
  printf 'FLEET_HUB_NATS_TRANSPORT_PRINCIPAL=%s\n' "$nats_principal"
  printf 'FLEET_HUB_NATS_TRANSPORT_AUTHORITY=borrowed_existing_transport_only\n'
  printf 'NATS_URL=%s\n' "$nats_url"
  printf 'NATS_USER=%s\n' "$nats_user"
  printf 'NATS_PASS=%s\n' "$nats_pass"
  printf 'NATS_STREAM=DHARMA_A2A\n'
  printf 'NATS_CHAT_SUBJECT=dharma.a2a.fleet\n'
} >"$hub_env"
chmod 600 "$owner_env" "$hub_env"

seed_launch_requested=1
docker run --rm \
  --name "$seed_container" \
  --entrypoint /usr/local/bin/python \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  --memory 1g \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/app:/fleet \
  -e HOME=/state/home \
  -e DHARMA_HOME=/state/home/.dharma \
  -e DHARMA_STATE_DIR=/state/home/.dharma \
  -e DHARMA_TRACES_DIR=/state/home/.dharma/traces \
  -v "$owner_source:/app:ro" \
  -v "$fleet_source:/fleet:ro" \
  -v "$state_root:/state:rw" \
  "$runtime_image" \
  /fleet/scripts/seed_local_owner_fixture.py \
  --state-dir "/state/$fixture_name" \
  --mission-id "$mission_id" >/dev/null
seed_launch_requested=0

owner_args=(
  docker run --rm
  --name "$owner_container"
  --network host
  --entrypoint /usr/local/bin/python
  --cap-drop ALL
  --security-opt no-new-privileges
  --pids-limit 512
  --memory 1536m
  --env-file "$owner_env"
  -v "$owner_source:/app:ro"
  -v "$fleet_source:/fleet:ro"
  -v "$state_root:/state:rw"
  "$runtime_image"
  -m uvicorn scripts.owner_fixture_app:app
  --app-dir /fleet
  --host 127.0.0.1
  --port "$owner_port"
)
printf -v owner_command '%q ' "${owner_args[@]}"
owner_launch_requested=1
session_created=1
tmux -L "$socket_name" new-session -d -s "$session_name" -n owner "$owner_command"

owner_ready=0
for _ in $(seq 1 120); do
  if curl --config "$owner_curl_config" \
    --output "$owner_snapshot_file" \
    "http://127.0.0.1:$owner_port/api/control-surface/missions/$mission_id/snapshot" \
    && jq --exit-status --arg mission "$mission_id" '
      .source_errors == []
      and .data.state == "observed"
      and .data.mission_id == $mission
      and .data.authority == "TaskBoard+RuntimeStateStore"
      and .data.runtime_projection_mode == "owner_supplied_read_only"
      and .data.snapshot != null
      and .data.snapshot.mission.mission_id == $mission
    ' "$owner_snapshot_file" >/dev/null; then
    owner_ready=1
    break
  fi
  sleep 1
done
if [[ "$owner_ready" -ne 1 ]]; then
  echo "Owner did not become ready; inspect the tmux owner window." >&2
  exit 1
fi

hub_args=(
  "$fleet_source/scripts/run_hub_from_env.sh"
  "$hub_env"
  "$hub_venv/bin/python"
  "$fleet_source/src"
  "$fleet_port"
)
printf -v hub_command '%q ' "${hub_args[@]}"
tmux -L "$socket_name" new-window -t "$session_name" -n hub "$hub_command"

hub_ready=0
for _ in $(seq 1 120); do
  if curl --config "$hub_curl_config" \
    --output "$hub_bootstrap_file" \
    "http://127.0.0.1:$fleet_port/api/v1/bootstrap" \
    && jq --exit-status --arg mission "$mission_id" '
      .available == true
      and .qualified == false
      and .evidence_mode == "fixture"
      and .generated_by_fixture == true
      and .source_instance == "meghadharma-loopback-r10"
      and .connections.hub == true
      and .connections.nats == true
      and .connections.mission_control == true
      and .missions.available == true
      and .missions.missions[0].mission_id == $mission
      and .selected_mission.mission_id == $mission
      and .needs_john.available == true
      and .capabilities.mission_read == true
      and .capabilities.mission_commands.available == false
      and .capabilities.chat.available == true
    ' "$hub_bootstrap_file" >/dev/null; then
    hub_ready=1
    break
  fi
  sleep 1
done
if [[ "$hub_ready" -ne 1 ]]; then
  echo "Fleet Hub did not become ready; inspect the tmux hub window." >&2
  exit 1
fi

launch_complete=1
printf 'Fleet R10 loopback candidate is active.\n'
printf 'Session: tmux -L %s attach -t %s\n' "$socket_name" "$session_name"
printf 'Fleet URL on host: http://127.0.0.1:%s/\n' "$fleet_port"
printf 'Login token file (not printed): %s\n' "$fleet_token_file"
printf 'Evidence: isolated fixture; no production owner effect; commands unavailable.\n'
