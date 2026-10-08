"""Validation rules for credit notes, registered from IntakeConfig.ready().

A credit note is checked against the invoice it reduces; it is never compared with invoices for
duplicates (the duplicate invoice rule only looks at payable documents), but two copies of the same
credit note are flagged, because claiming a credit twice is as wrong as paying an invoice twice.
"""
from __future__ import annotations

from apps.accounting.models import vendor_key
from apps.documents.models import Document
from apps.documents.services.normalize import norm_ref
from apps.shipments.services.validation import ERROR, WARNING, IssueSpec, _dec

from .services.credit import find_original_invoice


def check_credit_note(doc: Document, data: dict):
    if not doc.is_credit:
        return
    name = doc.original_filename
    original = data.get("original_invoice_number")
    if not original:
        yield IssueSpec("credit_no_original", WARNING, f"{name}: the credit note doesn't say which invoice it reduces",
                        doc)
        return
    invoice = find_original_invoice(doc, data)
    if invoice is None:
        yield IssueSpec("credit_original_not_found", WARNING,
                        f"{name}: credits invoice {original}, which hasn't been received", doc,
                        {"key": norm_ref(original)})
        return
    own = getattr(doc, "match", None)
    theirs = getattr(invoice, "match", None)
    if own and theirs and own.shipment_id != theirs.shipment_id:
        yield IssueSpec("credit_original_elsewhere", WARNING,
                        f"{name}: the invoice it credits ({invoice.original_filename}) is in "
                        f"{theirs.shipment.reference}", doc, {"key": norm_ref(original)})
    credit, invoiced = _dec(data.get("total_amount")), _dec(invoice.field("total_amount"))
    same_currency = (data.get("currency") or "").upper() == (invoice.field("currency") or "").upper() or \
        not data.get("currency") or not invoice.field("currency")
    if credit is not None and invoiced is not None and same_currency and abs(credit) > abs(invoiced):
        yield IssueSpec("credit_exceeds_invoice", WARNING,
                        f"{name}: credits {abs(credit):,.2f}, more than invoice {original} ({abs(invoiced):,.2f})",
                        doc, {"key": norm_ref(original), "credit": str(credit), "invoice": str(invoiced)},
                        currency=data.get("currency") or "")


def check_duplicate_credit_note(doc: Document, data: dict):
    """Same vendor, credit note number and amount received before."""
    if not doc.is_credit or not data.get("credit_note_number"):
        return
    vk, num = vendor_key(data.get("vendor_name")), norm_ref(data.get("credit_note_number"))
    amount = _dec(data.get("total_amount"))
    earlier = (Document.objects.filter(organization=doc.organization, doc_type=doc.doc_type, pk__lt=doc.pk)
               .prefetch_related("fields"))
    for other in earlier:
        od = other.data()
        if (norm_ref(od.get("credit_note_number")) == num and vendor_key(od.get("vendor_name")) == vk
                and _dec(od.get("total_amount")) == amount):
            yield IssueSpec("duplicate_credit_note", ERROR,
                            f"{doc.original_filename}: same vendor, credit note {data.get('credit_note_number')} and "
                            f"amount as {other.original_filename}", doc, {"duplicate_of": other.pk},
                            currency=data.get("currency") or "")
            return
