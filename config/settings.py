"""Django settings for ShipMatch.

All environment-specific values come from environment variables (see .env.example).
Local defaults let the project run with SQLite, local files, an in-memory cache and inline
background tasks, so a developer can run it and its tests without Docker.
"""
import os
from pathlib import Path

import dj_database_url
from celery.schedules import crontab
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    return env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [v.strip() for v in env(name, default).split(",") if v.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
APP_VERSION = env("APP_VERSION", "1.1.0")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "apps.core",
    "apps.accounts",
    "apps.documents",
    "apps.shipments",
    "apps.accounting",
    "apps.intake",
    "apps.rates",
    "apps.disputes",
    "apps.notifications",
    "apps.mailboxes",
    "apps.learning",
    "apps.customs",
    "apps.close",
    "apps.demo",
    "apps.landed",
    "apps.workflow",
    "apps.billing",
    "apps.integrations",
]

MIDDLEWARE = [
    "apps.core.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "apps.core.middleware.LoginNextMiddleware",   # a form posted after the session ended returns to its page, not to the POST-only URL
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.IdleTimeoutMiddleware",
    "apps.demo.middleware.DemoModeMiddleware",   # DEMO_MODE guard rails; must run before the organization is chosen
    "apps.core.middleware.CurrentOrganizationMiddleware",
    "apps.core.middleware.TimezoneMiddleware",
    "apps.core.middleware.MFAEnforcementMiddleware",
    "apps.core.middleware.SecurityHeadersMiddleware",
]
# The review screen embeds the source PDF from our own origin.
X_FRAME_OPTIONS = "SAMEORIGIN"

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.app_context",
                "apps.demo.context_processors.demo",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.parse(
        env("DATABASE_URL") or f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=60,
    )
}

# Shared cache for login lockout, 2FA replay protection and API rate limits.
# Use Redis in production so every web worker sees the same counters.
if env("CACHE_URL"):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": env("CACHE_URL")}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "shipmatch"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
WHITENOISE_AUTOREFRESH = DEBUG      # serve from static/ directly while developing
WHITENOISE_USE_FINDERS = DEBUG
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "accounts:login"
CSRF_FAILURE_VIEW = "apps.core.errors.csrf_failure"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# --- Sessions and sign-in protection ---
SESSION_COOKIE_AGE = int(env("SESSION_MAX_HOURS", "10")) * 3600
SESSION_IDLE_TIMEOUT = int(env("SESSION_IDLE_MINUTES", "30")) * 60   # 0 disables
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
LOGIN_MAX_FAILURES_PER_USER = int(env("LOGIN_MAX_FAILURES_PER_USER", "5"))
LOGIN_MAX_FAILURES_PER_IP = int(env("LOGIN_MAX_FAILURES_PER_IP", "25"))
LOGIN_LOCKOUT_SECONDS = int(env("LOGIN_LOCKOUT_MINUTES", "15")) * 60
PASSWORD_RESET_TIMEOUT = 3 * 24 * 3600   # invite and reset links: 3 days
# Only trust X-Forwarded-For when running behind our own proxy (Caddy in production).
TRUST_X_FORWARDED_FOR = env_bool("TRUST_X_FORWARDED_FOR", False)

# --- Email (invites and password resets). No SMTP host = print emails to the console. ---
EMAIL_HOST = env("EMAIL_HOST")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(env("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = env("EMAIL_HOST_USER")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "ShipMatch <no-reply@localhost>")

# Public address of this ShipMatch, used for links in alerts and emails (no trailing slash).
SITE_URL = env("SITE_URL", "http://localhost:8000").rstrip("/")

# --- File storage: S3/MinIO when a bucket is configured, local disk otherwise ---
STATIC_BACKEND = ("whitenoise.storage.CompressedManifestStaticFilesStorage" if not DEBUG
                  else "django.contrib.staticfiles.storage.StaticFilesStorage")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": STATIC_BACKEND},
}
S3_BUCKET = env("S3_BUCKET")
if S3_BUCKET and env("S3_ENDPOINT_URL"):
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": S3_BUCKET,
            "endpoint_url": env("S3_ENDPOINT_URL"),
            "access_key": env("S3_ACCESS_KEY"),
            "secret_key": env("S3_SECRET_KEY"),
            "default_acl": "private",
            "querystring_auth": True,
            "file_overwrite": False,
        },
    }

