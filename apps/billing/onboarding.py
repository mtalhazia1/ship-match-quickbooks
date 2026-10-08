"""Getting-started checklist for organizations created by self-serve sign-up. Each item is ticked from what
really exists (a connection, a mailbox, documents, colleagues), never from a click on the checklist itself.

Another accounting integration (Xero, ...) adds its own "connected" check with register_accounting_check(fn),
fn(org) -> bool, from its AppConfig.ready().
"""
from __future__ import annotations

from dataclasses import dataclass

from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.core.models import Membership
from apps.documents.models import Document, IngestedEmail

from .models import Onboarding

ACCOUNTING_CHECKS = []


def register_accounting_check(fn) -> None:
    if fn not in ACCOUNTING_CHECKS:
        ACCOUNTING_CHECKS.append(fn)


def _quickbooks_connected(org) -> bool:
    from apps.accounting.models import QBOConnection

    return QBOConnection.objects.filter(organization=org, needs_reconnect=False).exists()


register_accounting_check(_quickbooks_connected)


def _xero_connected(org) -> bool:
    from apps.accounting.models import XeroConnection

    return XeroConnection.objects.filter(organization=org, needs_reconnect=False).exclude(tenant_id="").exists()


register_accounting_check(_xero_connected)


@dataclass
class Item:
    key: str
    title: str
    help: str
    done: bool
    url: str
    action: str


def _url(name: str, *args) -> str:
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


def _email_intake_ready(org) -> bool:
    from apps.mailboxes.models import Mailbox

    connected = Mailbox.objects.filter(organization=org, enabled=True, kind__in=Mailbox.POLLED_KINDS,
                                       needs_reconnect=False).exists()
    return connected or IngestedEmail.objects.filter(organization=org).exists()


def items(org) -> list[Item]:
    return [
        Item("accounting", "Connect your accounting system",
             "Approved bills are posted there with the invoice PDF attached.",
             any(check(org) for check in ACCOUNTING_CHECKS), _url("accounting:settings", org.pk), "Connect"),
        Item("email", "Set up email intake",
             "Forward supplier emails to ShipMatch, or connect the AP mailbox, so documents arrive on their own.",
             _email_intake_ready(org), _url("mailboxes:index"), "Set up email"),
        Item("documents", "Upload your first documents",
             "A bill of lading and its invoices are enough to see a shipment checked.",
             Document.objects.filter(organization=org).exists(), _url("review:queue"), "Upload"),
        Item("team", "Invite your team",
             "Add reviewers and approvers. Maker-checker means the person who prepares a shipment can't approve it.",
             Membership.objects.filter(organization=org).count() > 1, _url("core:team"), "Invite"),
    ]


def checklist(org) -> dict | None:
    """The checklist to show, or None (not a self-serve organization, dismissed, or everything done)."""
    state = Onboarding.objects.filter(organization=org).first()
    if state is None or state.dismissed_at or state.completed_at:
        return None
    rows = items(org)
    done = sum(1 for i in rows if i.done)
    if done == len(rows):
        Onboarding.objects.filter(pk=state.pk, completed_at__isnull=True).update(completed_at=timezone.now())
        return None
    return {"items": rows, "done": done, "total": len(rows), "percent": int(round(100 * done / len(rows)))}


def dismiss(org) -> bool:
    return bool(Onboarding.objects.filter(organization=org, dismissed_at__isnull=True)
                .update(dismissed_at=timezone.now()))
