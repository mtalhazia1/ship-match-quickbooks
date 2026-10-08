"""Post an approved shipment to the organization's accounting system safely and only once: invoices as bills,
credit notes as vendor credits (which lower what is owed to the vendor).

`post_shipment` works through a provider (apps.accounting.services.providers): QuickBooks or Xero, whichever
the organization connected. The QuickBooks payload builders live here; Xero's are in xero_posting.py.

Bill lines: `register_line_builder(fn)` lets another app decide the lines of a bill or vendor credit, for
example one line per shipment when an invoice covers several. fn(doc) returns a list of lines or None to
leave the document to the next builder (and finally the default: one line per invoice line). A line is a
dict {"description", "amount", "memo"?, "class"?, "tracking"?} or a tuple (description, amount[, memo]).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from apps.accounting.models import PostedBill, QBOConnection, VendorMapping, vendor_key
from apps.core.utils import audit
from apps.documents.models import Document
from apps.shipments.models import Shipment

from .quickbooks import QBOClient, QBOError

log = logging.getLogger(__name__)
CENT = Decimal("0.01")

# QuickBooks error codes worth translating for a reviewer.
FRIENDLY = {
    "6240": "A customer, employee or other vendor in QuickBooks already uses this name.",
    "6140": "QuickBooks already has a bill with this number for this vendor (duplicate bill number).",
    "6000": "QuickBooks rejected the bill. Check the vendor's currency and the expense account.",
    "610": "The vendor or account referenced was deleted or made inactive in QuickBooks.",
}


class PostingBlocked(RuntimeError):
    pass


# --------------------------------------------------------------------------- bill lines


@dataclass
class BillLine:
    """One expense line of a bill or vendor credit, in the document's currency.

    memo: extra words added after the description (e.g. the shipment a share belongs to);
    class_ref: a QuickBooks class Id (ClassRef) for class tracking;
    tracking: Xero tracking as [(category name, option)], at most two categories.
    """

    description: str
    amount: Decimal
    memo: str = ""
    class_ref: str = ""
    tracking: tuple = ()

    @property
    def text(self) -> str:
        text = self.description or "Charge"
        return (f"{text} ({self.memo})" if self.memo else text)[:4000]


LINE_BUILDERS: list[Callable] = []


def register_line_builder(fn: Callable) -> None:
    """Add a bill line builder from an AppConfig.ready(). fn(doc) -> list of lines, or None for the default.

    The lines are posted as given; if they don't add up to the document's printed total, an adjustment line
    makes up the difference (as with the default lines), so the bill always equals what was approved."""
    if fn not in LINE_BUILDERS:
        LINE_BUILDERS.append(fn)


def _as_line(raw) -> BillLine:
    if isinstance(raw, BillLine):
        return raw
    if isinstance(raw, dict):
        amount = _dec(raw.get("amount"))
        tracking = raw.get("tracking") or ()
        if isinstance(tracking, dict):
            tracking = tuple(tracking.items())
        return BillLine(str(raw.get("description") or "Charge"), amount if amount is not None else Decimal("NaN"),
                        str(raw.get("memo") or ""), str(raw.get("class") or raw.get("class_ref") or ""),
                        tuple(tuple(t) for t in tracking))
    description, amount, *rest = raw
    value = _dec(amount)
    return BillLine(str(description or "Charge"), value if value is not None else Decimal("NaN"),
                    str(rest[0]) if rest else "")


def request_id_for(doc: Document) -> str:
    """Deterministic idempotency key (QuickBooks allows up to 50 characters)."""
    return f"sm-{doc.organization_id}-{doc.pk}-{doc.sha256[:12]}"[:50]


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(CENT) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def bill_currency(doc: Document, conn: QBOConnection) -> str:
    home = (conn.home_currency or doc.organization.home_currency).upper()
    return (doc.field("currency") or home).upper()


def resolve_vendor(client: QBOClient, doc: Document, conn: QBOConnection) -> VendorMapping:
    name = (doc.field("vendor_name") or "").strip()
    if not name:
        raise PostingBlocked("The invoice has no vendor name. Add it in the review screen.")
    mapping, _ = VendorMapping.objects.get_or_create(
        organization=doc.organization, vendor_key=vendor_key(name), defaults={"display_name": name[:200]})
    if mapping.qbo_vendor_id:
        return mapping
    currency = bill_currency(doc, conn)
    foreign = conn.multicurrency and conn.home_currency and currency != conn.home_currency
    vendor = client.find_vendor(mapping.display_name)
    if vendor is None:
        try:
            vendor = client.create_vendor(mapping.display_name, currency if foreign else None)
        except QBOError as e:
            if e.code == "6240":
                raise PostingBlocked(
                    f"QuickBooks already uses the name '{mapping.display_name}' for a customer or employee. "
                    "Create the vendor in QuickBooks under a slightly different name, then post again.") from e
            raise
        audit(doc.organization, "qbo.vendor_created", mapping, vendor=mapping.display_name, qbo_vendor_id=vendor["Id"])
    elif vendor.get("Active") is False:
        raise PostingBlocked(f"The vendor '{mapping.display_name}' is inactive in QuickBooks. Make it active and post again.")
    vendor_currency = ((vendor.get("CurrencyRef") or {}).get("value") or conn.home_currency or currency).upper()
    if conn.multicurrency and vendor_currency != currency:
        raise PostingBlocked(f"The invoice is in {currency} but the QuickBooks vendor '{mapping.display_name}' "
                             f"uses {vendor_currency}. QuickBooks needs a vendor per currency.")
    mapping.qbo_vendor_id = vendor["Id"]
    mapping.save(update_fields=["qbo_vendor_id", "updated_at"])
    return mapping


def bill_lines(doc: Document, what: str = "invoice", fallback: str | None = None) -> list[BillLine]:
    """The lines to post for a document: from the first registered builder that answers, else one per
    document line. Always adds up to the printed total (an adjustment line covers any difference)."""
    data = doc.data()
    if fallback is None:
        number = data.get("credit_note_number") if what == "credit note" else data.get("invoice_number")
        fallback = f"{'Credit note' if what == 'credit note' else 'Invoice'} {number or ''}".strip()
    for builder in LINE_BUILDERS:
        try:
            custom = builder(doc)
        except PostingBlocked:
            raise
        except Exception as e:
            log.exception("Bill line builder %s failed for document %s", getattr(builder, "__name__", builder), doc.pk)
            raise PostingBlocked(f"The {what}'s lines couldn't be prepared ({type(e).__name__}). "
                                 "Check the document and post again.") from e
        if custom is None:
            continue
        try:
            lines = [_as_line(raw) for raw in custom]
        except (TypeError, ValueError, InvalidOperation) as e:
            raise PostingBlocked(f"The {what}'s lines couldn't be prepared: {e}") from e
        if not lines or any(not line.amount.is_finite() for line in lines):
            raise PostingBlocked(f"The {what}'s lines are missing an amount. Check the document and post again.")
        total = _dec(data.get("total_amount"))
        difference = (total - sum(line.amount for line in lines)) if total is not None else Decimal("0")
        if difference != 0:
            lines.append(BillLine(f"Adjustment to the printed {what} total", difference))
        return lines
    return [BillLine(text, amount) for text, amount in _expense_lines(data, fallback, what)]


def _expense_lines(data: dict, fallback: str, what: str = "invoice") -> list[tuple[str, Decimal]]:
    """One expense line per document line. If the lines don't add up to the printed total (which an
    approver accepted), an adjustment line makes the result equal the amount on the document."""
    total = _dec(data.get("total_amount"))
    lines = []
    for item in data.get("line_items") or []:
        amount = _dec(item.get("amount"))
        if amount is None:
            continue
        lines.append((str(item.get("description") or "Charge")[:4000], amount))
    if not lines:
        if total is None:
            raise PostingBlocked(f"The {what} has no total amount. Add it in the review screen.")
        lines = [(fallback, total)]
    difference = (total - sum(a for _, a in lines)) if total is not None else Decimal("0")
    if difference != 0:
        lines.append((f"Adjustment to the printed {what} total", difference))
    return lines


def _line_json(lines: list, account_id: str) -> list[dict]:
    out = []
    for line in lines:
        line = _as_line(line)
        detail = {"AccountRef": {"value": account_id}}
        if line.class_ref:
            detail["ClassRef"] = {"value": line.class_ref}
        out.append({
            "DetailType": "AccountBasedExpenseLineDetail",
            "Amount": float(line.amount),
            "Description": line.text,
            "AccountBasedExpenseLineDetail": detail,
        })
    return out


def build_bill_payload(doc: Document, shipment: Shipment, vendor_id: str, account_id: str,
                       conn: QBOConnection | None = None) -> dict:
    """One expense line per invoice line, adjusted to the printed total."""
    data = doc.data()
    lines = bill_lines(doc, "invoice", f"Invoice {data.get('invoice_number') or ''}".strip())
    payload = {
        "VendorRef": {"value": vendor_id},
        "Line": _line_json(lines, account_id),
        "PrivateNote": f"ShipMatch {shipment.reference} | B/L {shipment.bl_number or '-'} | {doc.original_filename}"[:4000],
    }
    if data.get("invoice_number"):
        payload["DocNumber"] = str(data["invoice_number"])[:21]
    if data.get("invoice_date"):
        payload["TxnDate"] = data["invoice_date"]
    if data.get("due_date"):
        payload["DueDate"] = data["due_date"]
    if conn is not None:
        currency = bill_currency(doc, conn)
        if conn.multicurrency and currency != (conn.home_currency or currency):
            payload["CurrencyRef"] = {"value": currency}
    return payload


def build_vendor_credit_payload(doc: Document, shipment: Shipment, vendor_id: str, account_id: str,
                                conn: QBOConnection | None = None) -> dict:
    """A credit note as a QuickBooks VendorCredit: positive amounts on the same expense account, so the
    vendor's balance goes down by the credit."""
    data = doc.data()
    number = data.get("credit_note_number") or ""
    lines = bill_lines(doc, "credit note", f"Credit note {number}".strip())
    if any(line.amount < 0 for line in lines) or sum(line.amount for line in lines) <= 0:
        raise PostingBlocked("The credit note's amounts don't add up to a positive credit. Check the total and "
                             "the lines in the review screen.")
    original = data.get("original_invoice_number")
    note = (f"ShipMatch {shipment.reference} | credit note {number or '-'}"
            + (f" for invoice {original}" if original else "") + f" | {doc.original_filename}")
    payload = {"VendorRef": {"value": vendor_id}, "Line": _line_json(lines, account_id), "PrivateNote": note[:4000]}
    if number:
        payload["DocNumber"] = str(number)[:21]
    if data.get("invoice_date"):
        payload["TxnDate"] = data["invoice_date"]
    if conn is not None:
        currency = bill_currency(doc, conn)
        if conn.multicurrency and currency != (conn.home_currency or currency):
            payload["CurrencyRef"] = {"value": currency}
    return payload


