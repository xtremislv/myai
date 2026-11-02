#!/usr/bin/env bash
# Replace the database and stored files with a backup made by scripts/backup.sh.
# Run from the project folder:  scripts/restore.sh /path/to/ai-platform-<time>.tar
#
# This overwrites everything currently stored. It needs the original .env, or the app can't
# open what it restores.
set -euo pipefail

archive="${1:?Usage: scripts/restore.sh /path/to/ai-platform-<time>.tar}"
PROJECT="${COMPOSE_PROJECT_NAME:-ai-platform}"
ALPINE_IMAGE="alpine:3.24.3"

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

echo "This replaces ALL current data with: $(basename "$archive")"
read -r -p 'Type RESTORE to continue: ' answer
if [[ "$answer" != "RESTORE" ]]; then
  echo "Cancelled. Nothing was changed." >&2
  exit 1
fi

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
echo "Restore finished. Check it with: curl -s localhost:8000/readyz"
