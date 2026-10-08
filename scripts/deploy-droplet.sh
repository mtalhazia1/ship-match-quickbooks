#!/usr/bin/env bash
# Deploy ShipMatch to one Ubuntu server (a DigitalOcean droplet, or any Ubuntu 22.04/24.04 VM).
# Guide: deploy/digitalocean.md. Safe to run again: every step checks what is already done.
#
# From your computer (needs ssh and rsync; SSH in as root, the default on a new droplet):
#   DOMAIN=shipmatch.example.com ./scripts/deploy-droplet.sh push root@203.0.113.10
#       copies this folder to /opt/shipmatch/releases/<time> on the server and runs "install" there.
#       Run it again for every update.
#
# On the server, as root (from /opt/shipmatch/current):
#   scripts/deploy-droplet.sh install      set up Docker, firewall, .env, start this release (idempotent)
#   scripts/deploy-droplet.sh rollback     go back to the previous release
#   scripts/deploy-droplet.sh backup       database dump and uploaded files now (also runs nightly)
#   scripts/deploy-droplet.sh restore-db backups/db-<time>.sql.gz
#   scripts/deploy-droplet.sh status       containers, health and the URLs to register
#   scripts/deploy-droplet.sh manage <command>   e.g. manage createsuperuser
#   scripts/deploy-droplet.sh logs [service]
set -euo pipefail

APP="${SHIPMATCH_HOME:-/opt/shipmatch}"
PROJECT="shipmatch"
BACKUPS="$APP/backups"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
KEEP_RELEASES=5
MIN_COMPOSE="2.24.0"   # "!reset" / "!override" in docker-compose.prod.yml

log() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mWarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

script_dir() { cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd; }
release_dir() { dirname "$(script_dir)"; }

need_root() { [[ "$(id -u)" -eq 0 ]] || die "Run this on the server as root (or with sudo)."; }

valid_domain() { [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]]; }

# docker compose for the active release, always under the same project name so volumes are kept.
dc() {
  local dir
  dir="$(readlink -f "$APP/current")"
  docker compose -p "$PROJECT" --project-directory "$dir" -f "$dir/docker-compose.yml" -f "$dir/docker-compose.prod.yml" "$@"
}

# ----------------------------------------------------------------------------- .env helpers (values never printed)

env_file() { echo "$APP/shared/.env"; }