def _check_currency(doc: Document, conn: QBOConnection) -> None:
    currency = bill_currency(doc, conn)
    home = (conn.home_currency or doc.organization.home_currency).upper()
    if currency != home and not conn.multicurrency:
        raise PostingBlocked(f"The invoice is in {currency}, but multicurrency is off in QuickBooks (home currency "
                             f"{home}). Turn on multicurrency in QuickBooks, or enter the bill there by hand.")


def _explain(e: Exception) -> str:
    if isinstance(e, QBOError):
        hint = FRIENDLY.get(getattr(e, "code", ""), "")
        ref = f" (Intuit reference {e.intuit_tid})" if getattr(e, "intuit_tid", "") else ""
        return f"{hint} {e}{ref}".strip()
    return str(e)


def post_shipment(shipment: Shipment, client=None, actor=None) -> dict:
    """Post every invoice and credit note of an approved shipment to the connected accounting system.

    `client` (a QBOClient or XeroClient) is optional; without it the organization's active connection is used.
    Safe to repeat: posted documents are skipped, and a document whose bill was created but not finished
    (attachment failed) is completed without creating a second bill."""
    from .providers import provider_for

    if shipment.status not in (Shipment.Status.APPROVED, Shipment.Status.POSTED):
        raise PostingBlocked("Only approved shipments can be posted")
    from apps.shipments.services.approval import posting_blockers

    held = posting_blockers(shipment)
    if held:
        raise PostingBlocked(" ".join(held))
    provider = provider_for(shipment.organization, client=client)
    if provider is None:
        raise PostingBlocked("No accounting system is connected for this organization. An admin can connect "
                             "QuickBooks or Xero in Settings > Accounting.")
    summary = {"posted": 0, "already_posted": 0, "failed": 0}
    provider.ensure_company()

    # Bills first, then the credit notes that reduce them.
    docs = sorted((d for d in shipment.documents.prefetch_related("fields") if d.posts_to_accounting),
                  key=lambda d: (d.is_credit, d.received_at, d.pk))
    for doc in docs:
        kind = PostedBill.Kind.VENDOR_CREDIT if doc.is_credit else PostedBill.Kind.BILL
        noun = "vendor_credit" if doc.is_credit else "bill"
        pb, _ = PostedBill.objects.get_or_create(
            document=doc, defaults={"organization": shipment.organization, "shipment": shipment,
                                    "request_id": request_id_for(doc), "kind": kind, "system": provider.key,
                                    "ledger_id": provider.ledger_id})
        if pb.status == PostedBill.Status.POSTED:
            summary["already_posted"] += 1
            continue
        try:
            _switch_system(pb, provider)
            provider.check_currency(doc)
            mapping, vendor_id = provider.resolve_vendor(doc)
            account = provider.account_for(mapping)
            if not pb.qbo_bill_id:  # create once; a retry with the same idempotency key is safe anyway
                created = provider.create(doc, shipment, vendor_id, account, pb)
                pb.qbo_bill_id, pb.external_number, pb.response, pb.kind = (
                    created.id, created.number[:60], created.raw, kind)
                pb.ledger_id = provider.ledger_id
                pb.save(update_fields=["qbo_bill_id", "external_number", "response", "kind", "ledger_id"])
            if not pb.qbo_attachable_id:
                pb.qbo_attachable_id = provider.attach(pb, doc)
            pb.status, pb.error, pb.posted_at = PostedBill.Status.POSTED, "", timezone.now()
            pb.save()
            summary["posted"] += 1
            audit(doc.organization, f"{noun}.posted", doc, actor=actor, qbo_bill_id=provider.audit_ref(pb),
                  request_id=pb.request_id, system=provider.name)
        except provider.stop_errors as e:
            pb.status, pb.error = PostedBill.Status.FAILED, provider.explain(e)[:2000]
            pb.save(update_fields=["status", "error"])
            summary["failed"] += 1
            audit(doc.organization, f"{noun}.failed", doc, actor=actor, error=str(e)[:300], system=provider.name)
            break  # every other bill would fail the same way
        except (*provider.errors, PostingBlocked) as e:
            pb.status, pb.error = PostedBill.Status.FAILED, provider.explain(e)[:2000]
            pb.save(update_fields=["status", "error"])
            summary["failed"] += 1
            audit(doc.organization, f"{noun}.failed", doc, actor=actor, error=provider.explain(e)[:300],
                  system=provider.name)

    payable = [d for d in shipment.documents if d.posts_to_accounting]
    if payable and all(PostedBill.objects.filter(document=d, status=PostedBill.Status.POSTED).exists() for d in payable):
        shipment.status = Shipment.Status.POSTED
        shipment.save(update_fields=["status", "updated_at"])
    return summary


def _switch_system(pb: PostedBill, provider) -> None:
    """A document that failed to post before the organization switched accounting systems: start again in the
    new one, unless the old system already holds a bill for it (posting again would mean paying twice)."""
    if pb.system == provider.key:
        return
    old = PostedBill.System(pb.system).label if pb.system in PostedBill.System.values else pb.system
    if pb.qbo_bill_id:
        what = "vendor credit" if pb.is_credit else "bill"
        raise PostingBlocked(f"This {what} was already created in {old} ({pb.display_number}) before the switch to "
                             f"{provider.name}. Finish or delete it in {old}; ShipMatch won't create it twice.")
    pb.system, pb.ledger_id, pb.qbo_attachable_id, pb.external_number = provider.key, provider.ledger_id, "", ""
    pb.save(update_fields=["system", "ledger_id", "qbo_attachable_id", "external_number"])
