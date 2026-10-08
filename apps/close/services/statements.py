"""Storing vendor statements and the payments they are compared with."""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounting.models import PostedBill, QBOConnection, VendorMapping, vendor_key
from apps.core.money import AmountError, parse_amount
from apps.core.utils import audit
from apps.documents.models import Document
from apps.documents.services.ingest import display_name, safe_filename
from apps.documents.services.normalize import norm_ref, parse_date

from ..models import StatementLine, VendorPayment, VendorStatement
from . import reconcile, statement_reader


def upload(org, filename: str, content: bytes, user) -> tuple[VendorStatement, bool]:
    """Read and store a statement, then reconcile it. Returns (statement, created); the same file twice
    returns the statement already stored. Raises RejectedFile or StatementUnreadable with a message for the
    person who uploaded it."""
    filename = display_name(filename)
    sha = hashlib.sha256(content).hexdigest()
    existing = VendorStatement.objects.filter(organization=org, sha256=sha).first()
    if existing:
        return existing, False
    parsed = statement_reader.read(filename, content)
    with transaction.atomic():
        st = VendorStatement(organization=org, sha256=sha, original_filename=filename,
                             source_format=parsed.source_format, text=parsed.text[:200000], reader=parsed.reader,
                             notes=list(parsed.notes), llm_usage=parsed.usage or {},
                             statement_date=parsed.statement_date, currency=parsed.currency or org.home_currency,
                             opening_balance=parsed.opening_balance, closing_balance=parsed.closing_balance,
                             uploaded_by=user if getattr(user, "is_authenticated", False) else None)
        known = reconcile.best_vendor(org, parsed.vendor_name)
        if known:
            st.set_vendor(known[1])
        else:
            st.set_vendor(parsed.vendor_name)
            st.status = VendorStatement.Status.NEEDS_VENDOR
            st.notes.append(f"ShipMatch has no invoices from “{parsed.vendor_name}”. Choose the vendor this statement "
                            "is from." if parsed.vendor_name else "The vendor's name couldn't be read. Choose the "
                                                                    "vendor this statement is from.")
        if not parsed.currency:
            st.notes.append(f"No currency was printed; {org.home_currency} is assumed.")
        st.file.save(safe_filename(filename), ContentFile(content), save=False)
        st.save()
        StatementLine.objects.bulk_create([
            StatementLine(statement=st, position=n, kind=ln.kind, number=ln.number[:80], date=ln.date,
                          reference=ln.reference[:200], amount=ln.amount, balance=ln.balance, raw=ln.raw[:500])
            for n, ln in enumerate(parsed.lines, 1)])
        audit(org, "close.statement_uploaded", st, actor=user, vendor=st.vendor_name or "an unknown vendor",
              filename=filename, lines=len(parsed.lines), reader=parsed.reader,
              statement_date=st.statement_date.isoformat() if st.statement_date else None)
        if st.status != VendorStatement.Status.NEEDS_VENDOR:
            reconcile.run(st)
    return st, True


class PaymentError(ValueError):
    pass


def _amount(raw) -> Decimal:
    try:
        value = parse_amount(raw, allow_negative=True)
    except AmountError as e:
        raise PaymentError("That amount is too large. Check the number." if e.kind == "range"
                           else "Type the amount as a number, for example 2535.00.") from None
    if value <= 0:
        raise PaymentError("A payment amount must be more than zero.")
    return value


def allocations_for(org, vk: str, numbers: list[str], total: Decimal) -> tuple[list[dict], list[str]]:
    """Invoices a payment paid, found by invoice number among this vendor's documents."""
    wanted = [n.strip() for n in numbers if n and n.strip()]
    if not wanted:
        return [], []
    docs = {}
    for d in (Document.objects.filter(organization=org, doc_type__in=list(Document.PAYABLE_TYPES))
              .exclude(status__in=list(Document.CONTAINER_STATUSES)).prefetch_related("fields")):
        data = d.data()
        if vendor_key(data.get("vendor_name")) == vk and data.get("invoice_number"):
            docs.setdefault(norm_ref(data["invoice_number"]), (d, data))
    out, unknown, left = [], [], total
    for number in wanted:
        found = docs.get(norm_ref(number))
        if not found:
            unknown.append(number)
            out.append({"document_id": None, "invoice_number": number[:60], "amount": ""})
            continue
        d, data = found
        try:
            due = Decimal(str(data.get("total_amount"))).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError, TypeError):
            due = left
        paid = min(due, left) if left > 0 else Decimal("0.00")
        left -= paid
        out.append({"document_id": d.pk, "invoice_number": str(data["invoice_number"])[:60], "amount": f"{paid:.2f}"})
    return out, unknown