# --- Celery ---
CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", True)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
MAILBOX_POLL_MINUTES = max(1, int(env("MAILBOX_POLL_MINUTES", "5")))
CELERY_BEAT_SCHEDULE = {
    # Every connected mailbox (Microsoft 365, IMAP, Gmail); one failing mailbox never stops the others.
    "poll-mailboxes": {
        "task": "apps.mailboxes.tasks.poll_all_mailboxes",
        "schedule": MAILBOX_POLL_MINUTES * 60.0,
    },
    # Disputes past their follow-up date (checked hourly so each organization's own date is respected).
    "disputes-flag-overdue-hourly": {
        "task": "apps.disputes.tasks.flag_overdue_disputes",
        "schedule": crontab(minute=7),
    },
    # Daily summary: runs every hour and sends to each organization at its chosen local hour.
    "alerts-daily-digest-hourly": {
        "task": "apps.notifications.tasks.send_daily_digests",
        "schedule": crontab(minute=2),
    },
    # Alerts whose scheduled retry was lost (worker restart).
    "alerts-retry-stalled": {
        "task": "apps.notifications.tasks.retry_stalled_deliveries",
        "schedule": 600.0,
    },
    # Last free day alerts: every hour, each organization in its own time zone from CUSTOMS_ALERT_HOUR.
    "customs-free-time-hourly": {
        "task": "apps.customs.tasks.check_free_time",
        "schedule": crontab(minute=12),
    },
}

# --- Public try page (apps.demo): visitors upload one PDF without signing in ---
TRY_ENABLED = env_bool("TRY_ENABLED", False)
TRY_MAX_MB = float(env("TRY_MAX_MB", "10"))
TRY_MAX_PAGES = int(env("TRY_MAX_PAGES", "10"))
TRY_RATE_PER_HOUR = int(env("TRY_RATE_PER_HOUR", "5"))      # uploads per visitor IP
TRY_RATE_PER_DAY = int(env("TRY_RATE_PER_DAY", "20"))
TRY_DAILY_LIMIT = int(env("TRY_DAILY_LIMIT", "200"))        # all visitors together; bounds AI cost
TRY_RETENTION_HOURS = int(env("TRY_RETENTION_HOURS", "24"))
DEMO_CONTACT_URL = env("DEMO_CONTACT_URL")                  # "Talk to us" button, e.g. https://cal.com/you or mailto:
# --- Demo mode: banner, demo accounts on the sign-in page, guard rails, nightly reset ---
DEMO_MODE = env_bool("DEMO_MODE", False)
DEMO_ORGS = env_list("DEMO_ORGS", "demo")                   # organizations reset_demo wipes and rebuilds
DEMO_RESET_HOUR = int(env("DEMO_RESET_HOUR", "3"))          # UTC hour of the nightly reset
# A public demo never emails anyone or posts to Slack/Teams: anyone can sign in with the shared accounts
# and write a dispute email or alert. Set DEMO_SEND_OUTSIDE=1 only on a private demo.
DEMO_SEND_OUTSIDE = env_bool("DEMO_SEND_OUTSIDE", False)
if DEMO_MODE and not DEMO_SEND_OUTSIDE:
    EMAIL_BACKEND = "apps.demo.mail.DemoEmailBackend"
CELERY_BEAT_SCHEDULE["purge-try-uploads-hourly"] = {"task": "apps.demo.tasks.purge_try_uploads", "schedule": 3600.0}
# Outgoing webhook retries that are due (and any whose scheduled retry was lost); unverified sign-ups.
CELERY_BEAT_SCHEDULE["webhooks-retry-due"] = {"task": "apps.integrations.tasks.retry_due_deliveries", "schedule": 60.0}
CELERY_BEAT_SCHEDULE["signups-purge-hourly"] = {"task": "apps.billing.tasks.purge_pending_signups",
                                                "schedule": crontab(minute=23)}
if DEMO_MODE:
    from celery.schedules import crontab

    CELERY_BEAT_SCHEDULE["reset-demo-nightly"] = {"task": "apps.demo.tasks.reset_demo",
                                                  "schedule": crontab(hour=DEMO_RESET_HOUR, minute=7)}

