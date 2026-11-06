#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RUNNER="$SCRIPT_DIR/run-cc.sh"
LABEL="com.siray.cc-worker"
STATE_DIR=""
PYTHON=""
PROBE_DIR="$PWD"
MODE="launchd"
ACTION="install"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: install-daemon.sh [options]

Installs a user-level cc-worker daemon that runs outside the Codex sandbox.
Codex submits requests into a shared queue; the daemon runs the worker and checks.

Options:
  --state-dir DIR   Shared queue/state directory (default: per-user temp dir)
  --python PATH     Python 3.9+ interpreter (default: python3 in PATH)
  --probe-dir DIR   Directory the daemon must be able to read (default: $PWD)
  --terminal        Start with nohup in the current terminal instead of launchd
  --launchd         Use a launchd user agent (default)
  --status          Show launchd/daemon status and exit
  --uninstall       Stop the daemon and remove its launchd agent/env block
  --dry-run         Print the generated plist and env block without installing
  -h, --help        Show this help
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --python) PYTHON="${2:-}"; shift 2 ;;
    --probe-dir) PROBE_DIR="${2:-}"; shift 2 ;;
    --terminal) MODE="terminal"; shift ;;
    --launchd) MODE="launchd"; shift ;;
    --status) ACTION="status"; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
fi
if [ -z "$PYTHON" ]; then
  echo "Error: python3 was not found in PATH." >&2
  exit 2
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Error: Python 3.9 or newer is required." >&2
  exit 2
fi

OS="$(uname -s)"
if [ "$OS" != "Darwin" ] && [ "$DRY_RUN" -eq 0 ] && [ "$ACTION" != "status" ]; then
  echo "Error: install-daemon.sh currently supports macOS only. Use direct run/resume or a manual daemon on this platform." >&2
  exit 2
fi

if [ -z "$STATE_DIR" ]; then
  if [ -n "${CC_WORKER_STATE_DIR:-}" ]; then
    STATE_DIR="$CC_WORKER_STATE_DIR"
  elif [ "$OS" = "Darwin" ]; then
    BASE="$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null || true)"
    STATE_DIR="${BASE:-/tmp/}cc-worker-$(id -u)"
  else
    STATE_DIR="/tmp/cc-worker-$(id -u)"
  fi