def record_payment(org, user, *, vendor_name: str, paid_on, amount, currency: str, reference: str = "",
                   invoices: str = "", note: str = "") -> tuple[VendorPayment, list[str]]:
    name = (vendor_name or "").strip()
    if not vendor_key(name):
        raise PaymentError("Choose the vendor you paid.")
    day = paid_on if isinstance(paid_on, date) else parse_date(paid_on)
    if day is None:
        raise PaymentError("Type the payment date, for example 2026-09-15.")
    if day > timezone.localdate() + timedelta(days=1):
        raise PaymentError("The payment date is in the future. Record payments once they are made.")
    value = _amount(amount)
    cur = (currency or org.home_currency).strip().upper()   # not cut to 3 letters: "DOLLARS" is an error, not "DOL"
    if len(cur) != 3 or not cur.isalpha():
        raise PaymentError("Type the currency as a three-letter code, for example USD.")
    numbers = [n for n in (invoices or "").replace(";", ",").replace("\n", ",").split(",") if n.strip()]
    allocs, unknown = allocations_for(org, vendor_key(name), numbers, value)
    pay = VendorPayment.objects.create(organization=org, vendor_name=name, paid_on=day, amount=value, currency=cur,
                                       reference=(reference or "").strip()[:100], allocations=allocs,
                                       note=(note or "").strip()[:300], source=VendorPayment.Source.MANUAL,
                                       created_by=user)
    audit(org, "close.payment_recorded", pay, actor=user, vendor=name, amount=f"{cur} {value:,.2f}",
          paid_on=day.isoformat(), reference=pay.reference, invoices=[a["invoice_number"] for a in allocs])
    return pay, unknown


class SyncError(RuntimeError):
    pass


def sync_quickbooks_payments(org, user, client=None, today: date | None = None) -> dict:
    """Read bill payments from QuickBooks (BillPayment entity) for the last CLOSE_PAYMENT_SYNC_DAYS days.

    Each payment is stored once (by its QuickBooks Id) with the vendor (through the vendor's QuickBooks ID in
    the vendor mappings, else the name QuickBooks gives) and the bills it paid (through the bill IDs ShipMatch
    stored when posting). Only reads; nothing is written to QuickBooks.
    https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/billpayment
    """
    from apps.accounting.services.quickbooks import QBOClient, QBOError
    from apps.demo.mail import outbound_blocked

    if outbound_blocked():
        raise SyncError("This is the public demo, so ShipMatch doesn't contact QuickBooks. Record payments by hand "
                        "to try the reconciliation.")
    conn = QBOConnection.objects.filter(organization=org).first()
    if conn is None:
        raise SyncError("QuickBooks isn't connected. An admin can connect it in Settings > QuickBooks.")
    if conn.needs_reconnect:
        raise SyncError("QuickBooks needs to be connected again. An admin can reconnect it in Settings > QuickBooks.")
    client = client or QBOClient(conn)
    since = (today or timezone.localdate()) - timedelta(days=settings.CLOSE_PAYMENT_SYNC_DAYS)
    vendors = {m.qbo_vendor_id: m for m in VendorMapping.objects.filter(organization=org).exclude(qbo_vendor_id="")}
    # QuickBooks bill ids are small numbers per company: only bills posted to this company can match.
    bills = {pb.qbo_bill_id: pb for pb in PostedBill.objects.filter(
                 organization=org, kind=PostedBill.Kind.BILL, system=PostedBill.System.QUICKBOOKS)
             .filter(Q(ledger_id=conn.realm_id) | Q(ledger_id=""))
             .exclude(qbo_bill_id="").select_related("document").prefetch_related("document__fields")}
    created = updated = skipped = 0
    start, page = 1, 500
    try:
        while True:
            rows = client.query(f"select * from BillPayment where TxnDate >= '{since.isoformat()}' "
                                f"startposition {start} maxresults {page}").get("BillPayment") or []
            for bp in rows:
                ref = bp.get("VendorRef") or {}
                mapping = vendors.get(str(ref.get("value") or ""))
                name = mapping.display_name if mapping else str(ref.get("name") or "")
                day = parse_date(bp.get("TxnDate"))
                try:
                    total = Decimal(str(bp.get("TotalAmt"))).quantize(Decimal("0.01"))
                except (InvalidOperation, ValueError):
                    total = None
                if not vendor_key(name) or day is None or total is None or not bp.get("Id"):
                    skipped += 1
                    continue
                allocs = []
                for line in bp.get("Line") or []:
                    for linked in line.get("LinkedTxn") or []:
                        if linked.get("TxnType") != "Bill":
                            continue
                        pb = bills.get(str(linked.get("TxnId")))
                        allocs.append({"document_id": pb.document_id if pb else None,
                                       "invoice_number": str(pb.document.field("invoice_number") or "") if pb else "",
                                       "qbo_bill_id": str(linked.get("TxnId")),
                                       "amount": str(line.get("Amount") or "")})
                _, new = VendorPayment.objects.update_or_create(
                    organization=org, qbo_id=str(bp["Id"])[:40],
                    defaults={"vendor_name": name, "paid_on": day, "amount": total,
                              "currency": ((bp.get("CurrencyRef") or {}).get("value") or conn.home_currency
                                           or org.home_currency),
                              "reference": str(bp.get("DocNumber") or "")[:100], "allocations": allocs,
                              "source": VendorPayment.Source.QUICKBOOKS})
                created += int(new)
                updated += int(not new)
            if len(rows) < page:
                break
            start += page
    except QBOError as e:
        raise SyncError(f"QuickBooks didn't return the payments: {e}") from e
    result = {"created": created, "updated": updated, "skipped": skipped, "since": since.isoformat()}
    audit(org, "close.payments_synced", conn, actor=user, **result)
    return result