get_env() { # key -> value ('' when missing)
  local line
  line="$(grep -E "^$1=" "$(env_file)" 2>/dev/null | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}

set_env() { # key value
  local file tmp
  file="$(env_file)"
  tmp="$(mktemp)"
  awk -v k="$1" -v v="$2" 'BEGIN { done = 0 }
    index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' "$file" > "$tmp"
  cat "$tmp" > "$file"
  rm -f "$tmp"
}

set_if_empty() { # key value: only when missing, empty or a placeholder
  local current
  current="$(get_env "$1")"
  if [[ -z "$current" || "$current" == "change-me" ]]; then
    set_env "$1" "$2"
    return 0
  fi
  return 1
}

random_alnum() { openssl rand -base64 64 | tr -dc 'A-Za-z0-9' | head -c "$1"; }
fernet_key() { openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'; }

# ----------------------------------------------------------------------------- server setup

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "Docker is installed."
  else
    log "Installing Docker Engine and the Compose plugin from Docker's apt repository."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q ca-certificates curl gnupg openssl
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
  fi
  local version
  version="$(docker compose version --short 2>/dev/null | sed 's/^v//')"
  if [[ "$(printf '%s\n%s\n' "$MIN_COMPOSE" "$version" | sort -V | head -n 1)" != "$MIN_COMPOSE" ]]; then
    die "Docker Compose $version is too old; $MIN_COMPOSE or newer is needed. Run: apt-get install --only-upgrade docker-compose-plugin"
  fi
}

setup_firewall() {
  command -v ufw >/dev/null 2>&1 || apt-get install -y -q ufw
  ufw allow 22/tcp >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
  log "Firewall: only SSH (22), HTTP (80) and HTTPS (443) are open."
}

setup_swap() {
  # Small droplets (1 GB) run out of memory while building the image without swap.
  if [[ -z "$(swapon --show --noheadings 2>/dev/null)" && ! -f /swapfile ]]; then
    log "Adding a 2 GB swap file."
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
}

write_env() { # release dir
  local rel="$1" file domain created=0
  file="$(env_file)"
  mkdir -p "$APP/shared"
  if [[ ! -f "$file" ]]; then
    cp "$rel/.env.example" "$file"
    created=1
  fi
  chmod 600 "$file"
  domain="${DOMAIN:-$(get_env DOMAIN)}"
  [[ -n "$domain" && "$domain" != "localhost" ]] || die "Set DOMAIN to the name visitors will use, e.g. DOMAIN=shipmatch.example.com (its DNS A record must point to this server)."
  valid_domain "$domain" || die "DOMAIN '$domain' is not a valid host name."
  set_env DOMAIN "$domain"
  set_if_empty DJANGO_SECRET_KEY "$(random_alnum 64)" && log "Generated DJANGO_SECRET_KEY."
  set_if_empty FIELD_ENCRYPTION_KEY "$(fernet_key)" && log "Generated FIELD_ENCRYPTION_KEY (keep a copy: it decrypts QuickBooks tokens)."
  set_if_empty POSTGRES_PASSWORD "$(random_alnum 32)" && log "Generated POSTGRES_PASSWORD."
  set_env DJANGO_DEBUG 0
  set_env DJANGO_ALLOWED_HOSTS "$domain"
  set_env CELERY_TASK_ALWAYS_EAGER 0
  set_env TRUST_X_FORWARDED_FOR 1
  set_env SITE_URL "https://$domain"
  set_env QBO_REDIRECT_URI "https://$domain/accounting/qbo/callback"
  if [[ "$created" -eq 1 ]]; then
    set_env S3_BUCKET ""            # uploaded files in the "media" Docker volume (set S3_* to use S3 instead)
    set_env DATABASE_URL ""         # set by docker-compose.prod.yml
    log "Created $file from .env.example (readable by root only). Add provider keys there, then run install again."
  fi
  ln -sfn "$file" "$rel/.env"
}

install_cron() {
  local cron=/etc/cron.d/shipmatch
  cat > "$cron" <<EOF
# Nightly ShipMatch backup (database + uploaded files), kept $KEEP_DAYS days. Managed by deploy-droplet.sh.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
15 2 * * * root $APP/current/scripts/deploy-droplet.sh backup >> $BACKUPS/backup.log 2>&1
EOF
  chmod 644 "$cron"
}

wait_healthy() {
  log "Waiting for the app to answer its health check."
  local i
  for i in $(seq 1 60); do
    if dc exec -T web python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready/', timeout=5)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

prune_releases() {
  local keep_current keep_previous
  keep_current="$(readlink -f "$APP/current" 2>/dev/null || true)"
  keep_previous="$(cat "$APP/previous" 2>/dev/null || true)"
  # Newest first; keep the last few plus whatever current and previous point to.
  find "$APP/releases" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r | tail -n +"$((KEEP_RELEASES + 1))" |
    while read -r old; do
      [[ "$APP/releases/$old" == "$keep_current" || "$APP/releases/$old" == "$keep_previous" ]] && continue
      rm -rf "${APP:?}/releases/${old:?}"
    done
}

activate() { # release dir: point "current" at it, build and start
  local rel="$1" before
  before="$(readlink -f "$APP/current" 2>/dev/null || true)"
  if [[ -n "$before" && "$before" != "$rel" && -d "$before" ]]; then
    if dc ps --status running --services 2>/dev/null | grep -qx db; then
      log "Backing up before switching releases."
      backup "pre-update"
    fi
    echo "$before" > "$APP/previous"
  fi
  ln -sfn "$rel" "$APP/current"
  log "Building and starting $(basename "$rel")."
  dc up -d --build --remove-orphans
}

seed_if_new() {
  local demo exists
  demo="$(get_env DEMO_MODE)"
  if [[ "$demo" == "1" || "$demo" == "true" ]]; then
    exists="$(dc exec -T web python manage.py shell -c "from apps.core.models import Organization as O; print(O.objects.filter(slug='demo').exists())" 2>/dev/null | tail -n 1 || true)"
    if [[ "$exists" != "True" ]]; then
      log "DEMO_MODE is on: building the demo organization (reset_demo)."
      dc exec -T web python manage.py reset_demo
    fi
  fi
}

print_summary() {
  local domain
  domain="$(get_env DOMAIN)"
  cat <<EOF

ShipMatch is running at https://$domain/
  QuickBooks redirect URI to register in the Intuit app:  https://$domain/accounting/qbo/callback
  Reconnect URL for the Intuit app:                       https://$domain/accounting/qbo/reconnect/
  Settings:  $(env_file)   (then run: $APP/current/scripts/deploy-droplet.sh install)
  Backups:   $BACKUPS  (nightly at 02:15 server time, kept $KEEP_DAYS days; copy them off the server too)
  First admin user:  $APP/current/scripts/deploy-droplet.sh manage createsuperuser
EOF
}

cmd_install() {
  need_root
  local rel
  rel="$(release_dir)"
  [[ "$rel" == "$APP/releases/"* ]] || die "Put the project in $APP/releases/<name>/ first (push does this), then run its scripts/deploy-droplet.sh install."
  [[ -f "$rel/docker-compose.prod.yml" ]] || die "$rel does not look like a ShipMatch folder."
  install_docker
  setup_firewall
  setup_swap
  mkdir -p "$BACKUPS" && chmod 700 "$BACKUPS"
  write_env "$rel"
  getent hosts "$(get_env DOMAIN)" >/dev/null || warn "$(get_env DOMAIN) does not resolve yet. HTTPS starts working once its DNS A record points to this server."
  activate "$rel"
  wait_healthy || die "The app did not become healthy. See: $APP/current/scripts/deploy-droplet.sh logs web   To go back: $APP/current/scripts/deploy-droplet.sh rollback"
  seed_if_new
  install_cron
  prune_releases
  print_summary
}

cmd_rollback() {
  need_root
  local prev current
  prev="$(cat "$APP/previous" 2>/dev/null || true)"
  current="$(readlink -f "$APP/current")"
  [[ -n "$prev" && -d "$prev" ]] || die "No previous release to go back to."
  [[ "$prev" != "$current" ]] || die "The previous release is already active."
  log "Rolling back from $(basename "$current") to $(basename "$prev")."
  ln -sfn "$APP/shared/.env" "$prev/.env"
  ln -sfn "$prev" "$APP/current"
  echo "$current" > "$APP/previous"
  dc up -d --build --remove-orphans
  wait_healthy || die "The previous release did not become healthy either. See: $APP/current/scripts/deploy-droplet.sh logs web"
  local pre
  pre="$(ls -1t "$BACKUPS"/db-*-pre-update.sql.gz 2>/dev/null | head -n 1 || true)"
  log "Rolled back the code. If the update changed the database, also restore the backup taken just before it:"
  echo "  $APP/current/scripts/deploy-droplet.sh restore-db ${pre:-$BACKUPS/db-<time>-pre-update.sql.gz}"
}

backup() { # [label]
  local stamp label="${1:-}"
  stamp="$(date -u +%Y%m%d-%H%M%S)${label:+-$label}"
  mkdir -p "$BACKUPS" && chmod 700 "$BACKUPS"
  dc exec -T db pg_dump -U shipmatch -d shipmatch --no-owner --clean --if-exists | gzip > "$BACKUPS/db-$stamp.sql.gz.part"
  mv "$BACKUPS/db-$stamp.sql.gz.part" "$BACKUPS/db-$stamp.sql.gz"
  if dc exec -T web test -d /app/media; then
    dc exec -T web tar czf - -C /app/media . > "$BACKUPS/media-$stamp.tar.gz.part"
    mv "$BACKUPS/media-$stamp.tar.gz.part" "$BACKUPS/media-$stamp.tar.gz"
  fi
  chmod 600 "$BACKUPS"/*-"$stamp".* 2>/dev/null || true
  find "$BACKUPS" -maxdepth 1 \( -name 'db-*.sql.gz' -o -name 'media-*.tar.gz' \) -mtime +"$KEEP_DAYS" -delete
  find "$BACKUPS" -maxdepth 1 -name '*.part' -mmin +120 -delete
  log "Backup $stamp written to $BACKUPS."
}

cmd_backup() { need_root; backup "${1:-}"; }

cmd_restore_db() {
  need_root
  local file="${1:-}"
  [[ -f "$file" ]] || die "Usage: deploy-droplet.sh restore-db $BACKUPS/db-<time>.sql.gz"
  if [[ "${2:-}" != "--yes" ]]; then
    read -r -p "Replace the current database with $(basename "$file")? Type yes to continue: " answer
    [[ "$answer" == "yes" ]] || die "Cancelled."
  fi
  backup "before-restore"
  log "Stopping the app while the database is restored."
  dc stop web worker beat
  gunzip -c "$file" | dc exec -T db psql -U shipmatch -d shipmatch -v ON_ERROR_STOP=1 -q >/dev/null
  dc up -d
  wait_healthy || die "The app did not become healthy after the restore. See: deploy-droplet.sh logs web"
  log "Database restored from $(basename "$file")."
}

cmd_status() {
  dc ps
  if wait_healthy_once; then log "Health check: ok"; else warn "Health check failed."; fi
  print_summary
}

wait_healthy_once() {
  dc exec -T web python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready/', timeout=5)" >/dev/null 2>&1
}

cmd_push() { # user@host (runs on your computer)
  local target="${1:-}" stamp root link=()
  [[ -n "$target" ]] || die "Usage: DOMAIN=shipmatch.example.com scripts/deploy-droplet.sh push root@<server-ip>"
  command -v rsync >/dev/null || die "rsync is needed on this computer (on Windows, use WSL, or see 'Option B' in deploy/digitalocean.md)."
  if [[ -n "${DOMAIN:-}" ]]; then valid_domain "$DOMAIN" || die "DOMAIN '$DOMAIN' is not a valid host name."; fi
  root="$(release_dir)"
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  log "Copying $root to $target:$APP/releases/$stamp"
  ssh "$target" "mkdir -p '$APP/releases/$stamp'"
  if ssh "$target" "test -d '$APP/current/'"; then link=(--link-dest="$APP/current/"); fi
  rsync -az --delete "${link[@]}" \
    --exclude '.git/' --exclude '.env' --exclude '.venv/' --exclude 'db.sqlite3' --exclude 'media/' \
    --exclude 'staticfiles/' --exclude 'datasets/' --exclude 'real_docs/' --exclude 'secrets/' \
    --exclude 'eval_reports/' --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/' \
    --exclude '.ruff_cache/' --exclude 'backups/' \
    "$root/" "$target:$APP/releases/$stamp/"
  ssh -t "$target" "DOMAIN='${DOMAIN:-}' bash '$APP/releases/$stamp/scripts/deploy-droplet.sh' install"
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    push) cmd_push "$@" ;;
    install) cmd_install ;;
    rollback) cmd_rollback ;;
    backup) cmd_backup "$@" ;;
    restore-db) cmd_restore_db "$@" ;;
    status) need_root; cmd_status ;;
    manage) need_root; dc exec web python manage.py "$@" ;;
    logs) need_root; dc logs --tail=200 -f "$@" ;;
    *) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 1 ;;
  esac
}

main "$@"
