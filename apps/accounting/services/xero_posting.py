"""Xero side of posting: contacts, currency checks and the bill / credit note payloads.

An invoice becomes an ACCPAY invoice (a bill), a credit note an ACCPAYCREDIT credit note. Lines are posted
as printed with LineAmountTypes "NoTax", so the bill in Xero equals the approved total (ShipMatch doesn't
read tax separately; adjust tax in Xero if the organisation reclaims it). The supplier's invoice number goes
in InvoiceNumber, which Xero shows as "Reference" on bills.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.urls import reverse

from apps.accounting.models import PostedBill, VendorMapping, XeroConnection, vendor_key
from apps.core.utils import audit
from apps.documents.models import Document
from apps.shipments.models import Shipment

from .posting import PostingBlocked, bill_currency, bill_lines
from .providers import Created
from .xero import XeroClient, XeroValidationError

log = logging.getLogger(__name__)


def check_currency(doc: Document, conn: XeroConnection) -> None:
    currency = bill_currency(doc, conn)
    home = (conn.home_currency or doc.organization.home_currency).upper()
    if currency != home and currency not in {c.upper() for c in conn.currencies or []}:
        raise PostingBlocked(f"The invoice is in {currency}, but {currency} isn't added in Xero (base currency {home}). "
                             f"Add {currency} in Xero under Settings > Currencies (it needs a plan with multi-currency), "
                             "then post again, or enter the bill in Xero by hand.")


def resolve_contact(client: XeroClient, doc: Document, conn: XeroConnection) -> VendorMapping:
    """The vendor's Xero contact: remembered on the vendor rule, else found by name (archived ones included),
    else created with the invoice's currency."""
    name = (doc.field("vendor_name") or "").strip()
    if not name:
        raise PostingBlocked("The invoice has no vendor name. Add it in the review screen.")
    mapping, _ = VendorMapping.objects.get_or_create(
        organization=doc.organization, vendor_key=vendor_key(name), defaults={"display_name": name[:200]})
    if mapping.xero_contact_id:
        return mapping
    currency = bill_currency(doc, conn)
    contact = client.find_contact(mapping.display_name)
    if contact is not None and contact.get("ContactStatus") == "ARCHIVED" and contact.get("MergedToContactID"):
        contact = client.get_contact(contact["MergedToContactID"]) or contact   # merged in Xero: use the survivor
    if contact is None:
        foreign = currency != (conn.home_currency or currency)
        try:
            contact = client.create_contact(mapping.display_name, currency if foreign else None,
                                            key=f"sm-contact-{doc.organization_id}-{mapping.pk}")
        except XeroValidationError as e:
            # Created meanwhile by someone else (names are unique among active contacts): use that one.
            contact = client.find_contact(mapping.display_name)
            if contact is None:
                raise PostingBlocked(f"Xero wouldn't create the contact '{mapping.display_name}'. {e}") from e
        else:
            audit(doc.organization, "xero.contact_created", mapping, vendor=mapping.display_name,
                  contact_id=contact.get("ContactID", ""))
    status = contact.get("ContactStatus")
    if status == "ARCHIVED":
        raise PostingBlocked(f"The contact '{mapping.display_name}' is archived in Xero. Restore it in Xero "
                             "(Contacts, Archived), then post again.")
    if status == "GDPRREQUEST":
        raise PostingBlocked(f"The contact '{mapping.display_name}' was erased in Xero at the vendor's request. "
                             "Create a new contact for the vendor in Xero, then post again.")
    if not contact.get("ContactID"):
        raise PostingBlocked(f"Xero didn't return a contact for '{mapping.display_name}'. Post again to retry.")
    mapping.xero_contact_id = str(contact["ContactID"])[:64]
    mapping.save(update_fields=["xero_contact_id", "updated_at"])
    return mapping


def _line_items(lines, account_code: str) -> list[dict]:
    out = []
    for line in lines:
        item = {"Description": line.text, "Quantity": 1, "UnitAmount": float(line.amount), "AccountCode": account_code}
        tracking = [{"Name": str(n)[:100], "Option": str(o)[:100]} for n, o in (line.tracking or ())[:2] if n and o]
        if tracking:
            item["Tracking"] = tracking
        out.append(item)
    return out


def _source_url(shipment: Shipment) -> str | None:
    """Xero shows a "Go to ShipMatch" link on the bill when it has a source URL (https only)."""
    base = getattr(settings, "SITE_URL", "")
    return f"{base}{reverse('review:shipment', args=[shipment.pk])}" if base.startswith("https://") else None


