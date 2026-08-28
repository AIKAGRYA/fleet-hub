#!/usr/bin/env bash
set -euo pipefail

if (($# != 4)); then
  echo "usage: run_hub_from_env.sh ENV_FILE PYTHON APP_DIR PORT" >&2
  exit 64
fi

env_file=$1
python_bin=$2
app_dir=$3
port=$4

[[ -f "$env_file" && -x "$python_bin" && -f "$app_dir/server.py" ]] || {
  echo "Fleet host runner inputs are incomplete." >&2
  exit 1
}
if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1024 || port > 65535)); then
  echo "Fleet host runner received an invalid port." >&2
  exit 1
fi

# The launcher creates this root-only file. Sourcing keeps credentials out of
# process arguments and tmux metadata; the values are never printed.
set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

exec "$python_bin" -m uvicorn server:app \
  --app-dir "$app_dir" \
  --host 127.0.0.1 \
  --port "$port"
