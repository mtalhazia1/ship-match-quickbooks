"""Duty and customs fees as a charge source for landed cost.

The landed cost feature registers this with `register_charge_source(duty_charges)` (apps.landed). Each charge is
what the customs entries in the shipment state was paid (not a recalculation; the checks flag the difference):

    {"code": "customs_duty", "amount": Decimal("2287.80"), "currency": "USD", "basis_hint": "value",
     "label": "Customs duty", "entry_number": "HLB-2604117-3", "document_id": 41}

basis_hint "value" means the charge is ad valorem (spread it over the goods by their value); "shipment" means one
amount for the whole shipment. When the same entry number was received twice (a resend or a corrected entry), only
the latest copy counts, so duty is never counted twice.
"""
from __future__ import annotations

from decimal import Decimal

from apps.documents.models import Document

from .entry import dec, entry_currency, line_checks, normalize_entry_number

CHARGES = [
    # code, field, label, basis_hint
    ("customs_duty", "total_duty", "Customs duty", "value"),
    ("merchandise_processing_fee", "merchandise_processing_fee", "Merchandise processing fee", "value"),
    ("harbor_maintenance_fee", "harbor_maintenance_fee", "Harbor maintenance fee", "value"),
    ("customs_other_fees", "other_fees", "Other customs fees and taxes", "shipment"),
]


def latest_entries(shipment) -> list[Document]:
    """The shipment's customs entries, one per entry number (the latest received)."""
    by_number: dict[str, Document] = {}
    docs = (Document.objects.filter(match__shipment=shipment, doc_type=Document.DocType.CUSTOMS_ENTRY)
            .prefetch_related("fields").order_by("received_at", "pk"))
    for doc in docs:
        key = normalize_entry_number(doc.field("entry_number")) or f"doc{doc.pk}"
        by_number[key] = doc
    return list(by_number.values())


def duty_charges(shipment) -> list[dict]:
    out = []
    for doc in latest_entries(shipment):
        data = doc.data()
        cur = entry_currency(data) or shipment.organization.home_currency
        for code, field, label, basis in CHARGES:
            amount = dec(data.get(field))
            if amount is None and code == "customs_duty":
                lines = [c.stated for c in line_checks(data) if c.stated is not None]
                amount = sum(lines, Decimal("0.00")) if lines else None
            if amount is None or amount == 0:
                continue
            out.append({"code": code, "amount": amount, "currency": cur, "basis_hint": basis, "label": label,
                        "entry_number": str(data.get("entry_number") or ""), "document_id": doc.pk})
    return out
