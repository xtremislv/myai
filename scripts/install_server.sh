#!/usr/bin/env bash
# Install everything the platform needs on a fresh Ubuntu 24.04 server (a normal VM with systemd), for
# Claude through the API: no GPU, no vLLM. Safe to rerun: what is already installed is left alone.
#
#   git clone -b <branch> <repo> /root/myai && cd /root/myai
#   scripts/install_server.sh                # packages, Python env, browser, sandbox, Whisper, boot service
#   scripts/install_server.sh --no-voice     # skip Whisper (voice input off)
#
# Then either move the old server's data in (scripts/migrate_import.sh, docs/MIGRATION_CLAUDE.md) or, for a
# brand-new install, create .env with scripts/generate_env.py --native --provider anthropic and run
# scripts/install_server.sh --database, then scripts/start_native.sh.
#
# What runs where (all on 127.0.0.1; the Cloudflare tunnel is the only way in, no ports to open):
#   Postgres 16 :5432, Redis :6379 (systemd, Ubuntu packages), MinIO :9000, gateway :8000, Whisper :8011,
#   Loki :3100, Alloy :4317/:12345, Prometheus :9090, Grafana :3000, scheduler, watchdog, cloudflared.
#   systemd unit ai-platform.service runs scripts/start_native.sh at boot; ai-platform-backup.timer nightly.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
VOICE=1
DATABASE=""
for arg in "$@"; do
  case "$arg" in
    --no-voice) VOICE="" ;;
    --database) DATABASE=1 ;;
    -h|--help) sed -n 2,17p "$0"; exit 0 ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
. /etc/os-release
[[ "${VERSION_CODENAME:-}" == noble ]] || echo "Note: written for Ubuntu 24.04 (noble); this is ${PRETTY_NAME:-unknown}."
CODENAME="${VERSION_CODENAME:-noble}"
step() { echo; echo "== $*"; }
export DEBIAN_FRONTEND=noninteractive

database() {
  # The app's role and database, with the password from .env (needs .env). Skipped if they exist.
  local pw
  pw=$(grep -m1 '^POSTGRES_PASSWORD=' .env | cut -d= -f2- || true)
  [[ -n "$pw" ]] || { echo "No POSTGRES_PASSWORD in .env." >&2; return 1; }
  if ! su postgres -c "psql -tAc \"select 1 from pg_roles where rolname='ai'\"" | grep -q 1; then
    su postgres -c "psql -q" <<<"CREATE ROLE ai LOGIN PASSWORD '$pw';"
  else
    su postgres -c "psql -q" <<<"ALTER ROLE ai PASSWORD '$pw';"
  fi
  su postgres -c "psql -tAc \"select 1 from pg_database where datname='ai_platform'\"" | grep -q 1 || \
    su postgres -c "createdb -O ai ai_platform"
  # Redis password, so local processes (the code sandbox runs as nobody) can't use it.
  local rpw
  rpw=$(grep -m1 '^REDIS_PASSWORD=' .env | cut -d= -f2- || true)
  if [[ -n "$rpw" ]]; then
    sed -i '/^requirepass /d' /etc/redis/redis.conf
    echo "requirepass $rpw" >> /etc/redis/redis.conf
    chown redis:redis /etc/redis/redis.conf && chmod 640 /etc/redis/redis.conf
    systemctl restart redis-server
  fi
  echo "database ready (role ai, database ai_platform)"
}

if [[ -n "$DATABASE" ]]; then database; exit 0; fi

step "System packages"
apt-get update -q
apt-get install -y -q --no-install-recommends ca-certificates curl gnupg git gcc g++ make ffmpeg rsync \
  python3 python3-venv postgresql-16 postgresql-client-16 redis-server openssh-client tar xz-utils

step "Postgres and Redis: local only"
grep -q "^listen_addresses = 'localhost'" /etc/postgresql/16/main/postgresql.conf || \
  echo "listen_addresses = 'localhost'" >> /etc/postgresql/16/main/postgresql.conf
sed -i 's/^bind .*/bind 127.0.0.1 ::1/; s/^supervised .*/supervised systemd/; s/^daemonize .*/daemonize no/' \
  /etc/redis/redis.conf
systemctl enable --now postgresql redis-server
systemctl restart postgresql redis-server

step "Cloudflare tunnel (cloudflared), Grafana, Loki, Alloy, Prometheus"
install -d -m 0755 /usr/share/keyrings /etc/apt/keyrings
[[ -f /usr/share/keyrings/cloudflare-main.gpg ]] || \
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $CODENAME main" \
  > /etc/apt/sources.list.d/cloudflared.list
