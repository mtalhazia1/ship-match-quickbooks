# Hosting ShipMatch on one DigitalOcean droplet

One Ubuntu server runs everything with Docker: the web app (gunicorn), two Celery processes (worker
and beat), Postgres, Redis and Caddy, which gets and renews the HTTPS certificate for your domain
automatically. `scripts/deploy-droplet.sh` does the setup and is safe to run again at any time.

Good for a pilot or a public demo. For several customers in production, add managed Postgres,
off-site backups and monitoring (see "Going further").

## What you need

| Item | Notes |
| --- | --- |
| A droplet | Ubuntu 24.04 (or 22.04), Basic, **2 GB RAM** or more (1 GB works; the script adds swap). Add your SSH key when creating it. |
| A domain name | For example `shipmatch.example.com`. Create a DNS **A record** pointing to the droplet's IP address. |
| On your computer | `ssh` and `rsync` (macOS and Linux have them; on Windows use WSL, or Option B below). |

Ports: the script opens only 22 (SSH), 80 and 443 in the server firewall (ufw). Postgres and Redis
are never published; only Caddy listens on the internet.

## First deployment

### Option A: from your computer (recommended)

From the project folder:

```bash
DOMAIN=shipmatch.example.com ./scripts/deploy-droplet.sh push root@203.0.113.10
```

This copies the project (without `.env`, databases, uploads, `real_docs/` or `.git`) to
`/opt/shipmatch/releases/<time>/` on the droplet and runs `install` there, which:

1. installs Docker Engine and the Compose plugin from Docker's apt repository (once);
2. turns on the firewall for ports 22, 80 and 443, and adds a 2 GB swap file if there is none;
3. creates `/opt/shipmatch/shared/.env` from `.env.example` with new random values for
   `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEY` and `POSTGRES_PASSWORD` (readable by root only, never printed),
   and sets `DOMAIN`, `DJANGO_DEBUG=0` and the QuickBooks redirect URI;
4. starts `docker compose -f docker-compose.yml -f docker-compose.prod.yml` under the project name
   `shipmatch`, waits for `/health/ready/`, and builds the demo organization when `DEMO_MODE=1`;
5. installs a nightly backup (02:15 server time) and keeps the last 5 releases.

### Option B: on the droplet only (for example from Windows without WSL)

```bash
ssh root@203.0.113.10
apt-get update && apt-get install -y git
git clone <your repository URL> /opt/shipmatch/releases/$(date -u +%Y%m%d-%H%M%S)
cd /opt/shipmatch/releases/<that folder>
DOMAIN=shipmatch.example.com ./scripts/deploy-droplet.sh install
```

### After the first run

1. Open `https://shipmatch.example.com/`. The certificate is issued on the first visit once DNS points to the droplet.
2. Create the first admin: `/opt/shipmatch/current/scripts/deploy-droplet.sh manage createsuperuser`
   (or turn on `DEMO_MODE=1` for the shared demo accounts, see the README).
3. Add provider keys to `/opt/shipmatch/shared/.env` (for example `EXTRACTION_PROVIDER=anthropic` and
   `ANTHROPIC_API_KEY`, `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `EMAIL_HOST`...), then apply them with
   `/opt/shipmatch/current/scripts/deploy-droplet.sh install`.
4. **Keep a copy of `FIELD_ENCRYPTION_KEY`** in your password manager. Without it, saved QuickBooks
   connections and two-factor secrets can't be decrypted after a restore.

## QuickBooks

In the Intuit developer portal, add to your app's **Redirect URIs**:

```
https://shipmatch.example.com/accounting/qbo/callback
```

The script sets `QBO_REDIRECT_URI` to the same value. For production keys Intuit also asks for a
**Launch URL** and a **Disconnect/Reconnect URL**: use `https://shipmatch.example.com/` and
`https://shipmatch.example.com/accounting/qbo/reconnect/`. `deploy-droplet.sh status` prints them.

## Public demo

To hand the server to prospects, add to `/opt/shipmatch/shared/.env`:

```
DEMO_MODE=1
TRY_ENABLED=1
DEMO_CONTACT_URL=https://cal.com/your-name   # or mailto:sales@example.com
TRY_DAILY_LIMIT=100                           # bounds AI cost when EXTRACTION_PROVIDER=anthropic
```

Run `install` again. The demo organization is rebuilt every night at `DEMO_RESET_HOUR` (UTC) by
Celery beat; rebuild it now with `deploy-droplet.sh manage reset_demo`. Try page uploads are
deleted after `TRY_RETENTION_HOURS`.

## Updating

Run the same `push` command again (Option A), or clone the new version into a new
`/opt/shipmatch/releases/<time>` folder and run its `install` (Option B). Before switching, the
script backs up the database (`backups/db-<time>-pre-update.sql.gz`); database changes are applied
when the web container starts.

## Rolling back

```bash
/opt/shipmatch/current/scripts/deploy-droplet.sh rollback
```

This switches the code back to the previous release and rebuilds it. If the update changed the
database (new migrations), also restore the backup taken just before it:

```bash
/opt/shipmatch/current/scripts/deploy-droplet.sh restore-db /opt/shipmatch/backups/db-<time>-pre-update.sql.gz
```

## Backups

Every night the script writes `/opt/shipmatch/backups/db-<time>.sql.gz` (a `pg_dump`) and
`media-<time>.tar.gz` (uploaded PDFs) and deletes copies older than 14 days (`BACKUP_KEEP_DAYS`).
Backups on the same droplet don't protect against losing the droplet: copy the folder elsewhere,
for example with DigitalOcean's weekly droplet backups, Spaces, or `rsync` from another machine.
Run one now with `deploy-droplet.sh backup`.

Restore uploaded files:

```bash
cd /opt/shipmatch/current
docker compose -p shipmatch -f docker-compose.yml -f docker-compose.prod.yml exec -T web \
  tar xzf - -C /app/media < /opt/shipmatch/backups/media-<time>.tar.gz
```

## Everyday commands

| Task | Command (on the droplet, from `/opt/shipmatch/current`) |
| --- | --- |
| Status, health and URLs | `scripts/deploy-droplet.sh status` |
| Logs | `scripts/deploy-droplet.sh logs web` (or `worker`, `beat`, `caddy`) |
| Django command | `scripts/deploy-droplet.sh manage <command>`, e.g. `manage accuracy_report --org demo` |
| Apply `.env` changes | `scripts/deploy-droplet.sh install` |

## Going further

- Managed Postgres: set `DATABASE_URL` in `docker-compose.prod.yml`'s `x-prod-env` and stop the `db` service.
- Uploaded files in Spaces (S3-compatible): set `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`.
- Uptime monitoring on `https://<domain>/health/ready/`; JSON logs are already on (`LOG_FORMAT=json`).
- A DigitalOcean Cloud Firewall with the same three ports adds a second layer in front of ufw.