# --- OCR / extraction ---
OCR_PROVIDER = env("OCR_PROVIDER", "auto")  # auto | text | anthropic | textract
AWS_REGION = env("AWS_REGION", "us-east-1")
EXTRACTION_PROVIDER = env("EXTRACTION_PROVIDER", "rules")  # rules | anthropic | openai
LLM_MODEL = env("LLM_MODEL")              # default claude-sonnet-5-5 (anthropic) / gpt-4o-mini (openai)
# What the LLM reads: text = PDF text layer only; pdf = the PDF itself too (layout, tables, stamps);
# auto = the PDF only for scanned documents.
LLM_INPUT = env("LLM_INPUT", "auto")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
OPENAI_API_KEY = env("OPENAI_API_KEY")
# A field below this confidence is sent to human review (each organization can override it).
REVIEW_CONFIDENCE_THRESHOLD = float(env("REVIEW_CONFIDENCE_THRESHOLD", "0.85"))

# --- Intake: which files are accepted and the safety limits for each kind ---
# PDFs, images (JPG, PNG, TIFF, WebP), spreadsheets (XLSX, CSV) and ZIP archives of those.
INTAKE_MAX_FILE_MB = int(env("INTAKE_MAX_FILE_MB", "25"))            # one PDF, image or spreadsheet
INTAKE_MAX_ARCHIVE_MB = int(env("INTAKE_MAX_ARCHIVE_MB", "100"))     # one ZIP as received
INTAKE_ZIP_MAX_FILES = int(env("INTAKE_ZIP_MAX_FILES", "200"))       # files inside one ZIP (nested ones included)
INTAKE_ZIP_MAX_TOTAL_MB = int(env("INTAKE_ZIP_MAX_TOTAL_MB", "300"))  # everything inside one ZIP, unpacked
INTAKE_ZIP_MAX_RATIO = int(env("INTAKE_ZIP_MAX_RATIO", "100"))       # unpacked size / packed size of one file
INTAKE_ZIP_MAX_DEPTH = int(env("INTAKE_ZIP_MAX_DEPTH", "1"))         # a ZIP inside a ZIP is opened, no deeper
INTAKE_MAX_IMAGE_MEGAPIXELS = int(env("INTAKE_MAX_IMAGE_MEGAPIXELS", "120"))
INTAKE_MAX_IMAGE_PAGES = int(env("INTAKE_MAX_IMAGE_PAGES", "50"))    # frames of a multi-page TIFF
INTAKE_MAX_IMAGE_TOTAL_MEGAPIXELS = int(env("INTAKE_MAX_IMAGE_TOTAL_MEGAPIXELS", "500"))  # all frames together
INTAKE_SHEET_MAX_ROWS = int(env("INTAKE_SHEET_MAX_ROWS", "5000"))    # rows read per sheet
INTAKE_SHEET_MAX_COLUMNS = int(env("INTAKE_SHEET_MAX_COLUMNS", "60"))
# Split PDFs that hold several invoices (a carrier batch) into one document per invoice.
INTAKE_SPLIT_PDFS = env_bool("INTAKE_SPLIT_PDFS", True)
# With AI reading on, one short AI call checks uncertain page boundaries before splitting.
INTAKE_SPLIT_AI_CONFIRM = env_bool("INTAKE_SPLIT_AI_CONFIRM", True)
# --- Vendor learning (apps.learning): remember how each vendor prints values that reviewers corrected ---
VENDOR_LEARNING = env_bool("VENDOR_LEARNING", True)
LEARNING_HINT_MAX_CHARS = int(env("LEARNING_HINT_MAX_CHARS", "1200"))   # hard cap on vendor notes sent to the AI
LEARNING_MATCH_SCORE = float(env("LEARNING_MATCH_SCORE", "90"))         # 0-100: how close a vendor name must be

# --- Gmail ---
GMAIL_CREDENTIALS_FILE = env("GMAIL_CREDENTIALS_FILE", "secrets/gmail_credentials.json")
GMAIL_TOKEN_FILE = env("GMAIL_TOKEN_FILE", "secrets/gmail_token.json")
GMAIL_LABEL = env("GMAIL_LABEL", "AP-Inbox")

