"""The facts a dispute email rests on, phrased for the vendor.

Internal issue messages are written for our reviewers ("lines add up to ..."). Vendors get one plain
sentence per problem with the amounts spelled out. Other apps can add wording for their own issue
codes with `register_explanation(code, fn)`; `fn(issue, invoice_data)` returns a sentence.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from apps.documents.models import Document
from apps.shipments.labels import issue_title
from apps.shipments.models import Shipment, ValidationIssue

# Checks about our own paperwork, not about what the vendor billed.
NOT_DISPUTABLE = {"low_confidence", "fuzzy_match", "missing_bl", "missing_commercial_invoice"}
QUOTE_KEYS = ("quote_reference", "quote_ref", "quote_number", "quote_id", "quote")

FIELD_WORDS = {
    "vendor_name": "your company name", "invoice_number": "invoice number", "total_amount": "invoice total",
    "invoice_date": "invoice date", "currency": "currency", "bl_number": "bill of lading number",
    "container_numbers": "container numbers", "po_numbers": "our purchase order number",
}


def money(amount, currency: str = "") -> str:
    if amount in (None, ""):
        return ""
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return str(amount)
    return f"{currency} {value:,.2f}".strip()


def dec(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01")) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _strip_filename(message: str) -> str:
    head, sep, tail = message.partition(": ")
    return tail if sep and head.lower().endswith(".pdf") else message


def _total_mismatch(issue: ValidationIssue, inv: dict) -> str:
    cur = issue.currency or inv.get("currency") or ""
    line_sum, total = dec(issue.data.get("line_sum")), dec(issue.data.get("total"))
    if line_sum is None or total is None:
        return _strip_filename(issue.message)
    if total > line_sum:
        return (f"The charges listed on the invoice add up to {money(line_sum, cur)}, but the invoice total is "
                f"{money(total, cur)}. That is {money(total - line_sum, cur)} more than the charges listed.")
    return (f"The charges listed on the invoice add up to {money(line_sum, cur)}, but the invoice total is "
            f"{money(total, cur)}. Please confirm which amount is correct.")


def _duplicate(issue: ValidationIssue, inv: dict) -> str:
    other = Document.objects.filter(pk=issue.data.get("duplicate_of"), organization=issue.organization).first()
    number = (other.field("invoice_number") if other else None) or inv.get("invoice_number") or ""
    when = other.field("invoice_date") if other else None
    earlier = f"invoice {number}" + (f" dated {when}" if when else "")
    amount = money(inv.get("total_amount"), issue.currency or inv.get("currency") or "")
    return (f"We already received {earlier}" + (f" for {amount}" if amount else "") + ". This copy bills the same "
            "charges again, and they can only be paid once.")


def _outlier(issue: ValidationIssue, inv: dict) -> str:
    cur = issue.currency or inv.get("currency") or ""
    per_box, typical = dec(issue.data.get("per_container")), dec(issue.data.get("typical"))
    if per_box is None or typical is None:
        return _strip_filename(issue.message)
    return (f"The charges come to {money(per_box, cur)} per container, while you usually bill us about "
            f"{money(typical, cur)} per container for this service.")


def _not_on_bl(issue: ValidationIssue, inv: dict) -> str:
    container = issue.data.get("key") or ""
    bl = inv.get("_bl_number") or ""
    where = f" ({bl})" if bl else ""
    return (f"Container {container} is billed on this invoice, but it is not on the bill of lading for this "
            f"shipment{where}.")


def _invalid_container(issue: ValidationIssue, inv: dict) -> str:
    container = issue.data.get("key") or ""
    return (f"Container number {container} on the invoice is not a valid container number, so we can't match it "
            "to our shipment. Please confirm the correct container.")


def _missing_field(issue: ValidationIssue, inv: dict) -> str:
    words = [FIELD_WORDS.get(f, f.replace("_", " ")) for f in issue.data.get("fields") or []]
    if not words:
        return _strip_filename(issue.message)
    listed = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
    return f"The invoice does not show the {listed}, which we need to process it."


EXPLANATIONS: dict[str, Callable[[ValidationIssue, dict], str]] = {
    "total_mismatch": _total_mismatch,
    "duplicate_invoice": _duplicate,
    "amount_outlier": _outlier,
    "container_not_on_bl": _not_on_bl,
    "invalid_container": _invalid_container,
    "missing_field": _missing_field,
}


# Headings for the vendor; our internal issue titles are written for reviewers.
VENDOR_TITLES = {
    "total_mismatch": "Invoice total is higher than the charges",
    "duplicate_invoice": "Invoice billed twice",
    "amount_outlier": "Charges above your usual rate",
    "container_not_on_bl": "Container not in this shipment",
    "invalid_container": "Container number not valid",
    "missing_field": "Details missing from the invoice",
}


def register_explanation(code: str, fn: Callable[[ValidationIssue, dict], str], title: str = "") -> None:
    """Let another app phrase its own issue code for vendors."""
    EXPLANATIONS[code] = fn
    if title:
        VENDOR_TITLES[code] = title


def vendor_title(issue: ValidationIssue) -> str:
    if issue.code == "total_mismatch" and (dec(issue.data.get("total")) or 0) <= (dec(issue.data.get("line_sum")) or 0):
        return "Invoice total doesn't match the charges"
    return VENDOR_TITLES.get(issue.code) or issue_title(issue.code)


def explain(issue: ValidationIssue, inv: dict) -> str:
    fn = EXPLANATIONS.get(issue.code)
    try:
        text = fn(issue, inv) if fn else _strip_filename(issue.message)
    except Exception:  # never let wording break drafting
        text = _strip_filename(issue.message)
    return text.strip()


def quote_reference(issue: ValidationIssue) -> str:
    for key in QUOTE_KEYS:
        value = (issue.data or {}).get(key)
        if value not in (None, "", [], {}):
            return str(value)
    return ""


def is_disputable(issue: ValidationIssue) -> bool:
    return bool(issue.code not in NOT_DISPUTABLE and issue.document_id and issue.document.is_payable)


@dataclass
class InvoiceGroup:
    """One payable invoice on a shipment and the issues that could be raised with its vendor."""

    invoice: Document
    vendor_name: str
    invoice_number: str
    currency: str
    total: Decimal | None
    issues: list[ValidationIssue] = field(default_factory=list)
    disputes: list = field(default_factory=list)

    @property
    def open_dispute(self):
        return next((d for d in self.disputes if d.is_open), None)


def disputable_groups(shipment: Shipment) -> list[InvoiceGroup]:
    """Payable invoices on the shipment with issues worth raising with the vendor, newest disputes first."""
    from .models import Dispute

    issues = (shipment.issues.select_related("document").prefetch_related("document__fields")
              .order_by("resolved", "-amount_at_risk", "id"))
    groups: dict[int, InvoiceGroup] = {}
    for issue in issues:
        if not is_disputable(issue):
            continue
        doc = issue.document
        if doc.pk not in groups:
            data = doc.data()
            groups[doc.pk] = InvoiceGroup(
                invoice=doc, vendor_name=data.get("vendor_name") or "", invoice_number=str(data.get("invoice_number") or ""),
                currency=(data.get("currency") or shipment.organization.home_currency).upper(),
                total=dec(data.get("total_amount")))
        groups[doc.pk].issues.append(issue)
    if groups:
        for d in Dispute.objects.filter(invoice_id__in=groups).order_by("-created_at"):
            groups[d.invoice_id].disputes.append(d)
    return list(groups.values())


def invoice_facts(invoice: Document, shipment: Shipment | None) -> dict:
    data = invoice.data()
    data["_bl_number"] = (shipment.bl_number if shipment else "") or data.get("bl_number") or ""
    return data


def item_snapshot(issue: ValidationIssue, inv: dict) -> dict:
    return {
        "issue": issue, "fingerprint": issue.fingerprint, "code": issue.code, "title": vendor_title(issue),
        "explanation": explain(issue, inv), "amount": issue.amount_at_risk,
        "currency": issue.currency or (inv.get("currency") or ""),
        "data": {"quote": quote_reference(issue), "severity": issue.severity, "message": issue.message},
    }
