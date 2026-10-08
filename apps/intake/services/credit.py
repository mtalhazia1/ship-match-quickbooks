"""Credit notes: reading them, finding the invoice they reduce, and checking them.

A credit note's amounts are stored as positive numbers: "credit of 500.00", whether it is printed as
500.00, -500.00, (500.00) or 500.00 CR. Totals of a shipment subtract it; QuickBooks receives it as a
VendorCredit (see apps/accounting/services/posting.py).
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from apps.documents.services.extract_rules import (
    GUESS_CONF,
    LABEL_CONF,
    PATTERN_CONF,
    RuleResult,
    extract_rules,
)
from apps.documents.services.normalize import norm_ref, parse_date, parse_money

CREDIT_TITLE = re.compile(r"\b(credit\s+note|credit\s+memo(?:randum)?|credit\s+advice)\b", re.I)
_NUM = r"(?P<v>[A-Z0-9][A-Z0-9/-]{2,})"
CREDIT_NO = re.compile(r"(?:credit\s*(?:note|memo)\s*(?:no\.?|number|#|ref(?:erence)?)|\bcn\s*(?:no\.?|#))"
                       r"\s*[:#]?\s*" + _NUM, re.I)
ORIGINAL_NO = re.compile(r"(?:original\s+invoice|against\s+invoice|applies\s+to\s+invoice|credited\s+invoice|"
                         r"invoice\s+credited|reference\s+invoice|ref\.?\s+invoice|for\s+invoice|"
                         r"relat(?:es|ing)\s+to\s+invoice|corrects\s+invoice|cancels\s+invoice)"
                         r"\s*(?:no\.?|number|#|ref\.?)?\s*[:#]?\s*" + _NUM, re.I)
INVOICE_NO = re.compile(r"\binvoice\s*(?:no\.?|number|#)\s*[:#]?\s*" + _NUM, re.I)
DOC_NO = re.compile(r"^(?:document|doc|number|no)\.?\s*(?:no\.?|number|#)?\s*[:#]\s*" + _NUM, re.I)
DATE_LABEL = re.compile(r"^(?:credit\s*(?:note|memo)\s*date|date of issue|issue date|date|dated|issued)\s*:\s*(?P<v>.+)$",
                        re.I)
SIGNED = r"\(?\s*-?\s*(?:[A-Z]{3}\s*)?-?\s*[\d,]+\.\d{2}\s*\)?(?:\s*CR\b)?"
TOTAL_LINE = re.compile(r"\b(total\s+credit|credit\s+total|amount\s+credited|credit\s+amount|total\s+amount|"
                        r"grand\s+total|total\s+due|total)\b[^:\n]*:\s*(?P<v>" + SIGNED + ")", re.I)


def positive(raw) -> Decimal | None:
    """-500.00, (500.00), 500.00 CR and USD -500.00 all mean a credit of 500.00."""
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        return abs(Decimal(str(raw))).quantize(Decimal("0.01"))
    value = parse_money(re.sub(r"[()A-Za-z\s]", "", str(raw)))
    return abs(value) if value is not None else None


def credit_note_rules(text: str) -> RuleResult:
    """Rule-based reading of a credit note (or a spreadsheet's label/value lines)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    cleaned = "\n".join(CREDIT_TITLE.sub("", ln).strip() or ln for ln in lines)
    base = extract_rules("freight_invoice", cleaned)  # vendor, references, currency
    r = RuleResult()
    for name in ("vendor_name", "currency", "bl_number", "container_numbers", "po_numbers"):
        if name in base.values:
            r.put(name, base.values[name], base.confidence[name])

    original, original_span = None, None
    for ln in lines:
        m = ORIGINAL_NO.search(ln)
        if m:
            original, original_span = m.group("v"), (ln, m.span())
            break
    number = next((m.group("v") for m in (CREDIT_NO.search(ln) for ln in lines) if m), None)
    plain_invoice = None
    for ln in lines:
        for m in INVOICE_NO.finditer(ln):
            if original_span and original_span[0] == ln and m.start() >= original_span[1][0] and \
                    m.end() <= original_span[1][1]:
                continue
            if m.group("v") not in (original, number):
                plain_invoice = plain_invoice or m.group("v")
    if number:
        r.put("credit_note_number", number, LABEL_CONF)
    elif plain_invoice:  # a credit note printed on an invoice template: its own number, to be confirmed
        r.put("credit_note_number", plain_invoice, GUESS_CONF)
        plain_invoice = None
    else:
        doc_no = next((m.group("v") for m in (DOC_NO.search(ln) for ln in lines) if m), None)
        r.put("credit_note_number", doc_no, PATTERN_CONF)
    if original:
        r.put("original_invoice_number", original, LABEL_CONF)
    elif plain_invoice:
        r.put("original_invoice_number", plain_invoice, PATTERN_CONF)

    for ln in lines:
        m = DATE_LABEL.match(ln)
        if m and parse_date(m.group("v")):
            r.put("invoice_date", parse_date(m.group("v")).isoformat(), LABEL_CONF)
            break
    total = None
    for ln in lines:
        m = TOTAL_LINE.search(ln)
        if m and "subtotal" not in ln.lower():
            total = positive(m.group("v"))
    if total is not None:
        r.put("total_amount", str(total), LABEL_CONF)
    r.put("line_items", _credit_lines(lines), LABEL_CONF)
    return r


def _credit_lines(lines: list[str]) -> list[dict]:
    items, inside = [], False
    goods = re.compile(r"^(?P<d>.+?)\s+(?P<q>-?\d[\d,]*)\s+(?P<u>\(?-?[\d,]+\.\d{2,4}\)?)\s+(?P<a>" + SIGNED + ")$", re.I)
    charge = re.compile(r"^(?P<d>[A-Za-z(][^:]*?)\s+(?P<a>" + SIGNED + ")$", re.I)
    for line in lines:
        low = line.lower()
        if not inside:
            inside = "description" in low and ("amount" in low or "qty" in low or "credit" in low)
            continue
        if re.search(r"\b(total|amount due|subtotal|amount credited)\b", low):
            break
        m = goods.match(line)
        if m:
            items.append({"description": m["d"].strip(), "quantity": m["q"].replace(",", "").lstrip("-"),
                          "unit_price": str(positive(m["u"])), "amount": str(positive(m["a"]))})
            continue
        m = charge.match(line)
        if m:
            items.append({"description": m["d"].strip(), "amount": str(positive(m["a"]))})
    return items


def normalize_values(doc_type: str, data: dict) -> dict:
    """After an AI reading: a credit note's amounts are positive, however they were printed."""
    if doc_type != "credit_note":
        return data
    data = dict(data)
    if data.get("total_amount") not in (None, ""):
        data["total_amount"] = str(positive(data["total_amount"]))
    items = []
    for item in data.get("line_items") or []:
        item = dict(item)
        for key in ("amount", "unit_price"):
            if item.get(key) not in (None, ""):
                item[key] = str(positive(item[key]))
        if item.get("quantity") not in (None, ""):
            try:
                item["quantity"] = str(abs(Decimal(str(item["quantity"]))))
            except InvalidOperation:
                pass
        items.append(item)
    if "line_items" in data:
        data["line_items"] = items
    return data


# --------------------------------------------------------------------------- the invoice a credit note reduces


def find_original_invoice(doc, data: dict | None = None):
    """The received invoice this credit note reduces: same invoice number (ignoring spaces and dashes)
    and, when both are known, the same vendor. None if it hasn't been received."""
    from apps.accounting.models import vendor_key
    from apps.documents.models import Document, ExtractedField

    data = data if data is not None else doc.data()
    number = norm_ref(data.get("original_invoice_number"))
    if not number:
        return None
    vendor = vendor_key(data.get("vendor_name"))
    digits = max(re.findall(r"\d+", number), key=len, default="")
    qs = (ExtractedField.objects.filter(document__organization=doc.organization, name="invoice_number",
                                        document__doc_type__in=Document.PAYABLE_TYPES)
          .exclude(document=doc).select_related("document").order_by("document__received_at", "document_id"))
    if digits:
        qs = qs.filter(value__icontains=digits)
    for f in qs:
        if norm_ref(f.value) != number:
            continue
        other_vendor = vendor_key(f.document.field("vendor_name"))
        if vendor and other_vendor and other_vendor != vendor:
            continue
        return f.document
    return None


def original_invoice_shipment(doc):
    """(shipment, reason) when a credit note's original invoice is in a shipment still open for changes."""
    from apps.shipments.services.matching import ACTIVE

    if not doc.is_credit:
        return None
    invoice = find_original_invoice(doc)
    link = getattr(invoice, "match", None) if invoice is not None else None
    if link is None or link.shipment.status not in ACTIVE:
        return None
    return link.shipment, f"credits invoice {invoice.field('invoice_number')} ({invoice.original_filename})"