def build_invoice_payload(doc: Document, shipment: Shipment, contact_id: str, account_code: str,
                          conn: XeroConnection) -> dict:
    """An ACCPAY invoice (bill): one line per invoice line, adjusted to the printed total."""
    data = doc.data()
    lines = bill_lines(doc, "invoice", f"Invoice {data.get('invoice_number') or ''}".strip())
    payload = {
        "Type": "ACCPAY",
        "Contact": {"ContactID": contact_id},
        "LineAmountTypes": "NoTax",
        "Status": conn.bill_status or XeroConnection.BillStatus.DRAFT,
        "CurrencyCode": bill_currency(doc, conn),
        "LineItems": _line_items(lines, account_code),
    }
    if data.get("invoice_number"):
        payload["InvoiceNumber"] = str(data["invoice_number"])[:255]
    if data.get("invoice_date"):
        payload["Date"] = str(data["invoice_date"])[:10]
    if data.get("due_date"):
        payload["DueDate"] = str(data["due_date"])[:10]
    url = _source_url(shipment)
    if url:
        payload["Url"] = url
    return payload


def build_credit_note_payload(doc: Document, shipment: Shipment, contact_id: str, account_code: str,
                              conn: XeroConnection) -> dict:
    """An ACCPAYCREDIT credit note: positive amounts on the same expense account; it lowers what is owed."""
    data = doc.data()
    number = data.get("credit_note_number") or ""
    lines = bill_lines(doc, "credit note", f"Credit note {number}".strip())
    if any(line.amount < 0 for line in lines) or sum(line.amount for line in lines) <= 0:
        raise PostingBlocked("The credit note's amounts don't add up to a positive credit. Check the total and "
                             "the lines in the review screen.")
    payload = {
        "Type": "ACCPAYCREDIT",
        "Contact": {"ContactID": contact_id},
        "LineAmountTypes": "NoTax",
        "Status": conn.bill_status or XeroConnection.BillStatus.DRAFT,
        "CurrencyCode": bill_currency(doc, conn),
        "LineItems": _line_items(lines, account_code),
    }
    if number:
        payload["CreditNoteNumber"] = str(number)[:255]
    if data.get("invoice_date"):
        payload["Date"] = str(data["invoice_date"])[:10]
    return payload


def _note(doc: Document, shipment: Shipment) -> str:
    data = doc.data()
    original = data.get("original_invoice_number") if doc.is_credit else ""
    return (f"Posted from ShipMatch {shipment.reference}, B/L {shipment.bl_number or 'none'}"
            + (f", credits invoice {original}" if original else "") + f", file {doc.original_filename}")


def _same_total(found: dict | None, payload: dict) -> dict | None:
    """Reuse a bill found in Xero only if it is for the same amount (otherwise it is a different bill)."""
    if not found:
        return None
    ours = round(sum(item["UnitAmount"] * item.get("Quantity", 1) for item in payload["LineItems"]), 2)
    try:
        return found if abs(float(found.get("Total")) - ours) < 0.005 else None
    except (TypeError, ValueError):
        return None


def create_document(client: XeroClient, doc: Document, shipment: Shipment, contact_id: str, account_code: str,
                    conn: XeroConnection, pb: PostedBill) -> Created:
    """Create the bill or credit note once. After a failed attempt (the Idempotency-Key only lasts so long), look
    for a bill this contact already has with the same number before creating another."""
    if doc.is_credit:
        payload = build_credit_note_payload(doc, shipment, contact_id, account_code, conn)
        number = payload.get("CreditNoteNumber", "")
        existing = _same_total(client.find_credit_note(contact_id, number), payload) if pb.error and number else None
        made = existing or client.create_credit_note(payload, pb.request_id)
        entity_id, endpoint = str(made.get("CreditNoteID") or ""), "CreditNotes"
        number = str(made.get("CreditNoteNumber") or number)
    else:
        payload = build_invoice_payload(doc, shipment, contact_id, account_code, conn)
        number = payload.get("InvoiceNumber", "")
        existing = _same_total(client.find_bill(contact_id, number), payload) if pb.error and number else None
        made = existing or client.create_invoice(payload, pb.request_id)
        entity_id, endpoint = str(made.get("InvoiceID") or ""), "Invoices"
        number = str(made.get("InvoiceNumber") or number)
    if not entity_id:
        raise PostingBlocked("Xero didn't return the new bill's ID. Post again: the same request is never created twice.")
    if existing is None:
        client.add_note(endpoint, entity_id, _note(doc, shipment))
    else:
        log.info("Reusing Xero %s %s for document %s (found after an earlier failed attempt)", endpoint, entity_id, doc.pk)
    return Created(entity_id, number, made)
