"""Alerts the accounting app adds to apps.notifications (registered from AccountingConfig.ready)."""
from __future__ import annotations

from django.urls import reverse

XERO_RECONNECT = "xero.needs_reconnect"
BILL_VOIDED = "accounting.bill_voided"


def register() -> None:
    from apps.notifications import events

    events.register_event(
        XERO_RECONNECT, "Xero needs reconnecting", "Xero stopped accepting the connection; nothing can post.",
        audit_actions=["xero.needs_reconnect"], builder=_xero_reconnect, default_for=("slack", "teams", "email"))
    events.register_event(
        BILL_VOIDED, "Bill voided or deleted in accounting",
        "A posted bill or vendor credit was voided or deleted in QuickBooks or Xero, so it won't be paid as approved.",
        audit_actions=["bill.voided_in_accounting"], builder=_bill_voided, default_for=("slack", "teams", "email"))


def _xero_reconnect(e):
    from apps.notifications.events import Message, absolute

    org = e.organization
    facts = [["Organization", org.name]]
    company = (e.data or {}).get("company")
    if company:
        facts.append(["Xero organisation", str(company)])
    return Message(XERO_RECONNECT, "Xero needs to be connected again",
                   "Xero no longer accepts the saved connection, so approved bills can't be posted and payments "
                   "aren't checked. An admin can connect again from Settings, Accounting; vendor rules and accounts "
                   "are kept.", facts, absolute(reverse("accounting:settings", args=[org.pk])), "Reconnect Xero",
                   "error", org.name)


def _bill_voided(e):
    from apps.documents.models import Document
    from apps.notifications.events import Message, _money, absolute

    data = e.data or {}
    doc = (Document.objects.filter(pk=e.object_id, organization=e.organization).select_related("match__shipment")
           .first())
    if doc is None:
        return None
    shipment = doc.match.shipment if hasattr(doc, "match") else None
    system, what = str(data.get("system") or "the accounting system"), str(data.get("what") or "bill")
    status = str(data.get("status") or "voided")
    facts = [["Invoice", doc.original_filename]]
    if shipment:
        facts.insert(0, ["Shipment", shipment.reference])
    if data.get("number"):
        facts.append([f"{what.capitalize()} in {system}", str(data["number"])])
    vendor = doc.field("vendor_name")
    if vendor:
        facts.append(["Vendor", str(vendor)])
    if doc.field("total_amount") not in (None, ""):
        facts.append(["Amount", _money(doc.field("total_amount"), doc.field("currency") or e.organization.home_currency)])
    url = absolute(reverse("review:shipment", args=[shipment.pk]) + f"#doc-{doc.pk}") if shipment else \
        absolute(reverse("review:document", args=[doc.pk]))
    return Message(BILL_VOIDED, f"A {what} was {status} in {system}",
                   f"The approved {what} is no longer open in {system}, so it won't be paid as approved. If that was "
                   "a mistake, re-enter it there; if the vendor sent a corrected invoice, process that one in ShipMatch.",
                   facts, url, "Open the shipment", "warning", e.organization.name)
