#!/usr/bin/env bash
# Prepare Claude Code sessions on this server: install the CLI if missing, and seed its memory with the
# project background, owner's rules and server facts (deploy/claude-code/memory/, no secrets). Safe to rerun:
# memory files that already exist are kept unless --force.
#
#   scripts/setup_claude_code.sh                 # sessions started in /root/myai
#   scripts/setup_claude_code.sh --dir /root     # sessions started in another folder
#   scripts/setup_claude_code.sh --force         # replace existing memory files with the seeds
#
# Project permissions (.claude/settings.json) and CLAUDE.md come with the repo. Sign in once with `claude`
# (it prints a login link). Then read docs/CLAUDE_CODE_SERVER.md.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
DIR="$ROOT"; FORCE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR="$(cd "$2" && pwd)"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n 2,11p "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

if ! command -v claude >/dev/null; then
  echo "Installing Claude Code..."
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v claude >/dev/null && echo "Claude Code: $(claude --version 2>/dev/null || echo installed)"

# Claude Code keeps a folder's memory under ~/.claude/projects/<the folder's path with / replaced by ->/memory.
mem="$HOME/.claude/projects/$(echo "$DIR" | sed 's#[/.]#-#g')/memory"
mkdir -p "$mem"
for f in "$ROOT"/deploy/claude-code/memory/*.md; do
  name=$(basename "$f")
  if [[ -f "$mem/$name" && -z "$FORCE" && "$name" != MEMORY.md ]]; then
    echo "  kept    $name"
  elif [[ "$name" == MEMORY.md && -f "$mem/MEMORY.md" && -z "$FORCE" ]]; then
    # Add index lines for seeds that aren't listed yet; keep the session's own lines.
    while IFS= read -r line; do
      grep -qF "($(sed -E 's/.*\(([^)]+)\).*/\1/' <<<"$line"))" "$mem/MEMORY.md" || echo "$line" >> "$mem/MEMORY.md"
    done < "$f"
    echo "  merged  MEMORY.md"
  else
    cp "$f" "$mem/$name" && echo "  seeded  $name"
  fi
done
echo "Memory for sessions in $DIR: $mem"
echo "Next: run 'claude' in $DIR, sign in, and start with: read docs/CLAUDE_CODE_SERVER.md"
