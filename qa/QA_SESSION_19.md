# ShipMatch QA — Session 19: production deployment rehearsal (16–17 Oct 2026)

Same rules: **issues listed, nothing fixed**. The production image was built from this working copy (`docker build`, 29 min, 734 MB, Python 3.12.15) and run with the project's own `docker-compose.yml` + `docker-compose.prod.yml` (Compose v5.5.1) under a separate project name (`qa19`), from a scratch folder holding copies of the two compose files and `deploy/Caddyfile` and a QA-only `.env` (fresh random `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, `POSTGRES_PASSWORD`; `DOMAIN=localhost`; rules extraction, no AI or OCR keys), so the real `.env` and its provider keys were never used. Caddy served `https://localhost` with its internal certificate. Branch under test: `merge/qa-sessions-08-13` at `b94b52f`. Issue ids continue from session 21 (last was QA-100).

## 1. Coverage this session

| Area | Status | Result |
|---|---|---|
| **Stack start** | ✅ | `up -d`: Postgres and Redis healthy, then web ran `migrate` and `collectstatic` (163 files, 475 post-processed), gunicorn with 3 sync workers; `/health/ready/` healthy after 35 s. The worker and beat start at the same moment as web, before migrations finish (worker ready at 17:54:06, migrations done about 17:54:20): no errors this time, but see notes. |
| **Ports** | ✅ | Only Caddy publishes 80/443; Postgres and Redis are private (`ports: !reset []` works on this Compose). |
| **HTTPS and redirects** | ✅ | `http://` → 308 to `https://` for every path, including `/health/`. |
| **Security headers through the proxy** | ✅ | HSTS (1 year, subdomains), CSP, `X-Frame-Options`, `nosniff`, Referrer-Policy, COOP, Permissions-Policy, `Cache-Control: no-store` on signed-in pages (QA-082 fix), CSRF cookie `Secure`. Notes below on duplicates. |
| **Static files** | ✅ | Hashed names, `max-age=315360000, public, immutable`, gzip. |
| **`DEBUG` off** | ✅ | Branded 404; `check --deploy`: no issues with the proper `.env`. |
| **Client IP behind Caddy** | ✅ | Caddy replaces a client's `X-Forwarded-For`, so a spoofed header changes nothing: 37 failed sign-ins with 37 different spoofed addresses were all counted against one IP and locked after 25; the per-user lockout (5) held too. |
| **Upload through the proxy** | 🟡 | Real PDF: read by the prefork worker in 0.43 s. 28 MB PDF: refused by the app (25 MB limit). 40 MB ZIP: QA-103. |
| **Missing secrets** | ❌ | QA-101. |
| **Backup and restore drill** | ✅ | With the 3,000-shipment data set loaded: the `deploy-droplet.sh backup` commands (`pg_dump --clean --if-exists` + `tar` of `/app/media`), then `docker compose down -v` (database and media volumes destroyed), an empty stack, then the `restore-db` steps (stop web/worker/beat, `psql -v ON_ERROR_STOP=1`, start) and the media tar. 45 s. Every count identical (3,001 shipments, 6,001 documents, 30,008 fields, 74 audit rows), sign-in works, the stored PDF downloads byte for byte. `deploy/backup.sh`: QA-104. |
| **Upgrade with migrations over existing data** | ⬜ | Not possible from history: no migration has been added since the initial commit (`be64c64`). `makemigrations --check`: no changes; `migrate` on the restored data: nothing to apply. |
| **Image contents** | ❌ | QA-102. Also confirms QA-100: 20 of the image's 83 packages differ from the venv the tests ran on (Django 5.2.17 → 5.2.18, pydantic 2.13.5 → 2.14.1, pypdf 6.19.0 → 6.21.0, …); `pip-audit` over the image's own versions: no known vulnerabilities. |

## 2. New issues

**QA-101 — High — Without `DJANGO_SECRET_KEY`, production starts with a key that is printed in the source code**
`config/settings.py:30` falls back to `"dev-insecure-key-change-me"`. With the key left out of `.env` and `DEBUG` off, the web container starts, passes its health check and serves pages; the only sign is `security.W009` from `check --deploy`, which nothing runs (the web command is `migrate && collectstatic && gunicorn`). Anyone who reads the repository can then forge session cookies (sign in as anyone), password-reset links and signed values. `FIELD_ENCRYPTION_KEY` is handled better (`apps/core/crypto.py` refuses with `DEBUG` off) but only when something is first encrypted (connecting QuickBooks, setting up 2FA), so a missing key surfaces as a 500 on that screen, not at deploy time. Refuse to start when `DEBUG` is off and either key is missing or the default (an `ImproperlyConfigured` in settings, or a system check with `Error` level).

