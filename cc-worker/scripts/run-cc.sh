#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: Python 3.9 or newer is required." >&2
  exit 2
fi

# This optional file is trusted shell code, loaded only in this subprocess.
# Set CC_WORKER_ENV_FILE to an empty string to disable loading it.
ENV_FILE="${CC_WORKER_ENV_FILE-$HOME/.cc-worker.env}"
if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

exec python3 "$SCRIPT_DIR/cc_worker.py" "$@"
