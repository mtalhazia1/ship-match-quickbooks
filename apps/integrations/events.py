"""What outgoing webhooks can report, and the JSON sent for each.

Payload (the same envelope for every type):
    {"id": "evt_...", "type": "shipment.approved", "created": "2026-10-03T09:15:02Z",
     "organization": "acme", "api_version": "2026-10-01", "data": {"object": {...summary...}}}

Summaries hold what a connected system needs to act (references, status, amounts, a link) and never secrets,
document text or files. Other apps add types with register_webhook_event() from their AppConfig.ready().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.urls import NoReverseMatch, reverse

API_VERSION = "2026-10-01"
TEST = "test.ping"


@dataclass
class EventType:
    key: str
    label: str
    description: str
    audit_actions: list[str] = field(default_factory=list)
    builder: object = None   # fn(audit_event) -> (object_type, object_id, summary) | None


EVENT_TYPES: dict[str, EventType] = {}
AUDIT_ACTIONS: dict[str, str] = {}   # audit action -> webhook event type


def register_webhook_event(key: str, label: str, description: str, *, audit_actions=(), builder=None) -> None:
    EVENT_TYPES[key] = EventType(key, label, description, list(audit_actions), builder)
    for action in audit_actions:
        AUDIT_ACTIONS[action] = key


def choices() -> list[EventType]:
    return list(EVENT_TYPES.values())


# --------------------------------------------------------------------------- helpers


def _link(name: str, *args) -> str:
    try:
        return f"{settings.SITE_URL}{reverse(name, args=args)}"
    except NoReverseMatch:
        return ""


def _iso(value) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _money(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return None


SUMMARY_FIELDS = ("vendor_name", "carrier_name", "invoice_number", "invoice_date", "currency", "total_amount",
                  "bl_number", "container_numbers", "po_numbers", "credit_note_number", "original_invoice_number")


def document_summary(doc) -> dict:
    values = {f.name: f.value for f in doc.fields.all() if f.name in SUMMARY_FIELDS}
    if "total_amount" in values:
        values["total_amount"] = _money(values["total_amount"])
    shipment = doc.match.shipment if hasattr(doc, "match") else None
    return {
        "object": "document", "id": doc.pk, "filename": doc.original_filename, "type": doc.doc_type,
        "status": doc.status, "source": doc.source, "format": doc.source_format,
        "received_at": _iso(doc.received_at), "parent_id": doc.parent_id,
        "shipment": {"id": shipment.pk, "reference": shipment.reference} if shipment else None,
        "fields": values, "url": _link("review:document", doc.pk),
    }


def shipment_summary(shipment) -> dict:
    from apps.shipments.services.approval import shipment_totals

    open_issues = list(shipment.issues.filter(resolved=False).values_list("severity", flat=True))
    totals = shipment_totals(shipment)
    approved_by = shipment.approved_by
    return {
        "object": "shipment", "id": shipment.pk, "reference": shipment.reference, "status": shipment.status,
        "bl_number": shipment.bl_number, "container_numbers": list(shipment.container_numbers or []),
        "po_numbers": list(shipment.po_numbers or []),
        "totals": {cur: _money(v) for cur, v in totals.by_currency.items()},
        "total_home_currency": _money(totals.home) if totals.home is not None else None,
        "home_currency": shipment.organization.home_currency,
        "open_errors": sum(1 for s in open_issues if s == "error"),
        "open_warnings": sum(1 for s in open_issues if s != "error"),
        "approved_at": _iso(shipment.approved_at),
        "approved_by": (approved_by.get_full_name() or approved_by.get_username()) if approved_by else None,
        "updated_at": _iso(shipment.updated_at), "url": _link("review:shipment", shipment.pk),
    }


def issue_summary(issue) -> dict:
    return {
        "object": "issue", "id": issue.pk, "code": issue.code, "title": issue.title, "severity": issue.severity,
        "message": issue.message, "amount_at_risk": _money(issue.amount_at_risk),
        "currency": issue.currency or None, "resolved": issue.resolved,
        "shipment": {"id": issue.shipment_id, "reference": issue.shipment.reference} if issue.shipment_id else None,
        "document_id": issue.document_id,
        "url": _link("review:shipment", issue.shipment_id) if issue.shipment_id else "",
    }


# --------------------------------------------------------------------------- builders from audit rows


def _document(e):
    from apps.documents.models import Document

    doc = (Document.objects.filter(pk=e.object_id, organization=e.organization)
           .select_related("match__shipment").prefetch_related("fields").first())
    return ("Document", doc.pk, document_summary(doc)) if doc else None


def _shipment(e):
    from apps.shipments.models import Shipment

    s = Shipment.objects.filter(pk=e.object_id, organization=e.organization).select_related(
        "organization", "approved_by").first()
    if s is None:
        return None
    summary = shipment_summary(s)
    note = (e.data or {}).get("note") or (e.data or {}).get("reason")
    if e.action == "shipment.rejected" and note:
        summary["rejection_note"] = str(note)[:500]
    return ("Shipment", s.pk, summary)


def _bill(e):
    from apps.accounting.models import PostedBill
    from apps.documents.models import Document

    doc = (Document.objects.filter(pk=e.object_id, organization=e.organization)
           .select_related("match__shipment").prefetch_related("fields").first())
    if doc is None:
        return None
    pb = PostedBill.objects.filter(document=doc).first()
    shipment = doc.match.shipment if hasattr(doc, "match") else None
    data = e.data or {}
    summary = {
        "object": "bill", "kind": "vendor_credit" if e.action.startswith("vendor_credit") else "bill",
        "status": "posted" if e.action.endswith(".posted") else "failed",
        "accounting_system": (pb.system if pb else None) or data.get("system", "").lower() or None,
        "accounting_id": (pb.qbo_bill_id if pb else "") or data.get("qbo_bill_id") or None,
        "error": str(data.get("error"))[:300] if data.get("error") else None,
        "document": document_summary(doc),
        "shipment": {"id": shipment.pk, "reference": shipment.reference} if shipment else None,
        "url": _link("review:shipment", shipment.pk) if shipment else _link("review:document", doc.pk),
    }
    return ("Document", doc.pk, summary)


def _dispute(e):
    try:
        from apps.disputes.models import Dispute
    except ImportError:  # pragma: no cover - disputes app not installed
        return None
    d = Dispute.objects.filter(pk=e.object_id, organization=e.organization).first()
    if d is None:
        return None
    summary = {"object": "dispute", "id": d.pk}
    for name in ("reference", "status", "vendor_name", "invoice_number", "currency", "shipment_reference"):
        if hasattr(d, name):
            summary[name] = getattr(d, name)
    for name in ("amount_disputed", "amount_recovered"):
        if hasattr(d, name):
            summary[name] = _money(getattr(d, name))
    for name in ("sent_at", "follow_up_on", "recovered_at"):
        if getattr(d, name, None):
            summary[name] = _iso(getattr(d, name))
    summary["url"] = _link("disputes:detail", d.pk)
    return ("Dispute", d.pk, summary)


register_webhook_event("document.received", "Document received",
                       "A file arrived by upload, email, the API or a folder import.",
                       audit_actions=["document.received"], builder=_document)
register_webhook_event("document.extracted", "Document read",
                       "Values were read from a document (type, references, amounts).",
                       audit_actions=["document.extracted"], builder=_document)
register_webhook_event("shipment.needs_review", "Shipment needs review",
                       "A shipment's checks found something a person must look at.")
register_webhook_event("shipment.ready", "Shipment ready for approval",
                       "Every check passed or was accepted.")
register_webhook_event("shipment.approved", "Shipment approved", "An approver approved a shipment.",
                       audit_actions=["shipment.approved"], builder=_shipment)
register_webhook_event("shipment.rejected", "Shipment rejected", "An approver rejected a shipment, with a note.",
                       audit_actions=["shipment.rejected"], builder=_shipment)
register_webhook_event("bill.posted", "Bill posted", "A bill or vendor credit was posted to accounting.",
                       audit_actions=["bill.posted", "vendor_credit.posted"], builder=_bill)
register_webhook_event("bill.failed", "Bill posting failed", "Accounting refused a bill or vendor credit.",
                       audit_actions=["bill.failed", "vendor_credit.failed"], builder=_bill)
register_webhook_event("issue.created", "Issue found", "A check found a new problem on a shipment.")


def register_dispute_events() -> None:
    for key, label, description in (
        ("dispute.sent", "Dispute sent", "A dispute email was sent to a vendor."),
        ("dispute.credit_received", "Dispute credit received", "A vendor credited money on a dispute."),
        ("dispute.resolved", "Dispute resolved", "A dispute was resolved."),
        ("dispute.closed", "Dispute closed", "A dispute was closed without (full) recovery."),
        ("dispute.overdue", "Dispute overdue", "A vendor hasn't answered by the follow-up date."),
    ):
        register_webhook_event(key, label, description, audit_actions=[key], builder=_dispute)


def envelope(event) -> dict:
    """The full JSON body of a stored WebhookEvent."""
    return event.payload


def build_payload(org, event_type: str, summary: dict, event_id: str, created) -> dict:
    return {"id": event_id, "type": event_type, "created": _iso(created), "organization": org.slug,
            "api_version": API_VERSION, "data": {"object": summary}}
