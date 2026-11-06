#!/usr/bin/env bash
# Start the whole platform without Docker, on a host where the server is itself a container
# (no systemd, no docker). Safe to rerun: anything already running is left alone.
#
#   scripts/start_native.sh            # start everything
#   scripts/start_native.sh status     # show what is up, and the public addresses
#
# Logs go to $LOG_DIR (default /var/log/ai-platform). Every service listens on localhost only;
# the Cloudflare tunnel(s) are the only way in (scripts/public_urls.sh: own domain or quick tunnels). Setup: "Without Docker" in docs/SERVER_SETUP.md.
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

source scripts/public_urls.sh

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
  up scheduler "alive scheduler"
  up loki "curl -sf http://127.0.0.1:3100/ready"
  up alloy "curl -sf http://127.0.0.1:12345/-/ready"
  up prometheus "curl -sf http://127.0.0.1:9090/-/ready"
  up grafana "curl -sf http://127.0.0.1:3000/api/health"
  [[ -n "$(env_value PROXY_SSH)" ]] && up proxy-tunnel "timeout 8 curl -s -o /dev/null --socks5-hostname 127.0.0.1:1080 https://www.bing.com/"
  if named_tunnel; then
    up tunnel "alive tunnel && tunnel_ready"
  else
    up app-tunnel "alive app-tunnel"
    up grafana-tunnel "alive grafana-tunnel"
  fi
  up watchdog "alive watchdog"
  echo
  echo "  App:        $(app_url)/ui/"
  echo "  Dashboards: $(grafana_url)/   (user admin, password GRAFANA_PASSWORD in .env)"
  named_tunnel || echo "  The tunnel addresses change whenever the tunnels restart."
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
# Prometheus sends alerts to the gateway with this token (deploy/native/prometheus.yml).
( umask 077; printf '%s' "$(env_value ALERT_WEBHOOK_TOKEN)" > /var/lib/prometheus/alert-token )
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

# Outbound proxy over SSH (docs/SERVER_SETUP.md, "Outbound proxy"): PROXY_SSH=user@host in .env opens a
# SOCKS proxy on 127.0.0.1:1080 through that server; set OUTBOUND_PROXY=socks5h://127.0.0.1:1080 to use it.
# The hop is encrypted, so the network here sees only an SSH connection. Restarted if it drops.
if [[ -n "$(env_value PROXY_SSH)" ]]; then
  PROXY_SSH="$(env_value PROXY_SSH)" PROXY_SSH_PORT="$(env_value PROXY_SSH_PORT)" PROXY_SSH_KEY="$(env_value PROXY_SSH_KEY)" \
  start_bg proxy-tunnel bash -c 'while true; do
    ssh -N -D 127.0.0.1:1080 -p "${PROXY_SSH_PORT:-22}" -i "${PROXY_SSH_KEY:-/root/.ssh/proxy_vps}" \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
      -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$PROXY_SSH"
    echo "proxy tunnel dropped; reconnecting in 5s"; sleep 5
  done'
  sleep 3
fi

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

# Reminders, morning mail digests, and cleanup of trash and undo copies (scripts/scheduler.py).
start_bg scheduler "$PY" -m scripts.scheduler

# Public access through Cloudflare: outbound only, no open ports. With CF_TUNNEL set up, one named
# tunnel serves the own domain (fixed addresses); otherwise two quick tunnels with random addresses
# that change when they restart (see `status`).
stop_bg() { alive "$1" && kill "$(cat "$LOG_DIR/$1.pid")" 2>/dev/null; rm -f "$LOG_DIR/$1.pid"; }
if ! command -v cloudflared >/dev/null; then
  :
elif named_tunnel; then
  stop_bg app-tunnel; stop_bg grafana-tunnel
  start_bg tunnel cloudflared tunnel --no-autoupdate --metrics "$CF_METRICS" --config "$CF_CONFIG" run "$CF_TUNNEL"
else
  stop_bg tunnel
  alive app-tunnel || : > "$LOG_DIR/app-tunnel.log"
  start_bg app-tunnel cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000
  alive grafana-tunnel || : > "$LOG_DIR/grafana-tunnel.log"
  start_bg grafana-tunnel cloudflared tunnel --no-autoupdate --url http://127.0.0.1:3000
fi

# Restarts whatever goes down and emails the admin new links (scripts/watchdog.sh).
start_bg watchdog scripts/watchdog.sh

for _ in $(seq 30); do curl -sf http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break; sleep 1; done
if named_tunnel; then
  for _ in $(seq 20); do tunnel_ready && break; sleep 1; done
else
  for _ in $(seq 20); do [[ -n "$(app_url)" && -n "$(grafana_url)" ]] && break; sleep 1; done
fi
status