[[ -f /etc/apt/keyrings/grafana.gpg ]] || \
  curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" > /etc/apt/sources.list.d/grafana.list
apt-get update -q
apt-get install -y -q --no-install-recommends cloudflared grafana=13.0.2 loki=3.7.9 alloy prometheus
# start_native.sh runs these with the project's configs (deploy/native/): the packages' own services would
# take the same ports with default settings.
for svc in grafana-server loki alloy prometheus prometheus-node-exporter; do
  systemctl disable --now "$svc" >/dev/null 2>&1 || true
done

step "MinIO"
if command -v minio >/dev/null; then
  echo "already installed: $(minio --version | head -1)"
else
  # No free binaries any more (dl.min.io returns 410): build the release compose pins, from source.
  # scripts/migrate_import.sh installs the old server's binary instead, when the bundle has it.
  mkdir -p /root/build && cd /root/build
  curl -sSL https://go.dev/dl/go1.26.10.linux-amd64.tar.gz | tar -xz
  git clone -q --depth 1 --branch RELEASE.2025-09-06T17-38-46Z https://github.com/minio/minio.git minio-src
  (cd minio-src && CGO_ENABLED=0 GOTOOLCHAIN=local /root/build/go/bin/go build -trimpath -o /usr/local/bin/minio .)
  cd "$ROOT" && rm -rf /root/build /root/go /root/.cache/go-build
  minio --version | head -1
fi

step "Python environment (.venv)"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
[[ -x .venv/bin/python ]] || uv venv -q -p 3.12 .venv
if [[ -n "$VOICE" ]]; then
  VIRTUAL_ENV=.venv uv pip install -q -e ".[voice]"
else
  VIRTUAL_ENV=.venv uv pip install -q -e .
fi
.venv/bin/python -c "import anthropic, fastapi; print('anthropic', anthropic.__version__)"

step "Headless browser for the agents (Chromium)"
.venv/bin/playwright install --with-deps chromium

step "Code sandbox (/opt/agent-sandbox)"
scripts/setup_sandbox.sh

if [[ -n "$VOICE" ]]; then
  step "Voice input: Whisper large-v3-turbo on the CPU (~1.6 GB)"
  STT_MODEL=$(grep -m1 '^STT_MODEL=' .env 2>/dev/null | cut -d= -f2- || true)
  STT_MODEL="${STT_MODEL:-/root/models/whisper-large-v3-turbo}"
  if [[ -f "$STT_MODEL/model.bin" ]]; then
    echo "already downloaded: $STT_MODEL"
  else
    .venv/bin/python -c "from huggingface_hub import snapshot_download as d; \
d('mobiuslabsgmbh/faster-whisper-large-v3-turbo', local_dir='$STT_MODEL')"
  fi
fi

step "Swap (4 GB), so a burst of browser sessions can't kill the gateway"
if ! swapon --show | grep -q .; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "vm.swappiness=10" > /etc/sysctl.d/90-ai-platform.conf && sysctl -q -p /etc/sysctl.d/90-ai-platform.conf
fi

step "Start on boot (systemd)"
install -m 0644 deploy/systemd/ai-platform-native.service /etc/systemd/system/ai-platform.service
install -m 0644 deploy/systemd/ai-platform-backup-native.service /etc/systemd/system/ai-platform-backup.service
install -m 0644 deploy/systemd/ai-platform-backup.timer /etc/systemd/system/ai-platform-backup.timer
sed -i "s#/root/myai#$ROOT#g" /etc/systemd/system/ai-platform.service /etc/systemd/system/ai-platform-backup.service
systemctl daemon-reload
systemctl enable ai-platform.service >/dev/null
systemctl enable --now ai-platform-backup.timer >/dev/null  # enable alone would wait for a reboot
echo "enabled: ai-platform.service (starts at boot), ai-platform-backup.timer (nightly)"

if [[ -f .env ]]; then
  step "Database role and password from .env"
  database
fi

echo
echo "Installed. Next:"
if [[ -f .env ]]; then
  echo "  systemctl start ai-platform && scripts/start_native.sh status"
else
  echo "  moving from the old server:  scripts/migrate_import.sh <bundle.tar>   (docs/MIGRATION_CLAUDE.md)"
  echo "  new install:  python3 scripts/generate_env.py --native --provider anthropic --domain ... --grafana-domain ..."
  echo "                then put the key in ANTHROPIC_API_KEY, scripts/install_server.sh --database,"
  echo "                .venv/bin/python -m alembic upgrade head, systemctl start ai-platform"
fi
