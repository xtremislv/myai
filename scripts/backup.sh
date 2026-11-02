#!/usr/bin/env bash
# Back up the database and stored files, and copy them off this server.
# Run from the project folder. deploy/systemd/ai-platform-backup.timer runs it nightly.
#
# Private chats stay encrypted inside the backup. Department work, reports and user records
# do not, so use an encrypted destination (for example an rclone "crypt" remote).
# .env is deliberately left out: store it separately, or the two together unlock everything.
set -euo pipefail

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
fi

BACKUP_DIR="${BACKUP_DIR:-/var/backups/ai-platform}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"
BACKUP_DEST="${BACKUP_DEST:-}"
PROJECT="${COMPOSE_PROJECT_NAME:-ai-platform}"
ALPINE_IMAGE="alpine:3.24.3"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="ai-platform-${stamp}"
work="${BACKUP_DIR}/${name}"
mkdir -p "$work"
trap 'rm -rf "$work"' EXIT

echo "Backing up the database..."
docker compose exec -T postgres pg_dump -U ai -Fc ai_platform > "${work}/database.dump"
if [[ ! -s "${work}/database.dump" ]]; then
  echo "The database dump is empty. Stopping so a bad backup isn't kept." >&2
  exit 1
fi

echo "Backing up stored files..."
docker run --rm \
  -v "${PROJECT}_minio-data:/data:ro" \
  -v "${work}:/backup" \
  "$ALPINE_IMAGE" tar czf /backup/minio.tar.gz -C /data .

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
