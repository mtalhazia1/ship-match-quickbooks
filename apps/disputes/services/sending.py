"""Send a dispute to the vendor through Django's email backend, with the invoice PDF attached.

Replies go to the organization's accounts payable mailbox (Settings > Disputes), or to the sender's own
address when none is set. The Message-ID is recorded so a reply can be matched to the dispute later.
"""
from __future__ import annotations

import logging
from email.utils import make_msgid, parseaddr

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from ..models import Dispute, DisputeEvent, DisputeSettings
from .workflow import DisputeError, _audit, _event, clean_emails, follow_up_default, remember_contact

log = logging.getLogger(__name__)


def reply_to_for(dispute: Dispute, user) -> str:
    configured = DisputeSettings.for_org(dispute.organization).reply_to
    return configured or (getattr(user, "email", "") or "")


def _domain(address: str) -> str:
    _, email = parseaddr(address)
    domain = email.rpartition("@")[2].strip()
    return domain if domain and "." in domain else "shipmatch.local"


def build_message(dispute: Dispute, user) -> EmailMessage:
    to = clean_emails(dispute.vendor_email, "vendor's email address", required=True)
    cc = clean_emails(dispute.cc, "copy addresses")
    if not dispute.subject.strip() or not dispute.body.strip():
        raise DisputeError("The email needs a subject and a message.")
    reply_to = reply_to_for(dispute, user)
    if not reply_to:
        raise DisputeError("Vendor replies need somewhere to go. An admin can set the reply-to address in "
                           "Settings > Disputes, or add an email address to your profile.")
    invoice = dispute.invoice
    if invoice is None:
        raise DisputeError("The disputed invoice was removed, so there is nothing to attach.")
    try:
        with invoice.file.open("rb") as fh:
            pdf = fh.read()
    except (FileNotFoundError, OSError, ValueError):
        raise DisputeError(f"The invoice PDF ({invoice.original_filename}) could not be read from storage, so the "
                           "dispute was not sent. Check the file storage, then try again.")
    copy = DisputeSettings.for_org(dispute.organization).copy_reply_to
    message_id = make_msgid(idstring=dispute.reference, domain=_domain(settings.DEFAULT_FROM_EMAIL))
    msg = EmailMessage(
        subject=dispute.subject.strip(), body=dispute.body, from_email=settings.DEFAULT_FROM_EMAIL, to=to, cc=cc,
        bcc=[reply_to] if copy and reply_to not in to + cc else [], reply_to=[reply_to],
        headers={"Message-ID": message_id, "X-ShipMatch-Dispute": dispute.reference})
    filename = invoice.original_filename if invoice.original_filename.lower().endswith(".pdf") \
        else f"{invoice.original_filename}.pdf"
    msg.attach(filename, pdf, "application/pdf")
    return msg


def send(dispute: Dispute, user) -> None:
    """Send a draft. Raises DisputeError with a message for the user; the draft is kept on failure."""
    if dispute.status != Dispute.Status.DRAFT:
        raise DisputeError(f"{dispute.reference} was already sent on {timezone.localtime(dispute.sent_at):%d %b %Y}."
                           if dispute.sent_at else f"{dispute.reference} can't be sent in its current state.")
    if not dispute.items.exists():
        raise DisputeError("Link at least one issue before sending.")
    msg = build_message(dispute, user)
    # Claim the draft first so two people pressing Send at once can't email the vendor twice.
    error, sent = None, 0
    with transaction.atomic():
        claimed = Dispute.objects.select_for_update().filter(pk=dispute.pk, status=Dispute.Status.DRAFT).first()
        if claimed is None:
            raise DisputeError(f"{dispute.reference} was sent by someone else a moment ago.")
        try:
            sent = msg.send(fail_silently=False)
        except Exception as e:  # SMTP refusal, network, bad header
            error = e
        if not error and sent:
            dispute.status, dispute.sent_at, dispute.sent_by = Dispute.Status.SENT, timezone.now(), user
            dispute.message_id = msg.extra_headers["Message-ID"][:250]
            if not dispute.follow_up_on or dispute.follow_up_on < timezone.localdate():
                dispute.follow_up_on = follow_up_default(dispute.organization)
            dispute.updated_by = user
            dispute.save(update_fields=["status", "sent_at", "sent_by", "message_id", "follow_up_on", "updated_by",
                                        "updated_at"])
    if error is not None or not sent:
        reason = f"{type(error).__name__}: {str(error)[:160]}" if error else "no message was sent"
        log.warning("Dispute %s could not be sent: %s", dispute.reference, reason)
        _event(dispute, DisputeEvent.Kind.SEND_FAILED, user, reason)
        _audit(dispute, "dispute.send_failed", user, error=reason)
        raise DisputeError(f"The email server didn't accept the message ({reason}). The draft is saved; "
                           "try again, or ask an admin to check the email settings.")
    remember_contact(dispute)
    recipients = ", ".join(msg.to + msg.cc)
    _event(dispute, DisputeEvent.Kind.SENT, user, f"Emailed {recipients} with {msg.attachments[0][0]}",
           message_id=dispute.message_id, to=msg.to, cc=msg.cc, reply_to=msg.reply_to)
    _audit(dispute, "dispute.sent", user, to=recipients, message_id=dispute.message_id)
