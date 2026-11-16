#!/usr/bin/env bash
# Move the old server's data and settings onto this (new) server and start it with Claude as the model.
# Run on the NEW server, from the project folder, after scripts/install_server.sh.
# docs/MIGRATION_CLAUDE.md has the whole move.
#
#   ANTHROPIC_API_KEY=... scripts/migrate_import.sh /root/tw-ai-migration-<time>.tar
#   scripts/migrate_import.sh <bundle>                 # asks for the API key if .env has none
#   CLAUDE_MODEL=claude-opus-5-5 scripts/migrate_import.sh <bundle>   # another main model
#
# A rehearsal bundle (exported without --final) starts with TUNNEL_OFF=1: everything runs here but the
# domain stays on the old server (test with an SSH port forward). A --final bundle takes the domain over:
# the same Cloudflare tunnel, so tw-ai.in and grafana.tw-ai.in, the Android app and bookmarks keep working.
# Rerunning with a newer bundle replaces this server's data with that bundle's.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
bundle="${1:?Usage: scripts/migrate_import.sh /path/to/tw-ai-migration-<time>.tar}"
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ -x .venv/bin/python ]] || { echo "Run scripts/install_server.sh first." >&2; exit 1; }
LOG_DIR="${LOG_DIR:-/var/log/ai-platform}"
MAIN_MODEL="${CLAUDE_MODEL:-claude-sonnet-5-5}"
FAST_MODEL="${CLAUDE_FAST_MODEL:-claude-haiku-5-5}"
umask 077

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
echo "Checking the bundle..."
tar -xf "$bundle" -C "$work"
pack="$work/tw-ai-migration"
(cd "$pack" && sha256sum --check --quiet SHA256SUMS) || { echo "The bundle is damaged. Nothing was changed." >&2; exit 1; }
echo "  made $(cat "$pack/made") from commit $(cut -c1-8 "$pack/commit" 2>/dev/null || echo '?')$([[ -f "$pack/final" ]] && echo ', final')"

env_of() { grep -m1 "^$2=" "$1" 2>/dev/null | cut -d= -f2- || true; }
if [[ -f .env && "$(env_of .env VAULT_MASTER_KEY)" != "$(env_of "$pack/env" VAULT_MASTER_KEY)" && -z "${FORCE:-}" ]]; then
  echo "This server's .env has a different vault key than the bundle's. Replacing it would lock out whatever" >&2
  echo "was stored here with it. If that's intended (this server has nothing of its own), rerun with FORCE=1." >&2
  exit 1
fi

# Keep an API key this server already has (a rerun with a newer bundle).
key="${ANTHROPIC_API_KEY:-$(env_of .env ANTHROPIC_API_KEY)}"
if [[ -z "$key" && -t 0 ]]; then
  read -r -s -p "Anthropic API key (sk-ant-..., not shown): " key; echo
fi

echo "Stopping anything already running here..."
scripts/start_native.sh stop >/dev/null 2>&1 || true

echo "Settings (.env) for Claude, no GPU..."
install -m 600 "$pack/env" .env
tunnel_off=1
[[ -f "$pack/final" ]] && tunnel_off=0
python3 scripts/env_set.py LLM_PROVIDER=anthropic "ANTHROPIC_API_KEY=$key" "LLM_MODEL=$MAIN_MODEL" \
  "LLM_FAST_MODEL=$FAST_MODEL" LLM_THINKING=off LLM_CONTEXT_TOKENS=200000 LLM_MAX_TOKENS=8192 \
  STT_DEVICE=cpu "TUNNEL_OFF=$tunnel_off" "SCHEDULER_OFF=$tunnel_off" \
  --comment-out VLLM_MODEL_PATH VLLM_EXTRA_ARGS VLLM_GPU_UTIL VLLM_MAX_MODEL_LEN LLM_BASE_URL HUGGING_FACE_HUB_TOKEN
[[ -n "$key" ]] || echo "  WARNING: no ANTHROPIC_API_KEY yet: the assistant won't answer until you set it" \
  "(python3 scripts/env_set.py ANTHROPIC_API_KEY=... then systemctl restart ai-platform)."

