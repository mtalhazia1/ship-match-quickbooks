"""Read posted bills' payment status from QuickBooks or Xero, in batches, as PaymentInfo.

QuickBooks: `select * from Bill where Id in (...)` gives Balance, TotalAmt, DueDate and LinkedTxn; the linked
bill payments (`select * from BillPayment where Id in (...)`) give the payment dates and amounts, and any
vendor credit applied in the same payment. Vendor credits: `select * from VendorCredit where Id in (...)`,
Balance is the credit not used yet. A bill missing from the results is read once more on its own: QuickBooks
answers "not found" for a deleted bill. A voided bill comes back with a zero total and a "Voided" note.

Xero: `GET Invoices?IDs=...` (every status, so voided and deleted bills are reported, not dropped) gives Status,
AmountDue, AmountPaid, AmountCredited, DueDate, FullyPaidOnDate and the payments; credit notes are read with
a where filter on CreditNoteID and give RemainingCredit and their allocations to bills.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from apps.accounting.models import PostedBill

from .providers import PaymentInfo
from .xero import money, parse_date

log = logging.getLogger(__name__)
P = PostedBill.Payment
ZERO = Decimal("0.00")


def _status(total: Decimal | None, open_amount: Decimal | None) -> str:
    """unpaid / partly paid / paid from the total and what is still open."""
    if total is None or open_amount is None:
        return P.UNPAID
    if open_amount <= ZERO and total > ZERO:
        return P.PAID
    if ZERO < open_amount < total:
        return P.PARTLY_PAID
    return P.UNPAID


def _iso(d: date | None) -> str:
    return d.isoformat() if d else ""


def _qbo_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- QuickBooks


def quickbooks_statuses(client, bills: list[PostedBill]) -> dict[int, PaymentInfo]:
    out: dict[int, PaymentInfo] = {}
    bill_rows = [pb for pb in bills if not pb.is_credit and pb.qbo_bill_id]
    credit_rows = [pb for pb in bills if pb.is_credit and pb.qbo_bill_id]

    found = {str(b.get("Id")): b for b in client.bills_by_ids([pb.qbo_bill_id for pb in bill_rows])}
    payment_ids = {str(link.get("TxnId")) for b in found.values() for link in b.get("LinkedTxn") or []
                   if str(link.get("TxnType") or "").startswith("BillPayment")}
    payments = {str(p.get("Id")): p for p in client.bill_payments_by_ids(sorted(payment_ids))} if payment_ids else {}

    for pb in bill_rows:
        bill = found.get(pb.qbo_bill_id)
        if bill is None:
            gone = client.exists("Bill", pb.qbo_bill_id)
            if gone is False:
                out[pb.pk] = PaymentInfo(P.DELETED, currency=pb.currency, total=pb.amount_total)
            elif gone is True:   # read on its own after all: try again on the next check
                log.info("QuickBooks bill %s missing from the batch but readable", pb.qbo_bill_id)
            continue
        out[pb.pk] = _qbo_bill(pb, bill, payments)

    credits = {str(c.get("Id")): c for c in client.vendor_credits_by_ids([pb.qbo_bill_id for pb in credit_rows])}
    for pb in credit_rows:
        credit = credits.get(pb.qbo_bill_id)
        if credit is None:
            if client.exists("VendorCredit", pb.qbo_bill_id) is False:
                out[pb.pk] = PaymentInfo(P.DELETED, currency=pb.currency, total=pb.amount_total)
            continue
        total, balance = money(credit.get("TotalAmt")), money(credit.get("Balance"))
        currency = str((credit.get("CurrencyRef") or {}).get("value") or "")
        if _voided(credit, total, pb):
            out[pb.pk] = PaymentInfo(P.VOIDED, total, ZERO, ZERO, currency)
            continue
        used = (total - balance) if total is not None and balance is not None else None
        out[pb.pk] = PaymentInfo(_status(total, balance), total, used, balance, currency)
    return out


def _voided(entity: dict, total: Decimal | None, pb: PostedBill) -> bool:
    """QuickBooks keeps a voided bill or credit with a zero total (and "Voided" at the start of its note)."""
    note = str(entity.get("PrivateNote") or "")
    posted_nonzero = bool(pb.response) or (pb.amount_total or ZERO) > ZERO
    return total is not None and total == ZERO and (note.lower().startswith("voided") or posted_nonzero)


def _qbo_bill(pb: PostedBill, bill: dict, payments: dict) -> PaymentInfo:
    total, balance = money(bill.get("TotalAmt")), money(bill.get("Balance"))
    currency = str((bill.get("CurrencyRef") or {}).get("value") or "")
    due_date = _qbo_date(bill.get("DueDate"))
    if _voided(bill, total, pb):
        return PaymentInfo(P.VOIDED, total, ZERO, ZERO, currency, due_date)
    rows = []
    for link in bill.get("LinkedTxn") or []:
        payment = payments.get(str(link.get("TxnId")))
        if not payment or not str(link.get("TxnType") or "").startswith("BillPayment"):
            continue
        when = _qbo_date(payment.get("TxnDate"))
        ref = str(payment.get("DocNumber") or "")
        for line in payment.get("Line") or []:
            for linked in line.get("LinkedTxn") or []:
                amount = money(line.get("Amount"))
                if linked.get("TxnType") == "Bill" and str(linked.get("TxnId")) == pb.qbo_bill_id and amount:
                    rows.append({"date": _iso(when), "amount": str(amount), "kind": "payment", "reference": ref})
                elif linked.get("TxnType") == "VendorCredit" and amount:
                    rows.append({"date": _iso(when), "amount": str(amount), "kind": "credit",
                                 "reference": f"vendor credit {linked.get('TxnId')}"})
    status = _status(total, balance)
    paid = (total - balance) if total is not None and balance is not None else None
    paid_on = None
    if status == P.PAID:
        dates = [r["date"] for r in rows if r["date"] and r["kind"] == "payment"] or [r["date"] for r in rows if r["date"]]
        paid_on = _qbo_date(max(dates)) if dates else None
    rows.sort(key=lambda r: r["date"])
    return PaymentInfo(status, total, paid, balance, currency, due_date, paid_on, rows)


# --------------------------------------------------------------------------- Xero


def xero_statuses(client, bills: list[PostedBill]) -> dict[int, PaymentInfo]:
    out: dict[int, PaymentInfo] = {}
    bill_rows = [pb for pb in bills if not pb.is_credit and pb.qbo_bill_id]
    credit_rows = [pb for pb in bills if pb.is_credit and pb.qbo_bill_id]
    if bill_rows:
        found = {str(i.get("InvoiceID")).lower(): i for i in client.invoices_by_ids([pb.qbo_bill_id for pb in bill_rows])}
        for pb in bill_rows:
            invoice = found.get(pb.qbo_bill_id.lower())
            # Xero keeps voided and deleted bills (we ask for every status), so a missing one no longer exists.
            out[pb.pk] = _xero_invoice(invoice) if invoice else PaymentInfo(P.DELETED, total=pb.amount_total,
                                                                             currency=pb.currency)
    if credit_rows:
        found = {str(c.get("CreditNoteID")).lower(): c
                 for c in client.credit_notes_by_ids([pb.qbo_bill_id for pb in credit_rows])}
        for pb in credit_rows:
            credit = found.get(pb.qbo_bill_id.lower())
            out[pb.pk] = _xero_credit(credit) if credit else PaymentInfo(P.DELETED, total=pb.amount_total,
                                                                          currency=pb.currency)
    return out


def _xero_invoice(inv: dict) -> PaymentInfo:
    status = str(inv.get("Status") or "")
    total, due = money(inv.get("Total")), money(inv.get("AmountDue"))
    paid_cash, credited = money(inv.get("AmountPaid")) or ZERO, money(inv.get("AmountCredited")) or ZERO
    currency = str(inv.get("CurrencyCode") or "")
    due_date = parse_date(inv.get("DueDateString") or inv.get("DueDate"))
    rows = [{"date": _iso(parse_date(p.get("Date"))), "amount": str(money(p.get("Amount")) or ZERO), "kind": "payment",
             "reference": str(p.get("Reference") or "")} for p in inv.get("Payments") or []]
    rows += [{"date": _iso(parse_date(c.get("Date"))), "amount": str(money(c.get("AppliedAmount")) or ZERO),
              "kind": "credit", "reference": str(c.get("CreditNoteNumber") or "")} for c in inv.get("CreditNotes") or []]
    rows += [{"date": _iso(parse_date(o.get("Date"))), "amount": str(money(o.get("AppliedAmount")) or ZERO),
              "kind": "credit", "reference": "prepayment or overpayment"}
             for o in (inv.get("Prepayments") or []) + (inv.get("Overpayments") or [])]
    rows.sort(key=lambda r: r["date"])
    if status == "VOIDED":
        return PaymentInfo(P.VOIDED, total, ZERO, ZERO, currency, due_date, payments=rows)
    if status == "DELETED":
        return PaymentInfo(P.DELETED, total, ZERO, ZERO, currency, due_date, payments=rows)
    settled = paid_cash + credited
    if status == "PAID":
        state = P.PAID
    elif total is not None and due is not None:
        state = _status(total, due)
    else:
        state = P.PARTLY_PAID if settled > ZERO else P.UNPAID
    paid_on = None
    if state == P.PAID:
        paid_on = parse_date(inv.get("FullyPaidOnDate")) or max(
            (parse_date(r["date"]) for r in rows if r["date"]), default=None)
    return PaymentInfo(state, total, settled, due if due is not None else None, currency, due_date, paid_on, rows)


def _xero_credit(cn: dict) -> PaymentInfo:
    status = str(cn.get("Status") or "")
    total, remaining = money(cn.get("Total")), money(cn.get("RemainingCredit"))
    currency = str(cn.get("CurrencyCode") or "")
    rows = [{"date": _iso(parse_date(a.get("Date"))), "amount": str(money(a.get("Amount")) or ZERO), "kind": "credit",
             "reference": str((a.get("Invoice") or {}).get("InvoiceNumber") or "")} for a in cn.get("Allocations") or []]
    rows += [{"date": _iso(parse_date(p.get("Date"))), "amount": str(money(p.get("Amount")) or ZERO),
              "kind": "refund", "reference": str(p.get("Reference") or "")} for p in cn.get("Payments") or []]
    rows.sort(key=lambda r: r["date"])
    if status == "VOIDED":
        return PaymentInfo(P.VOIDED, total, ZERO, ZERO, currency, payments=rows)
    if status == "DELETED":
        return PaymentInfo(P.DELETED, total, ZERO, ZERO, currency, payments=rows)
    if status == "PAID":
        state = P.PAID
    elif status in ("DRAFT", "SUBMITTED") or remaining is None:
        state = P.UNPAID
    else:
        state = _status(total, remaining)
    used = (total - remaining) if total is not None and remaining is not None else None
    if state == P.PAID and remaining is None:
        used, remaining = total, ZERO
    paid_on = parse_date(cn.get("FullyPaidOnDate")) if state == P.PAID else None
    return PaymentInfo(state, total, used, remaining, currency, paid_on=paid_on, payments=rows)
