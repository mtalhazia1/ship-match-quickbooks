"""The charges that make up a shipment's landed cost.

Charges come from charge sources. A source is a function `fn(shipment)` that returns (or yields) the
charges it knows about, each a dict or a `Charge`:

    {"code": "customs_duty", "amount": "1240.50", "currency": "USD",
     "basis": "value",                 # optional hint: value | quantity | weight | volume
     "description": "Duty 7326.90",    # optional
     "source": "Customs entry 123",    # optional, shown to the reviewer
     "category": "duty"}               # optional; otherwise derived from the code

Other apps add sources from their AppConfig.ready() with `register_charge_source(fn)` (a customs app
adds duties and taxes this way). A source may also yield `Note("...")` to explain what it left out.
The built-in source reads the shipment's freight invoices and credit notes, and its share of invoices
shared with other shipments (see allocation.py).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from apps.documents.models import Document

from ..models import Category, Method
from .rounding import CENT, from_cents, to_cents

log = logging.getLogger(__name__)

CATEGORY_CODES = {
    Category.FREIGHT: {"ocean_freight", "air_freight", "freight", "baf", "caf", "trucking", "chassis",
                       "fuel_surcharge", "congestion", "overweight", "hazmat", "pre_pull", "chassis_split",
                       "redelivery", "waiting_time"},
    Category.HANDLING: {"thc_origin", "thc_destination", "handling", "storage", "demurrage", "detention", "per_diem",
                        "exam"},
    Category.INSURANCE: {"insurance", "cargo_insurance"},
    Category.DUTY: {"duty", "duties", "customs_duty", "import_duty", "tax", "taxes", "vat", "import_vat", "gst",
                    "excise", "mpf", "hmf", "tariff", "anti_dumping", "countervailing", "section_301"},
    Category.OTHER: {"documentation", "bl_fee", "security_filing", "customs_clearance", "admin_fee", "other"},
}
_BY_CODE = {code: cat for cat, codes in CATEGORY_CODES.items() for code in codes}


def category_for(code: str, given: str | None = None) -> str:
    if given in Category.values:
        return given
    code = (code or "").lower()
    if code in _BY_CODE:
        return _BY_CODE[code]
    if code.startswith(("duty", "duties", "tax", "vat", "gst", "tariff")):
        return Category.DUTY
    return Category.OTHER


@dataclass
class Charge:
    code: str
    amount: Decimal
    currency: str
    description: str = ""
    source: str = ""
    category: str = ""
    basis: str | None = None
    document_id: int | None = None
    shared: bool = False

    def __post_init__(self):
        self.category = category_for(self.code, self.category)
        if self.basis not in Method.values:
            self.basis = None


@dataclass
class Note:
    text: str
    level: str = "info"   # info | warning


def as_charge(item, default_currency: str) -> Charge | None:
    if isinstance(item, Charge):
        return item
    if not isinstance(item, dict):
        return None
    try:
        amount = Decimal(str(item.get("amount"))).quantize(CENT)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return Charge(code=str(item.get("code") or "other")[:40], amount=amount,
                  currency=str(item.get("currency") or default_currency).upper()[:3],
                  description=str(item.get("description") or "")[:300], source=str(item.get("source") or "")[:120],
                  category=item.get("category") or "", basis=item.get("basis"),
                  document_id=item.get("document_id"))


# --------------------------------------------------------------------------- registry

CHARGE_SOURCES: list[Callable] = []


def register_charge_source(fn: Callable) -> None:
    if fn not in CHARGE_SOURCES:
        CHARGE_SOURCES.append(fn)


def collect(shipment) -> tuple[list[Charge], list[Note]]:
    """Every charge on the shipment from every source. A failing source is skipped with a note."""
    charges, notes = [], []
    home = shipment.organization.home_currency
    for source in [invoice_charges, *CHARGE_SOURCES]:
        try:
            for item in source(shipment) or []:
                if isinstance(item, Note):
                    notes.append(item)
                    continue
                charge = as_charge(item, home)
                if charge is None:
                    notes.append(Note(f"A charge from {_name(source)} has no usable amount and was left out.",
                                      "warning"))
                elif charge.amount:
                    charges.append(charge)
        except Exception:  # one broken source must not hide the others
            log.exception("Landed cost charge source %s failed for shipment %s", _name(source), shipment.pk)
            notes.append(Note(f"Charges from {_name(source)} couldn't be read, so they are not included.", "warning"))
    return charges, notes


def _name(fn) -> str:
    return getattr(fn, "label", None) or getattr(fn, "__name__", "a charge source").replace("_", " ")


# --------------------------------------------------------------------------- invoice lines


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(CENT) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def document_lines(doc: Document, data: dict | None = None) -> list[tuple[str, int]]:
    """(description, cents) for every line of an invoice or credit note, plus an adjustment line when the
    lines don't add up to the printed total, so the lines always add up to the amount on the document.
    Credit notes are returned as printed (positive); the caller makes them negative."""
    data = data if data is not None else doc.data()
    total = _dec(data.get("total_amount"))
    lines = []
    for item in data.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        amount = _dec(item.get("amount"))
        if amount is None:
            continue
        lines.append((str(item.get("description") or "Charge")[:300], to_cents(amount)))
    if doc.is_credit:
        total = abs(total) if total is not None else None
        lines = [(d, abs(c)) for d, c in lines]
    if not lines:
        if total is None:
            return []
        number = data.get("invoice_number") or data.get("credit_note_number") or ""
        return [(f"{doc.get_doc_type_display()} {number}".strip(), to_cents(total))]
    if total is not None:
        difference = to_cents(total) - sum(c for _, c in lines)
        if difference:
            lines.append(("Difference to the printed total", difference))
    return lines


def invoice_number(doc: Document, data: dict | None = None) -> str:
    data = data if data is not None else doc.data()
    return str(data.get("invoice_number") or data.get("credit_note_number") or doc.original_filename)


def invoice_charges(shipment):
    """Built-in source: freight invoices and credit notes in the shipment, and its shares of shared invoices.
    An invoice flagged as a possible duplicate (not resolved) is left out until someone decides."""
    from apps.rates.charges import classify_many
    from apps.shipments.models import ValidationIssue

    from . import allocation

    org = shipment.organization
    docs = [d for d in shipment.documents.prefetch_related("fields")
            if d.doc_type in (Document.DocType.FREIGHT_INVOICE, Document.DocType.CREDIT_NOTE)]
    duplicates = set(ValidationIssue.objects.filter(shipment=shipment, resolved=False,
                                                    code__in=["duplicate_invoice", "duplicate_credit_note"])
                     .values_list("document_id", flat=True))
    rows: list[tuple[str, int, str, Document, bool]] = []   # description, cents, currency, doc, shared
    for d in docs:
        data = d.data()
        cur = (data.get("currency") or org.home_currency).upper()
        if d.pk in duplicates:
            yield Note(f"{d.get_doc_type_display()} {invoice_number(d, data)} may be a duplicate, so it is left out "
                       "until someone accepts or removes it.", "warning")
            continue
        parts = allocation.parts_for(d, shipment)
        if parts is not None:
            rows += [(desc, cents, cur, d, True) for desc, cents in parts]
            continue
        sign = -1 if d.is_credit else 1
        lines = document_lines(d, data)
        if not lines:
            yield Note(f"{d.get_doc_type_display()} {invoice_number(d, data)} has no amount, so it is left out.",
                       "warning")
        rows += [(desc, sign * cents, cur, d, False) for desc, cents in lines]
    # Shares of invoices that are matched to another shipment.
    for doc in allocation.shared_with(shipment, exclude=[d.pk for d in docs]):
        parts = allocation.parts_for(doc, shipment)
        if parts:
            cur = (doc.field("currency") or org.home_currency).upper()
            rows += [(desc, cents, cur, doc, True) for desc, cents in parts]
    codes = classify_many([r[0] for r in rows], org=org, use_ai=False)
    for desc, cents, cur, doc, shared in rows:
        if not cents:
            continue
        label = f"{doc.get_doc_type_display()} {invoice_number(doc)}"
        yield Charge(code=codes.get(desc, "other"), amount=from_cents(cents), currency=cur, description=desc,
                     source=label + (" (this shipment's share)" if shared else ""), document_id=doc.pk, shared=shared)


invoice_charges.label = "freight invoices"