**QA-102 — Medium — `local-test-accounts.txt` (with a password) is copied into the production image**
`.dockerignore` excludes `.env`, `db.sqlite3`, `media/` and others, but not `local-test-accounts.txt` (the Northwind test owner's login and password). An image built from a working copy that has the file contains it at `/app/local-test-accounts.txt`; anyone with access to the image (a registry, a backup, a shared server) can read it. The droplet script builds from a clean `rsync` (it may not include the file), but `docker compose up --build` from a developer machine does. Also copied: `qa/` and `tests/` (harmless, but not needed in production). Add the file (and `*.txt` notes, `qa/`, `tests/`) to `.dockerignore`, and keep local credentials outside the repository folder.

**QA-103 — Low/Medium — Uploads between 30 MB and the app's limits get Caddy's bare "413" instead of the app's message**
`deploy/Caddyfile` caps request bodies at 30 MB, while the app accepts ZIP archives up to 100 MB (`INTAKE_MAX_ARCHIVE_MB`) and inbound email up to 40 MB (`INBOUND_EMAIL_MAX_MB`). A 40 MB ZIP through Caddy: HTTP 413 with an empty body in 0.3 s; the browser shows its own error page and the user never sees "too large, the limit is …". Inbound email providers posting 30–40 MB messages are refused the same way. Align the two (raise Caddy's limit to the largest app limit plus overhead, or lower the app limits), and document it.

**QA-104 — Low — `deploy/backup.sh` does not back up the PDFs in the default setup**
It copies files only from MinIO (`docker compose run … minio-init … mc mirror local/shipmatch`), but the default production setup keeps uploads in the `media` volume (no `S3_BUCKET`); it also calls `docker compose` without the production files and project name. The droplet script's own `backup` (database dump + tar of `/app/media`) is the one that works (drill above). Remove `deploy/backup.sh` or make it call the same steps, so nobody puts the wrong one in cron.

## 3. Notes (not issues)

- Headers sent twice (once by Django, once by Caddy): `Strict-Transport-Security`, `Referrer-Policy`, `X-Content-Type-Options`. Harmless; keep them in one place.
- `Server: gunicorn` is exposed; Caddy advertises HTTP/3 (`Alt-Svc: h3`) but the compose file publishes only TCP 443, so browsers fall back to HTTP/2.
- The worker and beat don't wait for migrations: on an upgrade they run new code against the old schema until web has migrated. Running `migrate` as a one-off step before starting the services (the droplet script could do this) closes the window.
- `POSTGRES_PASSWORD` falls back to `shipmatch` when unset (`${POSTGRES_PASSWORD:-shipmatch}`). The database port is not published, so the risk is low, but a deploy should refuse a missing password like QA-101.
- On Docker Desktop every client appears as the Docker gateway (`172.20.0.1`), so all users share one IP for the per-IP sign-in limit. On a Linux server the real client address reaches Caddy.

## 4. Still untested

A real domain and a public certificate (ACME), the droplet script end to end (`deploy-droplet.sh install/update/rollback`), S3/MinIO storage, an upgrade that adds migrations, log shipping.

## 5. Test data left behind

The `qa19` compose project (6 containers, volumes `qa19_pgdata`, `qa19_media`, `qa19_caddydata`) with the demo data, 3,000 synthetic shipments and 25 `loadNN` users; image `shipmatch-qa:19` (also tagged `qa19-web`, `qa19-worker`, `qa19-beat`), plus the pulled `redis:7` and `caddy:2`. Remove with `docker compose -p qa19 -f docker-compose.yml -f docker-compose.prod.yml down -v` from the scratch folder (or `docker rm -f` the `qa19-*` containers and `docker volume rm qa19_pgdata qa19_media qa19_caddydata`), then `docker rmi shipmatch-qa:19 qa19-web qa19-worker qa19-beat`.
