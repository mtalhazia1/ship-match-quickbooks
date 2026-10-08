"""One path for every email that reaches ShipMatch, whatever mail system delivered it.

Providers (webhooks, Microsoft Graph, IMAP, Gmail) turn an email into an `IncomingEmail` and call
`receive()`. It records the email once per organization (by Message-ID), applies the mailbox's allowed
sender list, hands every attachment to the single ingest function (which decides which file types it
accepts) and records what happened to each attachment.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parseaddr

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.core.utils import audit
from apps.documents.models import Document, IngestedEmail
from apps.documents.services.ingest import RejectedFile, ingest_bytes
from apps.mailboxes.models import EmailAttachment, Mailbox, MailboxMessage

log = logging.getLogger(__name__)

# Signature logos and pasted icons: images shown inside the email body, not attached documents.
INLINE_IMAGE_MAX_BYTES = 100 * 1024
# Tracking pixels and tiny icons are skipped however they are attached.
TINY_IMAGE_MAX_BYTES = 6 * 1024
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".svg"}
# After this many failed attempts at one email, it is recorded as failed and the mailbox moves on.
MAX_ATTEMPTS_PER_EMAIL = 3


@dataclass
class IncomingAttachment:
    filename: str
    content_type: str = "application/octet-stream"
    content: bytes | None = None
    size: int | None = None
    inline: bool = False
    skip_reason: str = ""   # set by a provider that already knows it can't import this (e.g. too large)

    def __post_init__(self):
        if self.size is None:
            self.size = len(self.content or b"")


@dataclass
class IncomingEmail:
    message_id: str
    subject: str = ""
    sender: str = ""
    received_at: datetime | None = None
    recipient: str = ""
    provider_ref: str = ""
    attachments: list[IncomingAttachment] = field(default_factory=list)
    text: str = ""   # start of the plain-text body; only used to recognise setup emails, never stored

    @property
    def sender_address(self) -> str:
        return parseaddr(self.sender or "")[1].strip().lower()


@dataclass
class ReceiveResult:
    message: MailboxMessage | None
    created: bool
    new_documents: list[Document] = field(default_factory=list)
    duplicate_files: int = 0
    skipped: int = 0

    @property
    def outcome(self) -> str:
        return self.message.outcome if self.message else ""


# ---------------------------------------------------------------- helpers


def message_key(raw: str | None, *fallback) -> str:
    """The Message-ID without angle brackets, or a stable hash when it is missing or unusable."""
    text = (raw or "").strip()
    m = re.search(r"<([^<>\s]+)>", text)
    mid = m.group(1) if m else text.strip("<> ")
    if mid and len(mid) <= 240 and not re.search(r"\s", mid):
        return mid
    basis = mid or "|".join(str(x) for x in fallback)
    return "sha256:" + hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()


def content_hash_key(*parts) -> str:
    return "sha256:" + hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()


def already_received(org, key: str) -> bool:
    return IngestedEmail.objects.filter(organization=org, message_id=key).exists()


def is_image(att: IncomingAttachment) -> bool:
    ctype = (att.content_type or "").lower()
    ext = ("." + att.filename.rsplit(".", 1)[-1].lower()) if "." in (att.filename or "") else ""
    return ctype.startswith("image/") or ext in IMAGE_EXTENSIONS


def skip_reason(att: IncomingAttachment) -> str:
    """Why an attachment is not even offered to ingest, or "" to offer it."""
    if att.skip_reason:
        return att.skip_reason
    if att.content is None:
        return "Couldn't download this attachment"
    if not att.content:
        return "Empty file"
    if is_image(att):
        if att.size <= TINY_IMAGE_MAX_BYTES:
            return "Tiny image (an icon or tracking pixel)"
        if att.inline and att.size <= INLINE_IMAGE_MAX_BYTES:
            return "Picture inside the email text (signature or logo)"
    return ""


def has_documents(incoming: IncomingEmail) -> bool:
    """True when at least one attachment is more than a signature image (polled mailboxes skip the rest)."""
    return any(not (is_image(a) and a.inline and a.size <= INLINE_IMAGE_MAX_BYTES)
               and not (is_image(a) and a.size <= TINY_IMAGE_MAX_BYTES)
               for a in incoming.attachments)


def default_filename(att: IncomingAttachment, n: int) -> str:
    if att.filename:
        return att.filename
    ext = mimetypes.guess_extension((att.content_type or "").split(";")[0].strip()) or ""
    return f"attachment-{n}{ext}"


def setup_note(incoming: IncomingEmail) -> str:
    """Recognise mail-provider setup emails so an admin can finish forwarding without seeing the email."""
    sender = incoming.sender_address
    subject = incoming.subject or ""
    if sender.endswith("@google.com") and "forwarding confirmation" in subject.lower():
        code = re.search(r"#(\d{5,})", subject)
        who = re.search(r"from\s+(\S+@\S+)", subject)
        parts = ["Gmail is asking to confirm forwarding"]
        if who:
            parts.append(f" from {who.group(1).strip('()<>.,')}")
        parts.append(".")
        if code:
            parts.append(f" Confirmation code: {code.group(1)}. Enter it in Gmail under Settings, Forwarding and POP/IMAP.")
        return "".join(parts)
    return ""


def _reason_text(err: Exception, filename: str) -> str:
    text = str(err)
    for prefix in (f"{filename}: ", f"{filename[:120]}: "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.strip() or "Not accepted"
    return (text[0].upper() + text[1:])[:300]


# ---------------------------------------------------------------- receive


def receive(mailbox: Mailbox, incoming: IncomingEmail, process: str = "async") -> ReceiveResult:
    """Record an email and import its attachments. Idempotent: the same Message-ID twice does nothing."""
    org = mailbox.organization
    key = incoming.message_id or content_hash_key(incoming.sender, incoming.subject, incoming.received_at)
    now = timezone.now()
    with transaction.atomic():
        try:
            with transaction.atomic():
                email = IngestedEmail.objects.create(
                    organization=org, message_id=key[:255], subject=(incoming.subject or "")[:500],
                    sender=(incoming.sender or "")[:320], received_at=incoming.received_at or now)
        except IntegrityError:
            existing = IngestedEmail.objects.filter(organization=org, message_id=key[:255]).first()
            return ReceiveResult(message=MailboxMessage.objects.filter(email=existing).first(), created=False)

        msg = MailboxMessage.objects.create(
            organization=org, mailbox=mailbox, mailbox_name=mailbox.label[:160], mailbox_kind=mailbox.kind,
            email=email, recipient=(incoming.recipient or "")[:320], provider_ref=(incoming.provider_ref or "")[:255])
        result = ReceiveResult(message=msg, created=True)

        if not mailbox.sender_allowed(incoming.sender_address):
            msg.outcome = MailboxMessage.Outcome.BLOCKED
            msg.note = (f"{incoming.sender_address or 'The sender'} isn't on this mailbox's allowed sender list, "
                        "so its attachments were not imported.")
            msg.save(update_fields=["outcome", "note"])
            Mailbox.objects.filter(pk=mailbox.pk).update(emails_received=F("emails_received") + 1, last_received_at=now)
            audit(org, "email.blocked", email, mailbox=mailbox.label, sender=incoming.sender_address,
                  subject=incoming.subject[:200])
            return result

        rows = []
        for n, att in enumerate(incoming.attachments, 1):
            name = default_filename(att, n)[:255]
            row = EmailAttachment(message=msg, filename=name, content_type=(att.content_type or "")[:150],
                                  size=min(att.size or 0, 2**31 - 1))
            reason = skip_reason(att)
            if reason:
                row.outcome, row.reason = EmailAttachment.Outcome.SKIPPED, reason[:300]
                result.skipped += 1
                rows.append(row)
                continue
            try:
                doc, created = ingest_bytes(org, name, att.content, source=Document.Source.EMAIL, email=email,
                                            process=process)
            except RejectedFile as e:
                row.outcome, row.reason = EmailAttachment.Outcome.SKIPPED, _reason_text(e, name)
                result.skipped += 1
                rows.append(row)
                continue
            row.document = doc
            if created:
                row.outcome = EmailAttachment.Outcome.IMPORTED
                result.new_documents.append(doc)
            else:
                row.outcome = EmailAttachment.Outcome.DUPLICATE
                row.reason = "This exact file was received before"
                result.duplicate_files += 1
            rows.append(row)
        EmailAttachment.objects.bulk_create(rows)

        email.attachment_count = len(result.new_documents) + result.duplicate_files
        email.save(update_fields=["attachment_count"])
        if result.new_documents:
            msg.outcome = MailboxMessage.Outcome.DOCUMENTS
        elif result.duplicate_files:
            msg.outcome = MailboxMessage.Outcome.DUPLICATES
        else:
            msg.outcome = MailboxMessage.Outcome.NOTHING
        msg.documents_created = len(result.new_documents)
        msg.note = setup_note(incoming)
        msg.save(update_fields=["outcome", "documents_created", "note"])
        Mailbox.objects.filter(pk=mailbox.pk).update(
            emails_received=F("emails_received") + 1,
            documents_received=F("documents_received") + len(result.new_documents),
            attachments_skipped=F("attachments_skipped") + result.skipped,
            last_received_at=now)
        audit(org, "email.received", email, mailbox=mailbox.label, sender=incoming.sender_address,
              subject=incoming.subject[:200], documents=len(result.new_documents),
              duplicates=result.duplicate_files, skipped=result.skipped)
    return result


def receive_or_give_up(mailbox: Mailbox, incoming: IncomingEmail, process: str = "async") -> ReceiveResult:
    """receive() for polled mailboxes: an email that keeps failing is recorded as failed so it can't block
    every later email in the folder. Earlier failures re-raise, so the mailbox stops and tries again later."""
    try:
        return receive(mailbox, incoming, process=process)
    except Exception as exc:
        counter = f"mailbox-email-failures:{mailbox.pk}:{hashlib.sha256(incoming.message_id.encode()).hexdigest()}"
        cache.add(counter, 0, 7 * 24 * 3600)
        try:
            attempts = cache.incr(counter)
        except ValueError:
            attempts = 1
        if attempts < MAX_ATTEMPTS_PER_EMAIL:
            raise
        log.exception("Giving up on email %s in mailbox %s after %s attempts", incoming.message_id, mailbox.pk, attempts)
        return record_failure(mailbox, incoming, exc)


def record_failure(mailbox: Mailbox, incoming: IncomingEmail, exc: Exception) -> ReceiveResult:
    org = mailbox.organization
    with transaction.atomic():
        email, created = IngestedEmail.objects.get_or_create(
            organization=org, message_id=incoming.message_id[:255],
            defaults={"subject": (incoming.subject or "")[:500], "sender": (incoming.sender or "")[:320],
                      "received_at": incoming.received_at or timezone.now()})
        if not created:
            return ReceiveResult(message=MailboxMessage.objects.filter(email=email).first(), created=False)
        msg = MailboxMessage.objects.create(
            organization=org, mailbox=mailbox, mailbox_name=mailbox.label[:160], mailbox_kind=mailbox.kind,
            email=email, recipient=(incoming.recipient or "")[:320], provider_ref=(incoming.provider_ref or "")[:255],
            outcome=MailboxMessage.Outcome.FAILED,
            note=(f"ShipMatch couldn't process this email ({type(exc).__name__}: {exc})."[:900]
                  + " Forward it again or upload its attachments by hand."))
        Mailbox.objects.filter(pk=mailbox.pk).update(emails_received=F("emails_received") + 1)
        audit(org, "email.failed", email, mailbox=mailbox.label, sender=incoming.sender_address,
              error=f"{type(exc).__name__}: {exc}"[:300])
    return ReceiveResult(message=msg, created=True)
