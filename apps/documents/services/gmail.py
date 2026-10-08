"""Gmail ingestion: import attachments from emails carrying a label (default 'AP-Inbox').

PDFs, photos and scans (JPG, PNG, TIFF, WebP), spreadsheets (XLSX, CSV) and ZIP archives are imported;
images placed inside the email body (signature logos) are not.

Setup once per organization:
    python manage.py gmail_auth --org demo     # opens a browser for Google consent, stores a token
Then polling runs every 5 minutes via Celery beat, or manually:
    python manage.py poll_gmail --org demo
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

from apps.core.models import Organization
from apps.documents.models import Document, IngestedEmail

from .ingest import RejectedFile, ingest_bytes

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def token_path(org: Organization) -> Path:
    raw = settings.GMAIL_TOKEN_FILE
    return Path(raw.format(org=org.slug) if "{org}" in raw else raw.replace(".json", f"_{org.slug}.json"))


def authorize_interactive(org: Organization) -> Path:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(settings.GMAIL_CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    path = token_path(org)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    return path


def get_service(org: Organization):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = token_path(org)
    if not path.exists():
        raise FileNotFoundError(f"No Gmail token for {org.slug}. Run: python manage.py gmail_auth --org {org.slug}")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _parts(payload: dict):
    yield payload
    for p in payload.get("parts", []) or []:
        yield from _parts(p)


def _is_inline_image(part: dict) -> bool:
    """An image shown inside the email body (a logo in a signature), not an attached document."""
    headers = {h["name"].lower(): h["value"].lower() for h in part.get("headers", []) or []}
    return (part.get("mimeType", "").startswith("image/") and "content-id" in headers
            and headers.get("content-disposition", "inline").startswith("inline"))


def search_query() -> str:
    from apps.intake.services.formats import SUPPORTED_EXTENSIONS

    names = " ".join(f"filename:{ext.lstrip('.')}" for ext in sorted(SUPPORTED_EXTENSIONS))
    return f"label:{settings.GMAIL_LABEL} has:attachment {{{names}}} newer_than:30d"


def poll(org: Organization, service=None, max_messages: int = 50, process: str = "async") -> dict:
    from apps.intake.services.formats import is_supported_name

    service = service or get_service(org)
    query = search_query()
    resp = service.users().messages().list(userId="me", q=query, maxResults=max_messages).execute()
    stats = {"emails": 0, "documents": 0, "skipped_emails": 0, "rejected": 0, "unsupported": 0}
    for ref in resp.get("messages", []):
        if IngestedEmail.objects.filter(organization=org, message_id=ref["id"]).exists():
            stats["skipped_emails"] += 1
            continue
        msg = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        email = IngestedEmail.objects.create(
            organization=org, message_id=ref["id"], subject=headers.get("subject", "")[:500],
            sender=headers.get("from", "")[:320],
            received_at=datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc),
        )
        stats["emails"] += 1
        for part in _parts(msg["payload"]):
            name = part.get("filename") or ""
            if not name or _is_inline_image(part):
                continue
            if not is_supported_name(name):
                stats["unsupported"] += 1
                log.info("Skipped attachment %s: not a supported file type", name)
                continue
            body = part.get("body", {})
            if body.get("attachmentId"):
                body = service.users().messages().attachments().get(
                    userId="me", messageId=ref["id"], id=body["attachmentId"]).execute()
            content = base64.urlsafe_b64decode(body.get("data", ""))
            try:
                _, created = ingest_bytes(org, name, content, source=Document.Source.EMAIL, email=email, process=process)
                stats["documents"] += int(created)
                email.attachment_count += 1
            except RejectedFile as e:
                stats["rejected"] += 1
                log.warning("Rejected attachment: %s", e)
        email.save(update_fields=["attachment_count"])
    return stats
