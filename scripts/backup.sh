#!/usr/bin/env bash
# Back up the database and stored files, and copy them off this server.
# Run from the project folder. deploy/systemd/ai-platform-backup.timer runs it nightly.
#
# Private chats stay encrypted inside the backup. Department work, reports and user records
# do not, so use an encrypted destination (for example an rclone "crypt" remote).
# .env is deliberately left out: store it separately, or the two together unlock everything.
#
# Without Docker (scripts/start_native.sh), it dumps Postgres with pg_dump over POSTGRES_DSN and
# archives MinIO's data folder (MINIO_DATA, default /var/lib/minio). BACKUP_MODE=native|docker
# overrides the choice, which is otherwise "docker if the docker command exists".
set -euo pipefail

# Read .env as plain KEY=value lines: sourcing it would run values with spaces as commands.
if [[ -f .env ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    export "${line%%=*}=${line#*=}"
  done < .env
fi

BACKUP_DIR="${BACKUP_DIR:-/var/backups/ai-platform}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"
BACKUP_DEST="${BACKUP_DEST:-}"
PROJECT="${COMPOSE_PROJECT_NAME:-ai-platform}"
ALPINE_IMAGE="alpine:3.24.3"
MINIO_DATA="${MINIO_DATA:-/var/lib/minio}"
if [[ -z "${BACKUP_MODE:-}" ]]; then
  if command -v docker >/dev/null; then BACKUP_MODE=docker; else BACKUP_MODE=native; fi
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="ai-platform-${stamp}"
work="${BACKUP_DIR}/${name}"
mkdir -p "$work"
trap 'rm -rf "$work"' EXIT

echo "Backing up the database..."
if [[ "$BACKUP_MODE" == native ]]; then
  pg_dump --dbname="$POSTGRES_DSN" -Fc > "${work}/database.dump"
else
  docker compose exec -T postgres pg_dump -U ai -Fc ai_platform > "${work}/database.dump"
fi
if [[ ! -s "${work}/database.dump" ]]; then
  echo "The database dump is empty. Stopping so a bad backup isn't kept." >&2
  exit 1
fi

echo "Backing up stored files..."
if [[ "$BACKUP_MODE" == native ]]; then
  tar czf "${work}/minio.tar.gz" -C "$MINIO_DATA" .
else
  docker run --rm \
    -v "${PROJECT}_minio-data:/data:ro" \
    -v "${work}:/backup" \
    "$ALPINE_IMAGE" tar czf /backup/minio.tar.gz -C /data .
fi

(cd "$work" && sha256sum database.dump minio.tar.gz > SHA256SUMS)
(cd "$BACKUP_DIR" && tar -cf "${name}.tar" "$name")
echo "Created ${BACKUP_DIR}/${name}.tar"

if [[ -n "$BACKUP_DEST" ]]; then
  echo "Copying to ${BACKUP_DEST}..."
  rclone copy "${BACKUP_DIR}/${name}.tar" "$BACKUP_DEST"
  echo "Copied off this server."
else
  echo "WARNING: this backup is NOT off this server. It is lost if the instance is wiped." >&2
  echo "Set BACKUP_DEST in .env to an rclone destination." >&2
fi

find "$BACKUP_DIR" -maxdepth 1 -name 'ai-platform-*.tar' -mtime "+${BACKUP_KEEP_DAYS}" -delete
echo "Backup finished."
