"""What the team can be alerted about, and the message for each.

Events come from two places:
* audit rows (AUDIT_EVENTS maps an audit action to an alert), written after the action commits;
* shipment status changes into "needs review" or "ready to approve" (a shipment's status is saved
  after some of its audit rows, so it is watched directly).

A Message is channel-neutral: a title, one line of text, label/value facts and a link. The renderers
in render.py turn it into Slack Block Kit, a Teams Adaptive Card or an HTML email.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from django.conf import settings
from django.urls import reverse

NEEDS_REVIEW = "shipment.needs_review"
READY = "shipment.ready"
BILL_FAILED = "bill.failed"
QBO_RECONNECT = "qbo.needs_reconnect"
MAILBOX_RECONNECT = "mailbox.needs_reconnect"
DISPUTE_OVERDUE = "dispute.overdue"
CREDIT_RECEIVED = "dispute.credit_received"
DIGEST = "digest.daily"
TEST = "test"

# key, label, when it is sent
EVENTS = [
    (NEEDS_REVIEW, "Shipment needs review", "A shipment's checks found something a person must look at."),
    (READY, "Shipment ready for approval", "Every check passed or was accepted; an approver can sign off."),
    (BILL_FAILED, "Posting to accounting failed", "QuickBooks or Xero refused a bill or vendor credit. The reason is in the message."),
    (QBO_RECONNECT, "QuickBooks needs reconnecting", "Intuit stopped accepting the connection; nothing can post."),
    (MAILBOX_RECONNECT, "Mailbox needs reconnecting", "A connected mailbox stopped accepting the sign-in; its email isn't being read."),
    (DISPUTE_OVERDUE, "Dispute overdue", "A vendor hasn't answered a dispute by its follow-up date."),
    (CREDIT_RECEIVED, "Credit received", "A vendor credited money on a dispute."),
    (DIGEST, "Daily summary", "Once a day: what needs review or approval, failed postings and money at risk."),
]
EVENT_LABELS = {k: label for k, label, _ in EVENTS}
EVENT_KEYS = [k for k, _, _ in EVENTS]
DEFAULT_EVENTS = {
    "slack": [READY, BILL_FAILED, QBO_RECONNECT, MAILBOX_RECONNECT, DISPUTE_OVERDUE, CREDIT_RECEIVED],
    "teams": [READY, BILL_FAILED, QBO_RECONNECT, MAILBOX_RECONNECT, DISPUTE_OVERDUE, CREDIT_RECEIVED],
    "email": [QBO_RECONNECT, MAILBOX_RECONNECT, DIGEST],
}

# Audit actions that raise an alert.
AUDIT_EVENTS = {
    "bill.failed": BILL_FAILED,
    "vendor_credit.failed": BILL_FAILED,
    "mailbox.needs_reconnect": MAILBOX_RECONNECT,
    "qbo.needs_reconnect": QBO_RECONNECT,
    "dispute.overdue": DISPUTE_OVERDUE,
    "dispute.credit_received": CREDIT_RECEIVED,
}


@dataclass
class Message:
    event: str
    title: str
    text: str
    facts: list[list[str]] = field(default_factory=list)
    url: str = ""
    link_label: str = "Open in ShipMatch"
    tone: str = "info"  # info, warning, error, good
    org_name: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Message:
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


def absolute(path: str) -> str:
    base = (getattr(settings, "SITE_URL", "") or "http://localhost:8000").rstrip("/")
    return f"{base}{path}"


def _money(amount, currency: str) -> str:
    try:
        return f"{currency} {Decimal(str(amount)):,.2f}".strip()
    except Exception:
        return str(amount)


# --------------------------------------------------------------------------- shipments


def shipment_message(shipment, event: str) -> Message:
    from apps.shipments.services.approval import shipment_totals

    org = shipment.organization
    open_issues = list(shipment.issues.filter(resolved=False))
    errors = sum(1 for i in open_issues if i.severity == "error")
    warnings = len(open_issues) - errors
    totals = shipment_totals(shipment)
    facts = [["Bill of lading", shipment.bl_number or "Not received"]]
    if shipment.container_numbers:
        facts.append(["Containers", ", ".join(shipment.container_numbers[:6])
                      + (f" and {len(shipment.container_numbers) - 6} more" if len(shipment.container_numbers) > 6 else "")])
    if totals.by_currency:
        facts.append(["Payable total", ", ".join(_money(v, k) for k, v in totals.by_currency.items())])
    if event == NEEDS_REVIEW:
        parts = []
        if errors:
            parts.append(f"{errors} error{'s' if errors != 1 else ''}")
        if warnings:
            parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
        at_risk: dict[str, Decimal] = {}
        for i in open_issues:
            if i.amount_at_risk:
                cur = i.currency or org.home_currency
                at_risk[cur] = at_risk.get(cur, Decimal("0")) + i.amount_at_risk
        if at_risk:
            facts.append(["Money at risk", ", ".join(_money(v, k) for k, v in at_risk.items())])
        titles = sorted({i.title for i in open_issues})[:3]
        text = f"{' and '.join(parts) or 'Issues'} to check" + (f": {', '.join(titles)}." if titles else ".")
        return Message(event, f"{shipment.reference} needs review", text, facts,
                       absolute(reverse("review:shipment", args=[shipment.pk])), "Review the shipment",
                       "error" if errors else "warning", org.name)
    from apps.workflow.services.links import approval_url  # focused approval page, see apps/workflow

    return Message(event, f"{shipment.reference} is ready for approval",
                   "All checks passed or were accepted. An approver can approve it.", facts,
                   approval_url(shipment), "Review and approve", "good", org.name)


# --------------------------------------------------------------------------- audit events


# Alerts other apps add (register_event); builder(audit_event) -> Message | None.
EXTRA_BUILDERS: dict = {}


def register_event(key: str, label: str, description: str, *, audit_actions=(), builder=None,
                   default_for=("slack", "teams")) -> None:
    """Add an alert from another app's AppConfig.ready(), without editing this file.

    audit_actions: audit actions that raise it; builder(audit_event) returns the Message (or None to skip);
    default_for: channel kinds that get it ticked by default when a channel is created.
    """
    if key not in EVENT_LABELS:
        EVENTS.append((key, label, description))
        EVENT_LABELS[key] = label
        EVENT_KEYS.append(key)
    for action in audit_actions:
        AUDIT_EVENTS[action] = key
    if builder is not None:
        EXTRA_BUILDERS[key] = builder
    for kind in default_for:
        if key not in DEFAULT_EVENTS.setdefault(kind, []):
            DEFAULT_EVENTS[kind].append(key)


def audit_message(event: str, audit_event) -> Message | None:
    builder = {BILL_FAILED: _bill_failed, QBO_RECONNECT: _qbo_reconnect, MAILBOX_RECONNECT: _mailbox_reconnect,
               DISPUTE_OVERDUE: _dispute_overdue,
               CREDIT_RECEIVED: _credit_received}.get(event) or EXTRA_BUILDERS.get(event)
    return builder(audit_event) if builder else None


def _bill_failed(e) -> Message | None:
    from apps.documents.models import Document

    doc = Document.objects.filter(pk=e.object_id, organization=e.organization).select_related("match__shipment").first()
    if doc is None:
        return None
    shipment = doc.match.shipment if hasattr(doc, "match") else None
    facts = [["Invoice", doc.original_filename]]
    vendor = doc.field("vendor_name")
    if vendor:
        facts.append(["Vendor", str(vendor)])
    if doc.field("total_amount") not in (None, ""):
        facts.append(["Amount", _money(doc.field("total_amount"), doc.field("currency") or e.organization.home_currency)])
    if shipment:
        facts.insert(0, ["Shipment", shipment.reference])
    system = str((e.data or {}).get("system") or "QuickBooks")
    error = str((e.data or {}).get("error") or f"{system} didn't say why.")
    url = absolute(reverse("review:shipment", args=[shipment.pk]) + f"#doc-{doc.pk}") if shipment else \
        absolute(reverse("review:document", args=[doc.pk]))
    what = "vendor credit" if e.action.startswith("vendor_credit") else "bill"
    return Message(BILL_FAILED, f"A {what} couldn't be posted to {system}", error[:600], facts, url,
                   "Open the shipment", "error", e.organization.name)


def _qbo_reconnect(e) -> Message:
    org = e.organization
    return Message(QBO_RECONNECT, "QuickBooks needs to be connected again",
                   "Intuit no longer accepts the saved connection, so approved bills can't be posted. "
                   "An admin can connect again from Settings; vendor rules and accounts are kept.",
                   [["Organization", org.name]], absolute(reverse("accounting:settings", args=[org.pk])),
                   "Reconnect QuickBooks", "error", org.name)


def _mailbox_reconnect(e) -> Message:
    org = e.organization
    name = str((e.data or {}).get("name") or "A mailbox")
    error = str((e.data or {}).get("error") or "")
    text = (f"{name} no longer accepts the saved sign-in, so documents emailed there aren't being read. "
            "An admin can connect it again from Settings, Email intake.")
    facts = [["Mailbox", name], ["Organization", org.name]]
    if error:
        facts.append(["Reason", error[:200]])
    return Message(MAILBOX_RECONNECT, "A mailbox needs to be connected again", text, facts,
                   absolute(reverse("mailboxes:index")), "Open email intake", "error", org.name)


def _dispute(e):
    from apps.disputes.models import Dispute

    return Dispute.objects.filter(pk=e.object_id, organization=e.organization).first()


def _dispute_overdue(e) -> Message | None:
    d = _dispute(e)
    if d is None:
        return None
    days = (e.data or {}).get("days_overdue")
    facts = [["Vendor", d.vendor_name], ["Disputed", _money(d.amount_disputed, d.currency)]]
    if d.sent_at:
        facts.append(["Sent", f"{d.sent_at:%d %b %Y}"])
    if d.follow_up_on:
        facts.append(["Follow-up date", f"{d.follow_up_on:%d %b %Y}"])
    if d.shipment_reference:
        facts.append(["Shipment", d.shipment_reference])
    late = f" ({days} day{'s' if days != 1 else ''} late)" if isinstance(days, int) and days > 0 else ""
    return Message(DISPUTE_OVERDUE, f"{d.reference}: no answer from {d.vendor_name}",
                   f"The follow-up date passed without a reply{late}. Contact the vendor again, "
                   "then log their answer or set a new date.", facts,
                   absolute(reverse("disputes:detail", args=[d.pk])), "Open the dispute", "warning", d.organization.name)


def _credit_received(e) -> Message | None:
    d = _dispute(e)
    if d is None:
        return None
    facts = [["Vendor", d.vendor_name], ["Recovered", _money(d.amount_recovered, d.currency)],
             ["Disputed", _money(d.amount_disputed, d.currency)]]
    if d.shipment_reference:
        facts.append(["Shipment", d.shipment_reference])
    if d.credit_note_id:
        facts.append(["Credit note", d.credit_note.original_filename])
    return Message(CREDIT_RECEIVED, f"Credit received: {_money(d.amount_recovered, d.currency)} from {d.vendor_name}",
                   f"{d.reference} recovered money on invoice {d.invoice_number or 'without number'}.", facts,
                   absolute(reverse("disputes:detail", args=[d.pk])), "Open the dispute", "good", d.organization.name)


def channel_test_message(channel) -> Message:
    return Message(TEST, "Test message from ShipMatch",
                   f"Alerts for {channel.organization.name} will arrive in this channel ({channel.name}).",
                   [["Channel", channel.name], ["Alerts", ", ".join(channel.event_labels) or "None chosen yet"]],
                   absolute(reverse("notifications:settings")), "Alert settings", "info", channel.organization.name)