fi
case "$STATE_DIR" in
  /*) ;;
  *) STATE_DIR="$PWD/$STATE_DIR" ;;
esac
if [ -L "$STATE_DIR" ]; then
  echo "Error: the state directory cannot be a symlink: $STATE_DIR" >&2
  exit 2
fi

ENV_FILE="${CC_WORKER_ENV_FILE-$HOME/.cc-worker.env}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"

if [ "$ACTION" = "uninstall" ]; then
  if [ "$OS" = "Darwin" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  fi
  rm -f "$PLIST"
  if [ -f "$STATE_DIR/queue/daemon.json" ]; then
    PID="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid", ""))' "$STATE_DIR/queue/daemon.json" 2>/dev/null || true)"
    if [ -n "$PID" ]; then
      kill -TERM "$PID" 2>/dev/null || true
    fi
  fi
  if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
    "$PYTHON" - "$ENV_FILE" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text()
start = text.find("# >>> cc-worker daemon >>>")
end = text.find("# <<< cc-worker daemon <<<")
if start != -1 and end != -1:
    end += len("# <<< cc-worker daemon <<<")
    text = text[:start].rstrip() + "\n" + text[end:].lstrip()
    path.write_text(text)
PY
  fi
  echo "Uninstalled the cc-worker daemon. State retained at: $STATE_DIR"
  exit 0
fi

if [ "$ACTION" = "status" ]; then
  if [ "$OS" = "Darwin" ]; then
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | sed -n '1,16p' || true
  fi
  if [ -f "$STATE_DIR/queue/daemon.json" ]; then
    "$PYTHON" - "$STATE_DIR/queue/daemon.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
for key in ("pid", "started_at", "updated_at", "jobs", "active", "claude", "api", "probe"):
    print("%s: %s" % (key, data.get(key)))
PY
  else
    echo "No daemon heartbeat at $STATE_DIR/queue/daemon.json"
    exit 1
  fi
  exit 0
fi

if [ "$DRY_RUN" -eq 0 ]; then
  command -v claude >/dev/null 2>&1 || { echo "Error: claude was not found in PATH." >&2; exit 2; }
  command -v git >/dev/null 2>&1 || { echo "Error: git was not found in PATH." >&2; exit 2; }
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
fi

TMP_PLIST="$(mktemp "${TMPDIR:-/tmp}/cc-worker-plist.XXXXXX")"
trap 'rm -f "$TMP_PLIST"' EXIT
"$PYTHON" - "$TMP_PLIST" "$LABEL" "$RUNNER" "$STATE_DIR" "$PROBE_DIR" "$PATH" "$HOME" "${SHELL:-/bin/zsh}" "$ENV_FILE" >/dev/null <<'PY'
import os, plistlib, sys
out, label, runner, state, probe, path, home, shell, env_file = sys.argv[1:]
env = {"PATH": path, "HOME": home, "SHELL": shell,
       "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
       "LANG": os.environ.get("LANG", "en_US.UTF-8"),
       "CC_WORKER_STATE_DIR": state, "CC_WORKER_ENV_FILE": env_file}
for key in ("CLAUDE_CONFIG_DIR",):
    if os.environ.get(key):
        env[key] = os.environ[key]
data = {
    "Label": label,
    "ProgramArguments": ["/bin/bash", runner, "daemon", "--state-dir", state,
                         "--poll", "1", "--jobs", "1", "--probe-dir", probe],
    "EnvironmentVariables": env,
    "WorkingDirectory": home,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "StandardOutPath": os.path.join(home, "Library/Logs/cc-worker-daemon.log"),
    "StandardErrorPath": os.path.join(home, "Library/Logs/cc-worker-daemon.err.log"),
}
with open(out, "wb") as handle:
    plistlib.dump(data, handle)
PY

write_env_block() {
  if [ -z "$ENV_FILE" ]; then
    return
  fi
  "$PYTHON" - "$ENV_FILE" "$STATE_DIR" <<'PY'
import shlex, sys
from pathlib import Path
path = Path(sys.argv[1])
state = sys.argv[2]
text = path.read_text() if path.exists() else ""
start = text.find("# >>> cc-worker daemon >>>")
end = text.find("# <<< cc-worker daemon <<<")
if start != -1 and end != -1:
    end += len("# <<< cc-worker daemon <<<")
    text = text[:start].rstrip() + "\n" + text[end:].lstrip()
path.parent.mkdir(parents=True, exist_ok=True)
block = ('# >>> cc-worker daemon >>>\n'
         'export CC_WORKER_STATE_DIR=%s\n'
         '# <<< cc-worker daemon <<<\n' % shlex.quote(state))
path.write_text((text.rstrip() + "\n\n" + block) if text.strip() else block)
path.chmod(0o600)
PY
}

if [ "$DRY_RUN" -eq 1 ]; then
  echo "State directory: $STATE_DIR"
  echo "Plist: $PLIST"
  echo "Environment file: $ENV_FILE"
  echo "--- plist ---"
  cat "$TMP_PLIST"
  echo "--- env block ---"
  echo "# >>> cc-worker daemon >>>"
  echo "export CC_WORKER_STATE_DIR=\"$STATE_DIR\""
  echo "# <<< cc-worker daemon <<<"
  exit 0
fi

write_env_block
mkdir -p "$LOG_DIR"
INSTALL_STARTED="$(date +%s)"
OLD_PID=""
if [ -f "$STATE_DIR/queue/daemon.json" ]; then
  OLD_PID="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid", ""))' "$STATE_DIR/queue/daemon.json" 2>/dev/null || true)"
fi

if [ "$MODE" = "terminal" ]; then
  nohup /bin/bash "$RUNNER" daemon --state-dir "$STATE_DIR" --poll 1 --jobs 1 \
    --probe-dir "$PROBE_DIR" >>"$LOG_DIR/cc-worker-daemon.log" 2>&1 &
  echo "Started the cc-worker daemon in the background (PID $!)."
else
  mkdir -p "$HOME/Library/LaunchAgents"
  cp "$TMP_PLIST" "$PLIST"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  for _ in $(seq 1 50); do
    if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done
  for attempt in $(seq 1 5); do
    if launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
      break
    fi
    if [ "$attempt" -eq 5 ]; then
      echo "Error: launchctl bootstrap failed for $PLIST" >&2
      exit 1
    fi
    sleep 0.5
  done
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  echo "Loaded launchd agent $LABEL"
fi

heartbeat_fresh() {
  [ -f "$STATE_DIR/queue/daemon.json" ] || return 1
  "$PYTHON" - "$STATE_DIR/queue/daemon.json" "$INSTALL_STARTED" "$OLD_PID" <<'PY'
import json, os, sys
from datetime import datetime, timezone
try:
    path, started = sys.argv[1], float(sys.argv[2])
    old_pid = sys.argv[3]
    data = json.load(open(path))
    updated = datetime.fromisoformat(data["updated_at"])
    daemon_started = datetime.fromisoformat(data["started_at"]).timestamp()
except Exception:
    raise SystemExit(1)
if old_pid and str(data.get("pid", "")) == old_pid:
    raise SystemExit(1)
if os.path.getmtime(path) < started - 2 or daemon_started < started - 2:
    raise SystemExit(1)
raise SystemExit(0 if (datetime.now(timezone.utc) - updated).total_seconds() < 15 else 1)
PY
}

for _ in $(seq 1 50); do
  if heartbeat_fresh; then
    break
  fi
  sleep 0.2
done

if ! heartbeat_fresh; then
  echo "The daemon did not write a fresh heartbeat. Check $LOG_DIR/cc-worker-daemon.err.log" >&2
  exit 1
fi

"$PYTHON" - "$STATE_DIR/queue/daemon.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print("Daemon running: pid=%s api=%s claude=%s" % (
    data.get("pid"), (data.get("api") or {}).get("status"), data.get("claude")))
probe = data.get("probe") or {}
if probe.get("readable") is False:
    print("WARNING: the daemon cannot read the probe directory: %s" % probe.get("error"))
    print("If macOS TCC is blocking it, grant Full Disk Access to the daemon's Python "
          "(%s) or rerun this installer with --terminal from a terminal that can read the project."
          % data.get("python", "python3"))
PY
