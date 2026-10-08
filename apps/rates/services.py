"""Small operations shared by the Rates views, the CSV import and seed_rates."""
from __future__ import annotations

from apps.accounting.models import vendor_key
from apps.documents.models import Document
from apps.shipments.models import Shipment
from apps.shipments.services.validation import validate_shipment

from . import ledger
from .models import CaughtCharge, Quote

OPEN_STATUSES = [Shipment.Status.OPEN, Shipment.Status.NEEDS_REVIEW, Shipment.Status.READY]


def recheck(org, vendor_keys: set[str] | None = None) -> int:
    """Validate open shipments again after rates changed. Only shipments with a freight invoice
    from one of `vendor_keys` (all, when None). Approved, posted and rejected ones are left alone.
    Catches that disappear because of this are recorded as withdrawn, not as money saved."""
    count = 0
    with ledger.clearing_because(CaughtCharge.Cleared.RATES):
        for s in Shipment.objects.filter(organization=org, status__in=OPEN_STATUSES).order_by("id"):
            if vendor_keys is not None:
                invoices = s.documents.filter(doc_type=Document.DocType.FREIGHT_INVOICE).prefetch_related("fields")
                if not any(vendor_key(d.data().get("vendor_name")) in vendor_keys for d in invoices):
                    continue
            validate_shipment(s)
            count += 1
    return count


def snapshot(q: Quote) -> dict:
    """What a quote says, for the audit log."""
    return {
        "vendor": q.vendor_name, "reference": q.reference, "origin": q.origin, "destination": q.destination,
        "equipment": q.equipment, "valid_from": q.valid_from, "valid_to": q.valid_to, "currency": q.currency,
        "all_in": q.all_in, "archived": q.archived,
        "charges": [f"{c.code} {c.amount} {c.basis}" for c in q.charges.all()],
    }


def changes(before: dict, after: dict) -> dict:
    return {k: [before.get(k), after.get(k)] for k in after if str(before.get(k)) != str(after.get(k))}
