#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SKILL_SRC="$REPO_DIR/cc-worker"
TARGET_BASE="$HOME/.agents/skills"
UPDATE=0
DAEMON=0

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Installs the cc-worker skill as a symlink under ~/.agents/skills.

Options:
  --target DIR   Install into DIR instead of ~/.agents/skills
  --update       Update an existing directory copy (refuses non-cc-worker dirs)
  --daemon       Run the installed scripts/install-daemon.sh after installing
  -h, --help     Show this help
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "Usage: install.sh [--target <skills-directory>] [--update] [--daemon]" >&2
        exit 2
      fi
      TARGET_BASE="$2"
      shift 2
      ;;
    --update) UPDATE=1; shift ;;
    --daemon) DAEMON=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ ! -d "$SKILL_SRC" ]; then
  echo "Error: Skill directory not found: $SKILL_SRC" >&2
  exit 1
fi

mkdir -p "$TARGET_BASE"
TARGET_BASE="$(cd "$TARGET_BASE" && pwd)"
TARGET="$TARGET_BASE/cc-worker"

if [ -L "$TARGET" ]; then
  EXISTING="$(cd "$TARGET" 2>/dev/null && pwd -P || true)"
  if [ "$EXISTING" = "$SKILL_SRC" ]; then
    echo "Already installed: $TARGET"
  else
    echo "Error: Another symlink already exists at $TARGET. Remove it explicitly before reinstalling." >&2
    exit 1
  fi
elif [ -e "$TARGET" ]; then
  if [ "$UPDATE" -ne 1 ]; then
    echo "Error: Existing path will not be overwritten: $TARGET" >&2
    echo "Use --update to refresh an existing cc-worker installation." >&2
    exit 1
  fi
  if [ ! -f "$TARGET/SKILL.md" ] || [ ! -f "$TARGET/scripts/cc_worker.py" ]; then
    echo "Error: $TARGET does not look like a cc-worker installation; refusing to update." >&2
    exit 1
  fi
  cp -R "$SKILL_SRC/." "$TARGET/"
  echo "Updated: $TARGET"
else
  ln -s "$SKILL_SRC" "$TARGET"
  echo "Installed: $TARGET"
fi

if [ "$DAEMON" -eq 1 ]; then
  if [ ! -x "$TARGET/scripts/install-daemon.sh" ]; then
    echo "Error: $TARGET/scripts/install-daemon.sh is missing or not executable." >&2
    exit 1
  fi
  "$TARGET/scripts/install-daemon.sh"
fi

echo 'Use /skills or $cc-worker in Codex. Restart Codex if it does not appear.'