# --- Email intake (apps.mailboxes) ---
# Forwarding addresses look like <org-slug>-<token>@INBOUND_EMAIL_DOMAIN. The domain's MX records point at
# Postmark or Mailgun, which post each email to /inbound/email/<provider>/. Empty = forwarding addresses off.
INBOUND_EMAIL_DOMAIN = env("INBOUND_EMAIL_DOMAIN").strip().lower().lstrip("@")
POSTMARK_INBOUND_USER = env("POSTMARK_INBOUND_USER")
POSTMARK_INBOUND_PASSWORD = env("POSTMARK_INBOUND_PASSWORD")
MAILGUN_SIGNING_KEY = env("MAILGUN_SIGNING_KEY")
INBOUND_EMAIL_MAX_MB = int(env("INBOUND_EMAIL_MAX_MB", "40"))
INBOUND_EMAIL_RATE_PER_MINUTE = int(env("INBOUND_EMAIL_RATE_PER_MINUTE", "60"))   # per organization
# Microsoft 365 / Outlook.com mailboxes (Azure app registration, see README "Email intake")
MS_CLIENT_ID = env("MS_CLIENT_ID")
MS_CLIENT_SECRET = env("MS_CLIENT_SECRET")
MS_TENANT = env("MS_TENANT", "common") or "common"
MS_REDIRECT_URI = env("MS_REDIRECT_URI") or f"{SITE_URL}/settings/email/microsoft/callback"
# IMAP hosts on private networks (10.x, 192.168.x, localhost) are refused unless this is on.
MAILBOX_ALLOW_PRIVATE_HOSTS = env_bool("MAILBOX_ALLOW_PRIVATE_HOSTS", False)

# --- QuickBooks Online ---
QBO_CLIENT_ID = env("QBO_CLIENT_ID")
QBO_CLIENT_SECRET = env("QBO_CLIENT_SECRET")
QBO_ENVIRONMENT = env("QBO_ENVIRONMENT", "sandbox")  # sandbox | production
QBO_REDIRECT_URI = env("QBO_REDIRECT_URI") or f"{SITE_URL}/accounting/qbo/callback"
QBO_MINOR_VERSION = env("QBO_MINOR_VERSION", "75")

# --- Xero (apps.accounting; README "Xero"). An organization posts to QuickBooks or Xero, never both. ---
XERO_CLIENT_ID = env("XERO_CLIENT_ID")
XERO_CLIENT_SECRET = env("XERO_CLIENT_SECRET")   # empty = a PKCE app in the Xero portal (no secret, PKCE sign-in)
XERO_REDIRECT_URI = env("XERO_REDIRECT_URI") or f"{SITE_URL}/accounting/xero/callback"
# Xero apps created from 2 March 2026 must ask for granular scopes instead of accounting.transactions:
# offline_access accounting.invoices accounting.payments.read accounting.contacts accounting.settings accounting.attachments
XERO_SCOPES = env("XERO_SCOPES", "offline_access accounting.transactions accounting.contacts accounting.settings "
                                 "accounting.attachments")

# --- Payment status of posted bills (QuickBooks and Xero; README "Payment status") ---
PAYMENT_SYNC_ENABLED = env_bool("PAYMENT_SYNC_ENABLED", True)
PAYMENT_SYNC_HOURS = max(1, int(env("PAYMENT_SYNC_HOURS", "1")))   # each organization at most this often (>= 1 h)
CELERY_BEAT_SCHEDULE["accounting-payment-status"] = {
    "task": "apps.accounting.tasks.sync_all_payments", "schedule": crontab(minute=23)}

# --- Rates and savings (apps/rates) ---
# Defaults for organizations that haven't set their own tolerance: an overcharge is flagged when it
# is more than this percentage of the quoted amount and more than this fixed amount (home currency).
RATE_TOLERANCE_PERCENT = float(env("RATE_TOLERANCE_PERCENT", "2"))
RATE_TOLERANCE_AMOUNT = float(env("RATE_TOLERANCE_AMOUNT", "10"))
# Ask the AI to name invoice charges the keyword table doesn't know (only when an LLM provider is set).
RATE_AI_CLASSIFY = env_bool("RATE_AI_CLASSIFY", True)

