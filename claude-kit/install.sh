#!/usr/bin/env bash
# Installs the claude-kit into .claude/ so Claude Code auto-discovers skills, commands, and agents.
# Run from the repo root:  bash claude-kit/install.sh
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for dir in skills commands agents; do
  mkdir -p "$root/.claude/$dir"
  cp -R "$root/claude-kit/$dir/." "$root/.claude/$dir/"
  echo "Installed $dir -> .claude/$dir"
done
echo
echo "Done. Restart Claude Code in this repo; /kb-status should be available."
