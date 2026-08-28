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

# The launcher creates this root-only KEY=VALUE file. Read values literally:
# credentials may contain shell metacharacters and must never be re-expanded.
# Quoted export keeps them out of process arguments and tmux metadata.
while IFS='=' read -r key value || [[ -n "$key" ]]; do
  [[ -z "$key" ]] && continue
  if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "Fleet host runner rejected an invalid environment key." >&2
    exit 1
  fi
  export "$key=$value"
done <"$env_file"

exec "$python_bin" -m uvicorn server:app \
  --app-dir "$app_dir" \
  --host 127.0.0.1 \
  --port "$port"