# --- Landed cost and shared invoices (apps/landed) ---
# How charges are spread over a shipment's products for organizations that haven't chosen:
# value | quantity | weight | volume
LANDED_DEFAULT_METHOD = env("LANDED_DEFAULT_METHOD", "value").strip().lower() or "value"
# Look for freight invoices that cover several shipments and suggest a split (reviewers confirm it).
SHARED_INVOICE_DETECT = env_bool("SHARED_INVOICE_DETECT", True)
# --- Customs entries and free time (apps/customs) ---
# Alert this many days before a container's last free day (default for organizations; Settings > Customs).
CUSTOMS_LFD_ALERT_DAYS = int(env("CUSTOMS_LFD_ALERT_DAYS", "2"))
# Local hour (organization time zone) from which the day's last free day alerts are sent.
CUSTOMS_ALERT_HOUR = int(env("CUSTOMS_ALERT_HOUR", "7"))
# Duty, fee and total differences up to this amount (entry currency) are rounding, not errors.
CUSTOMS_DUTY_TOLERANCE = env("CUSTOMS_DUTY_TOLERANCE", "1.00")
# Entered value may differ from the commercial invoices by this percentage before it is flagged.
CUSTOMS_VALUE_TOLERANCE_PERCENT = env("CUSTOMS_VALUE_TOLERANCE_PERCENT", "1.0")
# MPF minimum/maximum rows added to or replacing the dated table in apps/customs/fees.py, as JSON, e.g.
# [{"from": "2027-10-01", "min": "35.50", "max": "690.00"}]. Checked by `manage.py check`.
CUSTOMS_MPF_TABLE = env("CUSTOMS_MPF_TABLE", "")
# Harbor Maintenance Fee in percent; empty = 0.125 (26 U.S.C. 4461).
CUSTOMS_HMF_RATE = env("CUSTOMS_HMF_RATE", "")
# With AI reading on, ask once per tariff number and description whether they fit (warning only).
CUSTOMS_AI_HTS_CHECK = env_bool("CUSTOMS_AI_HTS_CHECK", True)
# --- Month-end close (apps/close): accruals and vendor statement reconciliation ---
# Defaults for organizations that haven't saved their own month-end settings.
CLOSE_ACCRUED_LIABILITIES_ACCOUNT = env("CLOSE_ACCRUED_LIABILITIES_ACCOUNT", "Accrued liabilities")
CLOSE_FREIGHT_ACCOUNT = env("CLOSE_FREIGHT_ACCOUNT", "Freight expense")    # vendor without an account set
CLOSE_GOODS_ACCOUNT = env("CLOSE_GOODS_ACCOUNT", "Inventory")              # supplier (goods) invoices
CLOSE_LOOKBACK_DAYS = int(env("CLOSE_LOOKBACK_DAYS", "180"))     # older shipments are listed, not accrued
CLOSE_MIN_HISTORY = int(env("CLOSE_MIN_HISTORY", "3"))           # past invoices before a median is trusted
CLOSE_STATEMENT_MAX_LINES = int(env("CLOSE_STATEMENT_MAX_LINES", "2000"))   # lines read from one statement
CLOSE_PAYMENT_SYNC_DAYS = int(env("CLOSE_PAYMENT_SYNC_DAYS", "180"))        # QuickBooks bill payments read back
# --- Team workflow (apps.workflow): bulk actions, assignment, comments, approval links, firm view ---
# How long the link in "ready for approval" alerts names its shipment. The link never signs anyone in.
APPROVAL_LINK_MAX_AGE_HOURS = int(env("APPROVAL_LINK_MAX_AGE_HOURS", "72"))
# Minutes after posting during which an author may edit or delete their own comment.
COMMENT_EDIT_MINUTES = int(env("COMMENT_EDIT_MINUTES", "15"))
# Most shipments one bulk action (approve, assign, post) may cover.
BULK_MAX_SHIPMENTS = int(env("BULK_MAX_SHIPMENTS", "100"))
# Let admins of an organization create new client organizations from the All clients page (firms).
FIRM_CAN_CREATE_ORGS = env_bool("FIRM_CAN_CREATE_ORGS", False)

# --- API ---
API_RATE_LIMIT_PER_MINUTE = int(env("API_RATE_LIMIT_PER_MINUTE", "120"))

# --- Self-serve signup (apps.billing): /signup/ creates an organization after the email is verified ---
SIGNUP_ENABLED = env_bool("SIGNUP_ENABLED", False)
SIGNUP_VERIFY_HOURS = int(env("SIGNUP_VERIFY_HOURS", "48"))          # how long the emailed link works
SIGNUP_RATE_PER_HOUR = int(env("SIGNUP_RATE_PER_HOUR", "5"))         # sign-up attempts per visitor IP
SIGNUP_RATE_PER_DAY = int(env("SIGNUP_RATE_PER_DAY", "20"))
SIGNUP_BLOCK_DISPOSABLE = env_bool("SIGNUP_BLOCK_DISPOSABLE", True)  # refuse throwaway email domains
SIGNUP_BLOCKED_DOMAINS = [d.lower().lstrip("@") for d in env_list("SIGNUP_BLOCKED_DOMAINS")]

