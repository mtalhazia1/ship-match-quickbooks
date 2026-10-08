#!/bin/sh
# Nightly backup: Postgres dump + copy of every stored PDF. Run from cron on the server, e.g.
#   0 2 * * * cd /opt/shipmatch && ./deploy/backup.sh >> backups/backup.log 2>&1
# Copy the backups/ folder off the server (e.g. to S3 or Backblaze) as a second step.
set -e
STAMP=$(date +%Y%m%d-%H%M)
mkdir -p backups/files
docker compose exec -T db pg_dump -U shipmatch shipmatch | gzip > "backups/db-$STAMP.sql.gz"
docker compose run --rm -v "$PWD/backups/files:/backup" --entrypoint sh minio-init -c \
  "mc alias set local http://minio:9000 minioadmin minioadmin >/dev/null && mc mirror --overwrite local/shipmatch /backup"
find backups -name 'db-*.sql.gz' -mtime +14 -delete
echo "backup $STAMP done"
