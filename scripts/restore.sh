#!/usr/bin/env bash
# Replace the database and stored files with a backup made by scripts/backup.sh.
# Run from the project folder:  scripts/restore.sh /path/to/ai-platform-<time>.tar
#
# This overwrites everything currently stored. It needs the original .env, or the app can't
# open what it restores.
#
#   scripts/restore.sh --verify <archive>   restore into a scratch database and folder, check
#                                            them, and delete them again: live data is untouched.
#                                            Needs a Postgres role that may create databases
#                                            (natively: run as root, it uses the postgres user).
# Without Docker it works on the native services (BACKUP_MODE as in backup.sh).
set -euo pipefail

verify=""
if [[ "${1:-}" == "--verify" ]]; then verify=1; shift; fi
archive="${1:?Usage: scripts/restore.sh [--verify] /path/to/ai-platform-<time>.tar}"
PROJECT="${COMPOSE_PROJECT_NAME:-ai-platform}"
ALPINE_IMAGE="alpine:3.24.3"
MINIO_DATA="${MINIO_DATA:-/var/lib/minio}"
LOG_DIR="${LOG_DIR:-/var/log/ai-platform}"
if [[ -z "${BACKUP_MODE:-}" ]]; then
  if command -v docker >/dev/null; then BACKUP_MODE=docker; else BACKUP_MODE=native; fi
fi
# Read .env as plain KEY=value lines: sourcing it would run values with spaces as commands.
if [[ -f .env ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    export "${line%%=*}=${line#*=}"
  done < .env
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Checking the backup..."
if ! (cd "$(dirname "$archive")" && tar -xf "$(basename "$archive")" -C "$work") 2>/dev/null; then
  echo "This file can't be read as a backup. Nothing was changed." >&2
  exit 1
fi
folder="$(find "$work" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [[ -z "$folder" ]] || ! (cd "$folder" && sha256sum --check --quiet SHA256SUMS); then
  echo "This backup is damaged. Nothing was changed." >&2
  exit 1
fi

if [[ -n "$verify" ]]; then
  # A real restore, into throwaway places, so backups are proven without risking live data.
  scratch="restore_check_$(date +%s)"
  as_pg() { if [[ "$BACKUP_MODE" == native ]]; then su postgres -c "$1"; else docker compose exec -T postgres sh -c "$1"; fi; }
  as_pg "createdb $scratch"
  trap 'as_pg "dropdb --if-exists $scratch" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
  echo "Restoring the database into $scratch..."
  if [[ "$BACKUP_MODE" == native ]]; then
    su postgres -c "pg_restore --no-owner -d $scratch" < "${folder}/database.dump"
  else
    docker compose exec -T postgres pg_restore -U ai --no-owner -d "$scratch" < "${folder}/database.dump"
  fi
  counts="$(as_pg "psql -At -d $scratch -c \"select 'users=' || count(*) from users union all select 'secrets=' || count(*) from vault_secrets union all select 'policies=' || count(*) from acl_policies union all select 'checkpoints=' || count(*) from checkpoints union all select 'migration=' || version_num from alembic_version\"")"
  echo "$counts" | sed 's/^/  /'
  echo "$counts" | grep -q '^users=[1-9]' || { echo "The restored database has no users. This backup is not usable." >&2; exit 1; }
  files="$work/minio-check"
  mkdir -p "$files"
  tar xzf "${folder}/minio.tar.gz" -C "$files"
  echo "  stored files: $(find "$files" -type f | wc -l) file(s) unpacked"
  echo "Verified: $(basename "$archive") restores cleanly. Nothing live was changed."
  exit 0
fi

echo "This replaces ALL current data with: $(basename "$archive")"
read -r -p 'Type RESTORE to continue: ' answer
if [[ "$answer" != "RESTORE" ]]; then
  echo "Cancelled. Nothing was changed." >&2
  exit 1
fi

if [[ "$BACKUP_MODE" == native ]]; then
  echo "Stopping the app..."
  for svc in gateway scheduler retention minio; do
    [[ -f "$LOG_DIR/$svc.pid" ]] && kill "$(cat "$LOG_DIR/$svc.pid")" 2>/dev/null || true
  done
  sleep 3
  echo "Restoring the database..."
  su postgres -c "psql -d postgres -c 'DROP DATABASE IF EXISTS ai_platform WITH (FORCE);' -c 'CREATE DATABASE ai_platform OWNER ai;'"
  su postgres -c "pg_restore --no-owner --role=ai -d ai_platform" < "${folder}/database.dump"
  echo "Restoring stored files..."
  find "$MINIO_DATA" -mindepth 1 -delete
  tar xzf "${folder}/minio.tar.gz" -C "$MINIO_DATA"
  echo "Starting the app..."
  scripts/start_native.sh
else
  echo "Stopping the app..."
  docker compose stop gateway retention minio

  echo "Restoring the database..."
  docker compose exec -T postgres psql -U ai -d postgres \
    -c "DROP DATABASE IF EXISTS ai_platform WITH (FORCE);" -c "CREATE DATABASE ai_platform;"
  docker compose exec -T postgres pg_restore -U ai -d ai_platform --no-owner < "${folder}/database.dump"

  echo "Restoring stored files..."
  docker run --rm \
    -v "${PROJECT}_minio-data:/data" \
    -v "${folder}:/backup:ro" \
    "$ALPINE_IMAGE" sh -c 'find /data -mindepth 1 -delete && tar xzf /backup/minio.tar.gz -C /data'

  echo "Starting the app..."
  docker compose up -d minio gateway retention
fi
echo "Restore finished. Check it with: curl -s localhost:8000/readyz"