# --- Billing with Stripe (apps.billing). Off: no plans, no limits, no Stripe calls. ---
BILLING_ENABLED = env_bool("BILLING_ENABLED", False)
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY")
# Webhook signing secrets (whsec_...); several, comma separated, while rotating them in Stripe.
STRIPE_WEBHOOK_SECRETS = env_list("STRIPE_WEBHOOK_SECRET")
STRIPE_WEBHOOK_TOLERANCE = int(env("STRIPE_WEBHOOK_TOLERANCE", "300"))   # seconds a signed event stays valid
STRIPE_API_BASE = env("STRIPE_API_BASE", "https://api.stripe.com").rstrip("/")
STRIPE_API_VERSION = env("STRIPE_API_VERSION", "2024-06-20")
BILLING_CURRENCY = env("BILLING_CURRENCY", "USD")
BILLING_TRIAL_DAYS = int(env("BILLING_TRIAL_DAYS", "14"))
BILLING_TRIAL_PLAN = env("BILLING_TRIAL_PLAN", "growth")                 # users and features during the trial
BILLING_TRIAL_DOCUMENTS = int(env("BILLING_TRIAL_DOCUMENTS", "100"))     # documents a trial may receive
BILLING_HARD_LIMIT_PERCENT = int(env("BILLING_HARD_LIMIT_PERCENT", "120"))  # new documents stop here
# Plans: monthly Stripe price ids from the environment; documents = monthly allowance, users 0 = no limit.
BILLING_PLANS = {
    "starter": {"name": "Starter", "price_id": env("STRIPE_PRICE_STARTER"), "price": env("BILLING_STARTER_PRICE", "199"),
                "documents": int(env("BILLING_STARTER_DOCUMENTS", "300")), "users": 3,
                "features": ["email_intake", "quickbooks", "checks", "exports"]},
    "growth": {"name": "Growth", "price_id": env("STRIPE_PRICE_GROWTH"), "price": env("BILLING_GROWTH_PRICE", "499"),
               "documents": int(env("BILLING_GROWTH_DOCUMENTS", "1000")), "users": 10,
               "features": ["email_intake", "quickbooks", "checks", "exports", "disputes", "api", "webhooks"]},
    "scale": {"name": "Scale", "price_id": env("STRIPE_PRICE_SCALE"), "price": env("BILLING_SCALE_PRICE", "1290"),
              "documents": int(env("BILLING_SCALE_DOCUMENTS", "4000")), "users": 0,
              "features": ["email_intake", "quickbooks", "checks", "exports", "disputes", "api", "webhooks",
                           "priority_support"]},
}

# --- Outgoing webhooks and exports (apps.integrations) ---
WEBHOOK_MAX_ATTEMPTS = int(env("WEBHOOK_MAX_ATTEMPTS", "8"))                       # per delivery
WEBHOOK_DISABLE_AFTER_FAILURES = int(env("WEBHOOK_DISABLE_AFTER_FAILURES", "20"))  # consecutive failed attempts
WEBHOOK_TIMEOUT_SECONDS = float(env("WEBHOOK_TIMEOUT_SECONDS", "10"))
WEBHOOK_SECRET_OVERLAP_HOURS = int(env("WEBHOOK_SECRET_OVERLAP_HOURS", "24"))     # old secret still signs after rotation
EXPORT_XLSX_MAX_ROWS = int(env("EXPORT_XLSX_MAX_ROWS", "200000"))

# --- Logging: every line carries the request ID; LOG_FORMAT=json for log platforms ---
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_id": {"()": "apps.core.context.RequestIdFilter"}},
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s [%(request_id)s] %(name)s %(message)s"},
        "json": {"()": "apps.core.logging.JsonFormatter"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "filters": ["request_id"],
                             "formatter": "json" if env("LOG_FORMAT") == "json" else "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        "django": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO"), "propagate": False},
        "pdfminer": {"level": "WARNING"},
    },
}

# --- Production security (DJANGO_DEBUG=0) ---
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SECURE_REDIRECT_EXEMPT = [r"^health/"]
    SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
# HSTS preload is a long-term commitment for the whole domain; opt in deliberately.
SILENCED_SYSTEM_CHECKS = ["security.W021", "security.W019"]  # W019: SAMEORIGIN is required for the in-app PDF viewer
