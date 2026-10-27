#!/bin/sh
# Nightly backup of a production stack started by hand (docker-compose.yml + docker-compose.prod.yml):
# a Postgres dump and a copy of every uploaded file in the media volume. Servers set up with
# scripts/deploy-droplet.sh already back up nightly with `deploy-droplet.sh backup`; don't add this as well.
# Run from cron in the project folder, e.g.
#   0 2 * * * cd /opt/shipmatch && ./deploy/backup.sh >> backups/backup.log 2>&1
# Restore: the "restore-db" steps in scripts/deploy-droplet.sh (stop web/worker/beat, psql the dump, start),
# then untar the media archive into /app/media. Copy the backups/ folder off the server as a second step.
# With S3_BUCKET set, uploads live in the bucket, not the volume: turn on versioning or replication there.
set -e
cd "$(dirname "$0")/.."
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP=$(date -u +%Y%m%d-%H%M%S)
DC="docker compose -f docker-compose.yml -f docker-compose.prod.yml ${COMPOSE_PROJECT_NAME:+-p $COMPOSE_PROJECT_NAME}"
mkdir -p backups && chmod 700 backups
$DC exec -T db pg_dump -U shipmatch -d shipmatch --no-owner --clean --if-exists | gzip > "backups/db-$STAMP.sql.gz.part"
mv "backups/db-$STAMP.sql.gz.part" "backups/db-$STAMP.sql.gz"
if $DC exec -T web test -d /app/media; then
  $DC exec -T web tar czf - -C /app/media . > "backups/media-$STAMP.tar.gz.part"
  mv "backups/media-$STAMP.tar.gz.part" "backups/media-$STAMP.tar.gz"
fi
chmod 600 backups/*-"$STAMP".* 2>/dev/null || true
find backups -maxdepth 1 \( -name 'db-*.sql.gz' -o -name 'media-*.tar.gz' \) -mtime +"$KEEP_DAYS" -delete
find backups -maxdepth 1 -name '*.part' -mmin +120 -delete
echo "backup $STAMP done"