echo "Tunnel, proxy key, published app, tools..."
if [[ -d "$pack/cloudflared" ]]; then
  install -d -m 700 /root/.cloudflared
  cp -a "$pack/cloudflared/." /root/.cloudflared/
  chmod 600 /root/.cloudflared/* 2>/dev/null || true
fi
if [[ -d "$pack/ssh" ]]; then
  install -d -m 700 /root/.ssh
  key_path="$(env_of .env PROXY_SSH_KEY)"; key_path="${key_path:-/root/.ssh/proxy_vps}"
  install -m 600 "$pack/ssh/proxy_vps" "$key_path"
  [[ -f "$pack/ssh/proxy_vps.pub" ]] && install -m 644 "$pack/ssh/proxy_vps.pub" "$key_path.pub"
fi
if [[ -d "$pack/var-lib/android" ]]; then
  install -d -m 755 /var/lib/ai-platform
  rm -rf /var/lib/ai-platform/android && cp -a "$pack/var-lib/android" /var/lib/ai-platform/
fi
install -d -m 755 /var/lib/ai-platform/work
if ! command -v minio >/dev/null && [[ -f "$pack/minio" ]]; then
  install -m 755 "$pack/minio" /usr/local/bin/minio
fi
command -v minio >/dev/null || { echo "MinIO is missing: rerun scripts/install_server.sh." >&2; exit 1; }
usage="$(env_of .env LLM_USAGE_LOG)"
if [[ -n "$usage" && -f "$pack/llm_usage.jsonl" ]]; then
  mkdir -p "$(dirname "$usage")"
  # Merged by time, without duplicates (a rerun brings the same lines again). Gemma-era lines have no "m".
  python3 - "$pack/llm_usage.jsonl" "$usage" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
lines = []
for path in (dst, src):
    try:
        lines += open(path).read().splitlines()
    except FileNotFoundError:
        pass
def t(line):
    try:
        return json.loads(line)["t"]
    except (ValueError, KeyError, TypeError):
        return 0
open(dst, "w").write("".join(f"{l}\n" for l in sorted(dict.fromkeys(l for l in lines if l.strip()), key=t)))
PY
fi
if [[ -d "$pack/twai-android" ]]; then
  if [[ -f /root/.config/twai-android/release.jks ]]; then
    cmp -s "$pack/twai-android/release.jks" /root/.config/twai-android/release.jks || \
      echo "  NOTE: a different Android signing key is already here; left it alone (phones accept only the original)."
  else
    install -d -m 700 /root/.config/twai-android && cp -a "$pack/twai-android/." /root/.config/twai-android/
  fi
fi
stt_model="$(env_of .env STT_MODEL)"; stt_model="${stt_model:-/root/models/whisper-large-v3-turbo}"
if [[ -d "$pack/whisper" && ! -f "$stt_model/model.bin" ]]; then
  mkdir -p "$stt_model" && cp -a "$pack/whisper/." "$stt_model/"
fi

echo "Database role and Redis password..."
scripts/install_server.sh --database

echo "Restoring the database and stored files..."
mkdir -p "${MINIO_DATA:-/var/lib/minio}"
echo RESTORE | BACKUP_MODE=native scripts/restore.sh "$pack/backup.tar"
# restore.sh starts everything by hand; hand it to systemd so it also comes back after a reboot.
scripts/start_native.sh stop >/dev/null 2>&1 || true
systemctl reset-failed ai-platform >/dev/null 2>&1 || true
systemctl restart ai-platform

echo "Waiting for the gateway..."
for _ in $(seq 60); do curl -sf http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break; sleep 2; done
if [[ -n "$key" ]]; then
  echo "Asking Claude for a one-word reply (a fraction of a cent)..."
  .venv/bin/python - <<'PY' || echo "  Claude did not answer: check ANTHROPIC_API_KEY and the gateway log." >&2
from app.llm import client
reply = client.chat([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=20)
print("  Claude answered:", reply.strip()[:40])
PY
fi
scripts/start_native.sh status
echo
if [[ "$tunnel_off" == 1 ]]; then
  echo "Rehearsal: the domain still points at the old server, and the scheduler is off here (SCHEDULER_OFF=1),"
  echo "so reminders, digests and scouts don't run twice. Test this one through an SSH forward:"
  echo "  ssh -L 8000:127.0.0.1:8000 -L 3000:127.0.0.1:3000 root@<this server>   then open http://127.0.0.1:8000/ui/"
  echo "For the real move: on the old server  scripts/migrate_export.sh --final, copy, and rerun this script."
else
  echo "This server now serves $(env_of .env PUBLIC_APP_URL). Delete the bundle on both servers:"
  echo "  shred -u $bundle"
fi
