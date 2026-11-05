#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SKILL_SRC="$REPO_DIR/cc-worker"
TARGET_BASE="$HOME/.agents/skills"

case "${1:-}" in
  --target)
    if [ "$#" -ne 2 ] || [ -z "$2" ]; then
      echo "Usage: install.sh [--target <skills-directory>]" >&2
      exit 2
    fi
    TARGET_BASE="$2"
    ;;
  -h|--help)
    echo "Usage: install.sh [--target <skills-directory>]"
    exit 0
    ;;
  "") ;;
  *) echo "Usage: install.sh [--target <skills-directory>]" >&2; exit 2 ;;
esac

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
    exit 0
  fi
  echo "Error: Another symlink already exists at $TARGET. Remove it explicitly before reinstalling." >&2
  exit 1
fi
if [ -e "$TARGET" ]; then
  echo "Error: Existing path will not be overwritten: $TARGET" >&2
  exit 1
fi
ln -s "$SKILL_SRC" "$TARGET"
echo "Installed: $TARGET"
echo 'Use /skills or $cc-worker in Codex. Restart Codex if it does not appear.'
