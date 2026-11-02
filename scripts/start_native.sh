#!/usr/bin/env bash
# Start the whole platform without Docker, on a host where the server is itself a container
# (no systemd, no docker). Safe to rerun: anything already running is left alone.
#
#   scripts/start_native.sh            # start everything
#   scripts/start_native.sh status     # show what is up, and the public addresses
#
# Logs go to $LOG_DIR (default /var/log/ai-platform). Every service listens on localhost only;
# the two Cloudflare quick tunnels are the only way in. Setup: "Without Docker" in docs/SERVER_SETUP.md.
set -uo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
LOG_DIR="${LOG_DIR:-/var/log/ai-platform}"
MINIO_DATA="${MINIO_DATA:-/var/lib/minio}"
PY="$ROOT/.venv/bin/python"
mkdir -p "$LOG_DIR"

[[ -f .env ]] || { echo "No .env: run scripts/generate_env.py --native first." >&2; exit 1; }
env_value() { grep -m1 "^$1=" .env | cut -d= -f2-; }

# Background services are tracked by PID file: matching command lines with pgrep also
# matches unrelated shells whose text happens to mention the service.
alive() { [[ -f "$LOG_DIR/$1.pid" ]] && kill -0 "$(cat "$LOG_DIR/$1.pid")" 2>/dev/null; }

start_bg() { # name, command...: start it unless its PID file points at a live process
  local name=$1; shift
  alive "$name" && return 0
  nohup "$@" >> "$LOG_DIR/$name.log" 2>&1 &
  echo $! > "$LOG_DIR/$name.pid"
}

tunnel_url() { grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/$1.log" 2>/dev/null | tail -1; }

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
  up retention "alive retention"
  up loki "curl -sf http://127.0.0.1:3100/ready"
  up alloy "curl -sf http://127.0.0.1:12345/-/ready"
  up prometheus "curl -sf http://127.0.0.1:9090/-/ready"
  up grafana "curl -sf http://127.0.0.1:3000/api/health"
  up app-tunnel "alive app-tunnel"
  up grafana-tunnel "alive grafana-tunnel"
  echo
  echo "  App:        $(tunnel_url app-tunnel)/ui/"
  echo "  Dashboards: $(tunnel_url grafana-tunnel)/   (user admin, password GRAFANA_PASSWORD in .env)"
  echo "  The tunnel addresses change whenever the tunnels restart."
}

if [[ "${1:-}" == "status" ]]; then status; exit 0; fi

echo "Starting services (logs in $LOG_DIR):"

pg_isready -q -h 127.0.0.1 -p 5432 || pg_ctlcluster 16 main start
redis-cli -h 127.0.0.1 ping >/dev/null 2>&1 || redis-server /etc/redis/redis.conf

if command -v minio >/dev/null; then
  mkdir -p "$MINIO_DATA"
  curl -sf http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1 || \
    MINIO_ROOT_USER="$(env_value MINIO_ACCESS_KEY)" MINIO_ROOT_PASSWORD="$(env_value MINIO_SECRET_KEY)" \
    start_bg minio minio server "$MINIO_DATA" --address 127.0.0.1:9000 --console-address 127.0.0.1:9001
else
  echo "  minio is not installed: attachments won't work (docs/SERVER_SETUP.md, Without Docker)." >&2
fi

# Monitoring first, so the gateway's logs have somewhere to go from its first line.
mkdir -p /var/lib/loki /var/lib/prometheus/native /var/lib/alloy
start_bg loki loki -config.file="$ROOT/deploy/native/loki.yaml"
start_bg alloy alloy run --server.http.listen-addr=127.0.0.1:12345 --storage.path=/var/lib/alloy \
  --disable-reporting "$ROOT/deploy/native/alloy.alloy"
start_bg prometheus prometheus --config.file="$ROOT/deploy/native/prometheus.yml" \
  --storage.tsdb.path=/var/lib/prometheus/native --storage.tsdb.retention.time=15d \
  --storage.tsdb.retention.size=2GB --web.listen-address=127.0.0.1:9090
AI_PLATFORM_ROOT="$ROOT" \
GF_SERVER_HTTP_ADDR=127.0.0.1 GF_SERVER_HTTP_PORT=3000 \
GF_SECURITY_ADMIN_PASSWORD="$(env_value GRAFANA_PASSWORD)" GF_USERS_ALLOW_SIGN_UP=false \
GF_ANALYTICS_REPORTING_ENABLED=false GF_ANALYTICS_CHECK_FOR_UPDATES=false \
GF_PATHS_PROVISIONING="$ROOT/deploy/native/grafana" GF_PATHS_DATA=/var/lib/grafana GF_PATHS_LOGS="$LOG_DIR" \
  start_bg grafana grafana server --homepath /usr/share/grafana --config /etc/grafana/grafana.ini

# The model can take minutes to load; the gateway doesn't wait for it (/readyz reports it).
# SKIP_VLLM=1 starts everything else, e.g. while the weights are still downloading.
if [[ -z "${SKIP_VLLM:-}" ]] && ! curl -sf http://127.0.0.1:8001/v1/models >/dev/null 2>&1; then
  start_bg vllm scripts/run_vllm.sh
fi

if ! curl -sf http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
  "$PY" -m alembic upgrade head >> "$LOG_DIR/gateway.log" 2>&1 || { echo "migrations failed, see $LOG_DIR/gateway.log" >&2; exit 1; }
  start_bg gateway "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
fi

# Deletes chat attachments after 3 days, hourly, like the compose "retention" service.
PY="$PY" start_bg retention bash -c 'while true; do "$PY" -m scripts.purge_history || echo purge failed; sleep 3600; done'

# Public access through Cloudflare quick tunnels: no domain or account, outbound only, no open
# ports. Each address is random and changes when its tunnel restarts (see `status`).
if command -v cloudflared >/dev/null; then
  alive app-tunnel || : > "$LOG_DIR/app-tunnel.log"
  start_bg app-tunnel cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000
  alive grafana-tunnel || : > "$LOG_DIR/grafana-tunnel.log"
  start_bg grafana-tunnel cloudflared tunnel --no-autoupdate --url http://127.0.0.1:3000
fi

for _ in $(seq 30); do curl -sf http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break; sleep 1; done
for _ in $(seq 20); do [[ -n "$(tunnel_url app-tunnel)" && -n "$(tunnel_url grafana-tunnel)" ]] && break; sleep 1; done
status
