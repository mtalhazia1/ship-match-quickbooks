"""Check polled mailboxes (Microsoft 365, IMAP, Gmail): one at a time per mailbox, errors recorded, never raised."""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone

from apps.core.utils import audit
from apps.mailboxes.models import Mailbox

from .errors import MailboxAuthError, MailboxError

log = logging.getLogger(__name__)
LOCK_SECONDS = 15 * 60


def _provider(kind: str):
    from . import gmail, imap, microsoft

    return {Mailbox.Kind.MICROSOFT: microsoft.poll, Mailbox.Kind.IMAP: imap.poll, Mailbox.Kind.GMAIL: gmail.poll}[kind]


def poll_mailbox(mailbox: Mailbox | int, process: str = "async") -> dict:
    """Check one mailbox now. Returns stats with "status": ok | error | reconnect | busy | skipped."""
    if not isinstance(mailbox, Mailbox):
        mailbox = Mailbox.objects.select_related("organization").filter(pk=mailbox).first()
        if mailbox is None:
            return {"status": "skipped", "reason": "Mailbox no longer exists"}
    if not mailbox.is_polled:
        return {"status": "skipped", "reason": "Forwarding addresses receive email by themselves"}
    lock = f"mailbox-poll:{mailbox.pk}"
    if not cache.add(lock, 1, LOCK_SECONDS):
        return {"status": "busy", "reason": "This mailbox is being checked right now"}
    stats: dict = {}
    try:
        stats = _provider(mailbox.kind)(mailbox, process=process)
        warnings = stats.pop("warnings", [])
        now = timezone.now()
        Mailbox.objects.filter(pk=mailbox.pk).update(
            last_checked_at=now, last_success_at=now, last_error="\n".join(dict.fromkeys(warnings))[:1000],
            needs_reconnect=False)
        stats["status"] = "ok"
        if warnings:
            stats["warning"] = warnings[0]
    except MailboxAuthError as e:
        already = Mailbox.objects.filter(pk=mailbox.pk, needs_reconnect=True).exists()
        Mailbox.objects.filter(pk=mailbox.pk).update(last_checked_at=timezone.now(), needs_reconnect=True,
                                                     last_error=str(e)[:1000])
        if not already:
            audit(mailbox.organization, "mailbox.needs_reconnect", mailbox, name=mailbox.label, error=str(e)[:300])
        stats = {**stats, "status": "reconnect", "error": str(e)}
    except MailboxError as e:
        Mailbox.objects.filter(pk=mailbox.pk).update(last_checked_at=timezone.now(), last_error=str(e)[:1000])
        stats = {**stats, "status": "error", "error": str(e)}
    except Exception as e:   # a bug or an unexpected library error: record it, don't take down the caller
        log.exception("Checking mailbox %s failed", mailbox.pk)
        message = f"Unexpected problem while checking this mailbox ({type(e).__name__}: {e})"[:1000]
        Mailbox.objects.filter(pk=mailbox.pk).update(last_checked_at=timezone.now(), last_error=message)
        stats = {**stats, "status": "error", "error": message}
    finally:
        cache.delete(lock)
    return stats


def due_mailboxes():
    return (Mailbox.objects.select_related("organization")
            .filter(enabled=True, needs_reconnect=False, kind__in=Mailbox.POLLED_KINDS).order_by("last_checked_at", "pk"))


def summary(stats: dict) -> str:
    """One sentence for a flash message after "Check now"."""
    if stats.get("status") == "busy":
        return "This mailbox is being checked right now. Refresh in a minute to see the result."
    if stats.get("status") in ("error", "reconnect"):
        return f"Couldn't check the mailbox: {stats.get('error')}"
    emails, docs = stats.get("emails", 0), stats.get("documents", 0)
    if not emails:
        text = "Checked: no new emails with attachments."
    else:
        text = (f"Checked: {emails} new email{'s' if emails != 1 else ''}, {docs} new document{'s' if docs != 1 else ''}"
                f" added.")
    if stats.get("more"):
        text += " There are more; the next check continues."
    if stats.get("warning"):
        text += f" {stats['warning']}"
    return text
