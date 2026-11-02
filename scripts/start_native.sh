#!/usr/bin/env bash
# Start the whole platform without Docker, on a host where the server is itself a container
# (no systemd, no docker). Safe to rerun: anything already running is left alone.
#
#   scripts/start_native.sh            # start everything
#   scripts/start_native.sh status     # show what is up
#
# Logs go to $LOG_DIR (default /var/log/ai-platform). Every service listens on localhost only.
# Setup: "Without Docker" in docs/SERVER_SETUP.md.
set -uo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
LOG_DIR="${LOG_DIR:-/var/log/ai-platform}"
MINIO_DATA="${MINIO_DATA:-/var/lib/minio}"
PY="$ROOT/.venv/bin/python"
mkdir -p "$LOG_DIR"

[[ -f .env ]] || { echo "No .env: run scripts/generate_env.py --native first." >&2; exit 1; }
env_value() { grep -m1 "^$1=" .env | cut -d= -f2-; }

up() { # name, check command
  if eval "$2" >/dev/null 2>&1; then echo "  up    $1"; return 0; fi
  echo "  down  $1"; return 1
}

status() {
  up postgres "pg_isready -q -h 127.0.0.1 -p 5432"
  up redis "redis-cli -h 127.0.0.1 ping | grep -q PONG"
  up minio "curl -sf http://127.0.0.1:9000/minio/health/live"
  up vllm "curl -sf http://127.0.0.1:8001/v1/models"
  up gateway "curl -sf http://127.0.0.1:8000/healthz"
  command -v tailscale >/dev/null && up tailscale "tailscale status"
  up retention "kill -0 \$(cat '$LOG_DIR/retention.pid')"
}

if [[ "${1:-}" == "status" ]]; then status; exit 0; fi

echo "Starting services (logs in $LOG_DIR):"

pg_isready -q -h 127.0.0.1 -p 5432 || pg_ctlcluster 16 main start

# Private-network access (docs/SERVER_SETUP.md, "Without Docker"). Userspace networking because a
# container has no /dev/net/tun. The saved state rejoins the tailnet and restores `tailscale serve`.
if command -v tailscaled >/dev/null && ! tailscale status >/dev/null 2>&1; then
  mkdir -p /var/lib/tailscale /run/tailscale
  nohup tailscaled --tun=userspace-networking --state=/var/lib/tailscale/tailscaled.state \
    --socket=/run/tailscale/tailscaled.sock >> "$LOG_DIR/tailscaled.log" 2>&1 &
fi

redis-cli -h 127.0.0.1 ping >/dev/null 2>&1 || redis-server /etc/redis/redis.conf

if ! curl -sf http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1; then
  if command -v minio >/dev/null; then
    mkdir -p "$MINIO_DATA"
    MINIO_ROOT_USER="$(env_value MINIO_ACCESS_KEY)" MINIO_ROOT_PASSWORD="$(env_value MINIO_SECRET_KEY)" \
      nohup minio server "$MINIO_DATA" --address 127.0.0.1:9000 --console-address 127.0.0.1:9001 \
      >> "$LOG_DIR/minio.log" 2>&1 &
  else
    echo "  minio is not installed: attachments won't work (docs/SERVER_SETUP.md, Without Docker)." >&2
  fi
fi

# The model can take minutes to load; the gateway doesn't wait for it (/readyz reports it).
# SKIP_VLLM=1 starts everything else, e.g. while the weights are still downloading.
if [[ -z "${SKIP_VLLM:-}" ]] && ! curl -sf http://127.0.0.1:8001/v1/models >/dev/null 2>&1 \
   && ! pgrep -f "vllm serve" >/dev/null; then
  nohup scripts/run_vllm.sh >> "$LOG_DIR/vllm.log" 2>&1 &
fi

if ! curl -sf http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
  "$PY" -m alembic upgrade head >> "$LOG_DIR/gateway.log" 2>&1 || { echo "migrations failed, see $LOG_DIR/gateway.log" >&2; exit 1; }
  nohup "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> "$LOG_DIR/gateway.log" 2>&1 &
fi

# Deletes chat attachments after 3 days, hourly, like the compose "retention" service.
# Tracked by PID file: matching the command line with pgrep also matches unrelated shells.
RETENTION_PID="$LOG_DIR/retention.pid"
if ! { [[ -f "$RETENTION_PID" ]] && kill -0 "$(cat "$RETENTION_PID")" 2>/dev/null; }; then
  PY="$PY" nohup bash -c 'while true; do "$PY" -m scripts.purge_history || echo purge failed; sleep 3600; done' \
    >> "$LOG_DIR/retention.log" 2>&1 &
  echo $! > "$RETENTION_PID"
fi

for _ in $(seq 30); do curl -sf http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break; sleep 1; done
status
