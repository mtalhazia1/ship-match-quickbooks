"""Gmail label, read through the existing Gmail API connection (apps.documents.services.gmail).

The OAuth token stays where `manage.py gmail_auth --org <slug>` puts it (GMAIL_TOKEN_FILE). Here the
Gmail connection becomes a mailbox like the others: it shows on the Email intake page, is checked by the
same scheduled task and goes through the same intake path (Message-ID de-duplication, every attachment
offered to ingest, outcomes recorded). Gmail access is read only, so emails aren't marked afterwards.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime
from datetime import timezone as dt_timezone

from django.conf import settings

from apps.documents.models import IngestedEmail
from apps.documents.services import gmail as gmail_api
from apps.mailboxes.models import Mailbox

from . import intake
from .errors import MailboxAuthError, MailboxError
from .intake import IncomingAttachment, IncomingEmail

log = logging.getLogger(__name__)
MAX_MESSAGES_PER_CHECK = 50
MAX_LISTED = 500


def token_exists(org) -> bool:
    try:
        return gmail_api.token_path(org).exists()
    except OSError:
        return False


def sync(org) -> Mailbox | None:
    """Show the Gmail connection on the Email intake page when a token file exists for this organization."""
    mailbox = Mailbox.objects.filter(organization=org, kind=Mailbox.Kind.GMAIL).first()
    has_token = token_exists(org)
    if mailbox is None and has_token:
        mailbox = Mailbox.objects.create(organization=org, kind=Mailbox.Kind.GMAIL, display_name="Gmail",
                                         folder=settings.GMAIL_LABEL, folder_name=settings.GMAIL_LABEL)
    elif mailbox is not None and not has_token and not mailbox.needs_reconnect:
        Mailbox.objects.filter(pk=mailbox.pk).update(
            needs_reconnect=True,
            last_error=f"The Gmail sign-in for this organization is missing on the server. Run: python manage.py "
                       f"gmail_auth --org {org.slug}")
        mailbox.refresh_from_db()
    elif mailbox is not None and has_token and mailbox.needs_reconnect and "gmail_auth" in mailbox.last_error:
        Mailbox.objects.filter(pk=mailbox.pk).update(needs_reconnect=False, last_error="")
        mailbox.refresh_from_db()
    return mailbox


def _service(org):
    try:
        return gmail_api.get_service(org)
    except FileNotFoundError:
        raise MailboxAuthError(f"Gmail isn't signed in for this organization. Run: python manage.py gmail_auth "
                               f"--org {org.slug}")
    except Exception as e:
        if type(e).__name__ == "RefreshError":
            raise MailboxAuthError(f"Google no longer accepts the saved Gmail sign-in. Run: python manage.py "
                                   f"gmail_auth --org {org.slug}")
        raise


def _http_status(e: Exception) -> int | None:
    return getattr(getattr(e, "resp", None), "status", None)


def _execute(request):
    try:
        return request.execute()
    except Exception as e:
        status = _http_status(e)
        if status in (401, 403):
            raise MailboxAuthError("Gmail refused access with the saved sign-in. Run gmail_auth again.")
        if status:
            raise MailboxError(f"Gmail returned an error ({status}). ShipMatch will try again at the next check.")
        raise


def _parts(payload: dict):
    yield payload
    for p in payload.get("parts") or []:
        yield from _parts(p)


def _attachments(service, gmail_id: str, payload: dict) -> list[IncomingAttachment]:
    out = []
    for part in _parts(payload):
        name = part.get("filename") or ""
        body = part.get("body") or {}
        if not name and not body.get("attachmentId"):
            continue   # body text, not an attachment
        headers = {h["name"].lower(): h["value"] for h in part.get("headers") or []}
        disposition = headers.get("content-disposition", "").lower()
        inline = disposition.startswith("inline") or (not disposition and "content-id" in headers)
        data = body.get("data")
        if not data and body.get("attachmentId"):
            data = _execute(service.users().messages().attachments().get(
                userId="me", messageId=gmail_id, id=body["attachmentId"])).get("data", "")
        try:
            content = base64.urlsafe_b64decode((data or "") + "=" * (-len(data or "") % 4))
        except ValueError:
            content = None
        out.append(IncomingAttachment(filename=name, content_type=part.get("mimeType") or "application/octet-stream",
                                      content=content, inline=inline))
    return out


def test_connection(mailbox: Mailbox, service=None) -> str:
    service = service or _service(mailbox.organization)
    labels = _execute(service.users().labels().list(userId="me")).get("labels") or []
    label = mailbox.folder or settings.GMAIL_LABEL
    if not any((lb.get("name") or "").lower() == label.lower() for lb in labels):
        raise MailboxError(f"Signed in to Gmail, but there's no label named “{label}”. Create it in Gmail and add a "
                           "filter that applies it to supplier emails.")
    profile = _execute(service.users().getProfile(userId="me"))
    return f"Connected to {profile.get('emailAddress', 'Gmail')}. ShipMatch reads the label “{label}”."


def poll(mailbox: Mailbox, process: str = "async", service=None) -> dict:
    org = mailbox.organization
    service = service or _service(org)
    stats = {"emails": 0, "documents": 0, "already": 0, "skipped_attachments": 0, "warnings": []}
    label = (mailbox.folder or settings.GMAIL_LABEL).replace(" ", "-")
    query = f"label:{label} has:attachment " + (f"after:{mailbox.cursor}" if mailbox.cursor.isdigit()
                                                 else "newer_than:30d")
    ids, page = [], None
    while len(ids) < MAX_LISTED:
        resp = _execute(service.users().messages().list(userId="me", q=query, maxResults=100, pageToken=page))
        ids += [m["id"] for m in resp.get("messages") or []]
        page = resp.get("nextPageToken")
        if not page:
            break
    ids = list(reversed(ids))   # Gmail lists newest first; import oldest first so the cursor only moves forward
    if len(ids) > MAX_MESSAGES_PER_CHECK:
        ids, stats["more"] = ids[:MAX_MESSAGES_PER_CHECK], True
    cursor = int(mailbox.cursor) if mailbox.cursor.isdigit() else 0
    for gmail_id in ids:
        if IngestedEmail.objects.filter(organization=org, message_id=gmail_id).exists():
            stats["already"] += 1   # imported before by the old Gmail poller, which keyed emails by Gmail ID
            continue
        msg = _execute(service.users().messages().get(userId="me", id=gmail_id, format="full"))
        payload = msg.get("payload") or {}
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or []}
        received = datetime.fromtimestamp(int(msg.get("internalDate") or 0) / 1000, tz=dt_timezone.utc)
        key = intake.message_key(headers.get("message-id"), "gmail", gmail_id)
        if intake.already_received(org, key):
            stats["already"] += 1
        else:
            incoming = IncomingEmail(message_id=key, subject=headers.get("subject", "")[:500],
                                     sender=headers.get("from", "")[:320], received_at=received,
                                     recipient=headers.get("to", "")[:320], provider_ref=f"Gmail {gmail_id}")
            if mailbox.sender_allowed(incoming.sender_address):
                incoming.attachments = _attachments(service, gmail_id, payload)
            result = intake.receive_or_give_up(mailbox, incoming, process=process)
            if result.created:
                stats["emails"] += 1
                stats["documents"] += len(result.new_documents)
                stats["skipped_attachments"] += result.skipped
        seconds = int(received.timestamp())
        if seconds > cursor:
            cursor = seconds
            mailbox.cursor = str(cursor)
            Mailbox.objects.filter(pk=mailbox.pk).update(cursor=mailbox.cursor)
    return stats
