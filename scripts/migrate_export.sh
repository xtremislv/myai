#!/usr/bin/env bash
# Pack up everything the new server needs from this one, into one root-only file. Run on the OLD server,
# from the project folder. docs/MIGRATION_CLAUDE.md has the whole move.
#
#   scripts/migrate_export.sh                  # rehearsal: this server keeps running
#   scripts/migrate_export.sh --final          # cutover: stop the gateway, scheduler and jobs first, so
#                                              # nothing changes after the copy; afterwards stop this server's
#                                              # tunnel and set TUNNEL_OFF=1 here, so the domain is free for
#                                              # the new server (the site is down until that one starts)
#   ... --with-android-key                     # also the Android signing key (~/.config/twai-android), needed
#                                              # to publish app updates from the new server. Never make a new one.
#   ... --with-whisper                         # also the Whisper model (1.6 GB), instead of downloading it there
#
# The bundle holds: a backup (database + stored files, scripts/backup.sh), .env (vault key, all secrets),
# the Cloudflare tunnel's credentials and config (same tunnel, same tw-ai.in addresses), the outbound proxy's
# SSH key, the published Android app (/var/lib/ai-platform/android), the MinIO binary, and the token-usage log.
# It unlocks everything: copy it with scp only, and delete it on both servers once the new one is running.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
FINAL=""; ANDROID_KEY=""; WHISPER=""
for arg in "$@"; do
  case "$arg" in
    --final) FINAL=1 ;;
    --with-android-key) ANDROID_KEY=1 ;;
    --with-whisper) WHISPER=1 ;;
    -h|--help) sed -n 2,19p "$0"; exit 0 ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ -f .env ]] || { echo "No .env here: run from the project folder on the old server." >&2; exit 1; }
LOG_DIR="${LOG_DIR:-/var/log/ai-platform}"
env_value() { grep -m1 "^$1=" .env | cut -d= -f2-; }

umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${MIGRATION_DIR:-/root/migration}"
mkdir -p "$OUT_DIR"
work="$(mktemp -d "$OUT_DIR/.pack-XXXXXX")"
trap 'rm -rf "$work"' EXIT
pack="$work/tw-ai-migration"
mkdir -p "$pack"

if [[ -n "$FINAL" ]]; then
  echo "Stopping the watchdog, gateway, scheduler and jobs (the final copy must not miss anything)..."
  for svc in watchdog scheduler retention gateway; do
    [[ -f "$LOG_DIR/$svc.pid" ]] && kill "$(cat "$LOG_DIR/$svc.pid")" 2>/dev/null && echo "  stopped $svc"
    rm -f "$LOG_DIR/$svc.pid"
  done
  sleep 3
fi

echo "Backing up the database and stored files..."
BACKUP_DIR="$work/backup" BACKUP_MODE=native BACKUP_DEST="" scripts/backup.sh > "$work/backup.log" 2>&1 || {
  cat "$work/backup.log" >&2; exit 1; }
mv "$(ls "$work"/backup/ai-platform-*.tar)" "$pack/backup.tar"

echo "Settings and keys..."
cp .env "$pack/env"
if [[ -d /root/.cloudflared ]]; then
  mkdir -p "$pack/cloudflared" && cp -a /root/.cloudflared/. "$pack/cloudflared/"
fi
key="$(env_value PROXY_SSH_KEY)"; key="${key:-/root/.ssh/proxy_vps}"
if [[ -n "$(env_value PROXY_SSH)" && -f "$key" ]]; then
  mkdir -p "$pack/ssh" && cp "$key" "$pack/ssh/proxy_vps" && cp "$key.pub" "$pack/ssh/proxy_vps.pub" 2>/dev/null || true
fi
[[ -d /var/lib/ai-platform/android ]] && mkdir -p "$pack/var-lib" && cp -a /var/lib/ai-platform/android "$pack/var-lib/"
command -v minio >/dev/null && cp "$(command -v minio)" "$pack/minio"
usage="$(env_value LLM_USAGE_LOG)"
[[ -n "$usage" && -f "$usage" ]] && cp "$usage" "$pack/llm_usage.jsonl"
if [[ -n "$ANDROID_KEY" ]]; then
  [[ -d /root/.config/twai-android ]] && mkdir -p "$pack/twai-android" && cp -a /root/.config/twai-android/. "$pack/twai-android/"
fi
if [[ -n "$WHISPER" ]]; then
  model="$(env_value STT_MODEL)"; model="${model:-/root/models/whisper-large-v3-turbo}"
  [[ -d "$model" ]] && mkdir -p "$pack/whisper" && cp -a "$model/." "$pack/whisper/"
fi
git rev-parse HEAD > "$pack/commit" 2>/dev/null || true
date -u '+%Y-%m-%d %H:%M UTC' > "$pack/made"
[[ -n "$FINAL" ]] && touch "$pack/final"

(cd "$pack" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
bundle="$OUT_DIR/tw-ai-migration-$stamp.tar"
tar -cf "$bundle" -C "$work" tw-ai-migration
chmod 600 "$bundle"

echo
echo "Bundle: $bundle ($(du -h "$bundle" | cut -f1))"
echo "  sha256 $(sha256sum "$bundle" | cut -d' ' -f1)"
echo "  contents: $(cd "$pack" && ls | tr '\n' ' ')"
echo
echo "Copy it to the new server (root only), then run there:"
echo "  scp $bundle root@<new-server>:/root/"
echo "  cd /root/myai && scripts/migrate_import.sh /root/$(basename "$bundle")"
if [[ -n "$FINAL" ]]; then
  # Two servers running the same named tunnel would split visitors between them: hand the domain over.
  python3 scripts/env_set.py TUNNEL_OFF=1 >/dev/null
  for svc in tunnel app-tunnel grafana-tunnel; do
    [[ -f "$LOG_DIR/$svc.pid" ]] && kill "$(cat "$LOG_DIR/$svc.pid")" 2>/dev/null && echo "  stopped $svc"
    rm -f "$LOG_DIR/$svc.pid"
  done
  echo
  echo "This server no longer serves tw-ai.in (gateway and tunnel stopped, TUNNEL_OFF=1 in .env, so a reboot"
  echo "won't bring the tunnel back). The site is down until the new server's import finishes."
  echo "To undo instead:  python3 scripts/env_set.py TUNNEL_OFF=0 && scripts/start_native.sh"
fi
