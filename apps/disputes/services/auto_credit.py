"""A credit note that names a disputed invoice is linked to its dispute for an approver to confirm.

When a credit note is read and its "original invoice number" matches the invoice of a dispute that is
waiting for the vendor (same vendor, same currency), the dispute's timeline says so and its "Record the
credit" form is filled in with that document and amount. Nothing is recorded or resolved until an
approver saves it: anyone can email a credit note to the AP inbox, so a document from outside never
releases a shipment on its own. Anything uncertain (no number or vendor, two possible disputes, another
currency, a non-positive amount) is left alone.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from apps.accounting.models import vendor_key
from apps.core.utils import audit
from apps.documents.models import Document
from apps.documents.services.normalize import norm_ref

from ..evidence import money
from ..models import Dispute, DisputeEvent

log = logging.getLogger(__name__)


def _amount(value) -> Decimal | None:
    try:
        return abs(Decimal(str(value))).quantize(Decimal("0.01")) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def find_dispute(doc: Document) -> Dispute | None:
    """The one waiting dispute this credit note answers, or None."""
    if not doc.is_credit:
        return None
    data = doc.data()
    original = norm_ref(data.get("original_invoice_number") or "")
    if not original:
        return None
    vk = vendor_key(data.get("vendor_name") or "")
    if not vk:
        return None
    found = []
    waiting = (Dispute.objects.filter(organization=doc.organization, status__in=Dispute.WAITING,
                                      credit_note__isnull=True)
               .exclude(invoice_id=doc.pk).select_related("invoice"))
    for d in waiting:
        if d.vendor_key != vk:
            continue
        if d.invoice and norm_ref(d.invoice.field("invoice_number") or "") == original:
            found.append(d)
    return found[0] if len(found) == 1 else None


def apply(doc: Document) -> Dispute | None:
    """Suggest this credit note on its dispute. Returns the dispute, or None if nothing matched."""
    dispute = find_dispute(doc)
    if dispute is None:
        return None
    data = doc.data()
    amount = _amount(data.get("total_amount"))
    currency = (data.get("currency") or dispute.currency or "").upper()
    if not amount or (dispute.currency and currency and currency != dispute.currency.upper()):
        return None
    if dispute.events.filter(kind=DisputeEvent.Kind.NOTE, data__suggested_credit=doc.pk).exists():
        return dispute
    number = data.get("credit_note_number") or doc.original_filename
    DisputeEvent.objects.create(
        dispute=dispute, kind=DisputeEvent.Kind.NOTE,
        text=(f"Credit note {number} arrived naming invoice {data.get('original_invoice_number')} for "
              f"{money(amount, dispute.currency or currency)}. Check it, then save the credit."),
        data={"suggested_credit": doc.pk, "amount": str(amount)})
    audit(dispute.organization, "dispute.credit_matched", dispute, reference=dispute.reference,
          vendor=dispute.vendor_name, credit_note=doc.original_filename, amount=money(amount, dispute.currency))
    return dispute


def suggestion(dispute: Dispute) -> dict | None:
    """The latest matched credit note still waiting to be confirmed: {"doc", "amount"}."""
    if dispute.credit_note_id or dispute.status not in Dispute.WAITING:
        return None
    event = dispute.events.filter(kind=DisputeEvent.Kind.NOTE, data__has_key="suggested_credit").order_by(
        "-created_at", "-id").first()
    if event is None:
        return None
    doc = Document.objects.filter(organization=dispute.organization, pk=event.data.get("suggested_credit")).first()
    return {"doc": doc, "amount": event.data.get("amount", "")} if doc else None


def on_audit_event(sender, instance, created, **kwargs):
    """AuditEvent post_save: a document was read. Runs after the surrounding transaction commits."""
    if not created or instance.action != "document.extracted" or instance.object_type != "Document":
        return
    if instance.data.get("doc_type") not in Document.CREDIT_TYPES:
        return
    from django.db import transaction

    def run():
        doc = Document.objects.filter(pk=instance.object_id).first()
        if doc is None:
            return
        try:
            apply(doc)
        except Exception:  # never break document processing
            log.exception("Applying credit note %s to a dispute failed", instance.object_id)

    transaction.on_commit(run)
