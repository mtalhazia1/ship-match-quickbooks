"""Reconcile a vendor statement with what ShipMatch knows about that vendor.

ShipMatch's side, for the statement's vendor (same vendor key, or a name that differs only by a typo) and up
to the statement date:
  * invoices received (dated on or before the statement date), each at its printed total; an invoice in a
    rejected shipment counts as nothing owed, and an open possible duplicate is left out;
  * credit notes received, and credits recorded on disputes without a credit note document;
  * payments made (recorded here or read from QuickBooks).

Each statement line is matched to one item of the same kind: by invoice number (letters, digits, spaces
and punctuation compared without case: "HA-123 456" = "ha123456"), else by the digits of the number when
they are unique, else by B/L and amount; payments by their reference, else by amount within ten days.

Results, by bucket (see ReconItem.Bucket): matched; amount differs; on the statement but never received;
received but not on the statement; credit notes the vendor hasn't applied; possible duplicates on the
statement (the same number twice, or the same amount, date and reference under another number); payments
the vendor hasn't applied; payments ShipMatch has no record of. An invoice whose amount on the statement is
lower by exactly a credit note that names it is matched: the vendor applied that credit.

Statements come in two shapes. An activity statement (it lists payments, or starts from a balance brought
forward) lists everything since its first line; ShipMatch's items before that date are compared as one
opening balance. An open-item statement lists only what is still open; invoices ShipMatch knows were paid in
full are expected to be missing from it and are shown as paid and closed.

The summary explains the difference between the statement balance and ShipMatch's balance line by line:
every item carries its effect on that difference, and the effects add up to it exactly.

Nothing here changes a document, a shipment or a bill: the reconciliation only reads them.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import quote

from django.db import transaction
from django.utils import timezone
from rapidfuzz import fuzz

from apps.accounting.models import PostedBill, vendor_key
from apps.documents.models import Document
from apps.documents.services.normalize import norm_ref, parse_date
from apps.rates.checks import Converter
from apps.shipments.models import Shipment, ValidationIssue

from ..models import ReconItem, StatementLine, VendorPayment, VendorStatement
from .history import DUPLICATE_CODES, dec

ZERO = Decimal("0.00")
TOLERANCE = Decimal("0.01")
B = ReconItem.Bucket
PAYMENT_WINDOW = timedelta(days=10)


@dataclass
class Item:
    """One thing ShipMatch knows that could appear on the statement."""

    kind: str                      # invoice | credit | payment
    number: str
    day: date | None
    amount: Decimal | None         # positive, in the statement's currency (None: no exchange rate)
    owed: Decimal | None           # signed effect on what ShipMatch says is owed
    label: str
    document: Document | None = None
    payment: VendorPayment | None = None
    dispute_id: int | None = None
    bl: str = ""
    currency: str = ""
    original: str = ""             # credit notes: the invoice they credit (normalized)
    original_text: str = ""        # ... as printed
    note: str = ""
    info: dict = field(default_factory=dict)
    matched: bool = False
    settled: bool = False

    @property
    def key(self) -> str:
        return norm_ref(self.number)

    @property
    def digits(self) -> str:
        return re.sub(r"\D", "", self.number or "")

    @property
    def fingerprint(self) -> str:
        if self.document is not None:
            return f"doc:{self.document.pk}"
        if self.payment is not None:
            return f"pay:{self.payment.pk}"
        return f"dispute:{self.dispute_id}"


def _same_vendor_keys(org, vk: str) -> set[str]:
    keys = {vk}
    names = set()
    for d in Document.objects.filter(organization=org, fields__name="vendor_name").values_list("fields__value",
                                                                                                 flat=True):
        names.add(vendor_key(d if isinstance(d, str) else ""))
    for k in names:
        if k and k != vk and fuzz.ratio(k, vk) >= 92:
            keys.add(k)
    return keys


def known_vendors(org) -> list[tuple[str, str]]:
    """(vendor_key, name) of every vendor ShipMatch has invoices or credit notes from, for the vendor picker."""
    out: dict[str, str] = {}
    qs = Document.objects.filter(organization=org, doc_type__in=list(Document.PAYABLE_TYPES | Document.CREDIT_TYPES),
                                 fields__name="vendor_name").values_list("fields__value", flat=True)
    for name in qs:
        if isinstance(name, str) and name.strip():
            out.setdefault(vendor_key(name), name.strip())
    for p in VendorPayment.objects.filter(organization=org).values_list("vendor_key", "vendor_name"):
        out.setdefault(p[0], p[1])
    return sorted(((k, v) for k, v in out.items() if k), key=lambda kv: kv[1].lower())


def best_vendor(org, name: str) -> tuple[str, str] | None:
    """The known vendor a statement's printed name refers to, or None."""
    vk = vendor_key(name)
    if not vk:
        return None
    best, score = None, 0.0
    for k, display in known_vendors(org):
        s = 100.0 if k == vk else max(fuzz.ratio(k, vk), fuzz.token_set_ratio(k, vk) - 4)
        if s > score:
            best, score = (k, display), s
    return best if score >= 90 else None


def shipmatch_items(org, statement: VendorStatement) -> list[Item]:
    vk, day = statement.vendor_key, statement.statement_date or timezone.localdate()
    cur = (statement.currency or org.home_currency).upper()
    fx = Converter(org)
    keys = _same_vendor_keys(org, vk)
    duplicates = set(ValidationIssue.objects.filter(organization=org, resolved=False, code__in=DUPLICATE_CODES,
                                                    document__isnull=False).values_list("document_id", flat=True))
    items: list[Item] = []
    docs = (Document.objects.filter(organization=org, doc_type__in=list(Document.PAYABLE_TYPES | Document.CREDIT_TYPES))
            .exclude(status__in=list(Document.CONTAINER_STATUSES))
            .select_related("match__shipment", "posted_bill").prefetch_related("fields"))
    for d in docs:
        data = d.data()
        if vendor_key(data.get("vendor_name")) not in keys:
            continue
        when = parse_date(data.get("invoice_date")) or timezone.localdate(d.received_at)
        if when > day or d.pk in duplicates:
            continue
        total = dec(data.get("total_amount"))
        doc_cur = (data.get("currency") or org.home_currency).upper()
        amount = fx.convert(abs(total), doc_cur, cur) if total is not None else None
        shipment = d.match.shipment if hasattr(d, "match") else None
        info = {"shipment": shipment.reference if shipment else "", "shipment_id": shipment.pk if shipment else None,
                "posted": _posted_text(d), "currency": doc_cur}
        if doc_cur != cur and total is not None:
            info["original"] = f"{doc_cur} {abs(total):,.2f}"
        if d.is_credit:
            items.append(Item("credit", str(data.get("credit_note_number") or ""), when, amount,
                              -amount if amount is not None else None,
                              f"Credit note {data.get('credit_note_number') or d.original_filename}", document=d,
                              bl=norm_ref(data.get("bl_number")), currency=doc_cur,
                              original=norm_ref(data.get("original_invoice_number")),
                              original_text=str(data.get("original_invoice_number") or ""), info=info))
            continue
        rejected = shipment is not None and shipment.status == Shipment.Status.REJECTED
        items.append(Item("invoice", str(data.get("invoice_number") or ""), when, amount,
                          ZERO if rejected else amount, f"Invoice {data.get('invoice_number') or d.original_filename}",
                          document=d, bl=norm_ref(data.get("bl_number") or (shipment.bl_number if shipment else "")),
                          currency=doc_cur, note="rejected" if rejected else "", info=info))

    from apps.disputes.models import Dispute

    for disp in Dispute.objects.filter(organization=org, vendor_key__in=keys, credit_note__isnull=True,
                                       status__in=list(Dispute.RECOVERED), amount_recovered__gt=0):
        when = timezone.localdate(disp.recovered_at) if disp.recovered_at else None
        if when and when > day:
            continue
        amount = fx.convert(disp.amount_recovered, disp.currency or org.home_currency, cur)
        items.append(Item("credit", "", when, amount, -amount if amount is not None else None,
                          f"Credit recorded on dispute {disp.reference}", dispute_id=disp.pk,
                          original=norm_ref(disp.invoice_number), original_text=disp.invoice_number,
                          currency=disp.currency,
                          info={"dispute": disp.reference, "dispute_id": disp.pk}))

    for pay in VendorPayment.objects.filter(organization=statement.organization, vendor_key__in=keys, paid_on__lte=day):
        amount = fx.convert(pay.amount, pay.currency, cur)
        items.append(Item("payment", pay.reference, pay.paid_on, amount, -amount if amount is not None else None,
                          f"Payment {pay.reference + ' ' if pay.reference else ''}on {_day(pay.paid_on)}", payment=pay,
                          currency=pay.currency, info={"source": pay.get_source_display(),
                                                       "invoices": pay.invoice_numbers}))
    return items


def _posted_text(doc: Document) -> str:
    try:
        pb = doc.posted_bill
    except PostedBill.DoesNotExist:
        return "Not posted to accounting"
    system = pb.get_system_display()
    if pb.status == PostedBill.Status.POSTED:
        what = "vendor credit" if pb.kind == PostedBill.Kind.VENDOR_CREDIT else "bill"
        when = f" on {_day(timezone.localdate(pb.posted_at))}" if pb.posted_at else ""
        return f"Posted to {system} as {what} {pb.external_number or pb.qbo_bill_id}{when}"
    if pb.status == PostedBill.Status.FAILED:
        return f"Posting to {system} failed"
    return f"Not posted to {system}"


def _money(amount: Decimal | None, cur: str) -> str:
    return "–" if amount is None else f"{cur} {amount:,.2f}"


@dataclass
class Spec:
    bucket: str
    fingerprint: str
    label: str
    effect: Decimal = ZERO
    statement_amount: Decimal | None = None
    shipmatch_amount: Decimal | None = None
    explanation: str = ""
    line: StatementLine | None = None
    item: Item | None = None
    data: dict = field(default_factory=dict)


def _line_label(line: StatementLine) -> str:
    what = {StatementLine.Kind.INVOICE: "Invoice", StatementLine.Kind.CREDIT: "Credit note",
            StatementLine.Kind.PAYMENT: "Payment"}.get(line.kind, "Line")
    return f"{what} {line.number}".strip() if line.number else f"{what} on line {line.position}"


def compare(org, statement: VendorStatement) -> tuple[list[Spec], dict]:
    cur = (statement.currency or org.home_currency).upper()
    day = statement.statement_date or timezone.localdate()
    lines = list(statement.lines.all())
    body = [ln for ln in lines if ln.kind != StatementLine.Kind.OPENING]
    opening_line = next((ln for ln in lines if ln.kind == StatementLine.Kind.OPENING), None)
    stmt_opening = statement.opening_balance if statement.opening_balance is not None else (
        opening_line.amount if opening_line else None)
    activity = stmt_opening is not None or any(ln.kind == StatementLine.Kind.PAYMENT for ln in body)
    period_start = min((ln.date for ln in body if ln.date), default=None) if stmt_opening is not None else None

    every = shipmatch_items(org, statement)
    unread = Document.objects.filter(organization=org,
                                     status__in=[Document.Status.NEEDS_OCR, Document.Status.ERROR]).count()
    before = [i for i in every if period_start and i.day and i.day < period_start]
    before_ids = {id(i) for i in before}
    scope = [i for i in every if id(i) not in before_ids]
    specs: list[Spec] = []

    pools = {"invoice": [i for i in scope if i.kind == "invoice"], "credit": [i for i in scope if i.kind == "credit"],
             "payment": [i for i in scope if i.kind == "payment"]}
    seen: dict[str, dict[str, StatementLine]] = defaultdict(dict)
    for ln in body:
        kind = ln.kind
        key = norm_ref(ln.number)
        magnitude = abs(ln.amount)
        base = dict(line=ln, statement_amount=magnitude, label=_line_label(ln))
        label = base["label"]
        if key and key in seen[kind]:
            first = seen[kind][key]
            specs.append(Spec(B.DUPLICATE, f"dup:line:{ln.position}:{key}", effect=ln.amount,
                              explanation=f"{label} is listed twice on the statement (also on line {first.position}), "
                                          f"so the vendor's balance counts it twice. Ask the vendor to remove the copy.",
                              data={"first_line": first.position}, **base))
            continue
        match, how = _find(pools[kind], ln)
        if key:
            seen[kind][key] = ln
        if match is None:
            twin = _same_content(body, ln, specs)
            if twin is not None:
                specs.append(Spec(B.DUPLICATE, f"dup:line:{ln.position}:{key}", effect=ln.amount,
                                  explanation=f"Same amount, date and reference as {_line_label(twin)} (line "
                                              f"{twin.position}) under another number. Ask the vendor whether it was "
                                              "billed twice.", data={"first_line": twin.position}, **base))
                continue
            if kind == "payment":
                specs.append(Spec(B.PAYMENT_UNKNOWN, f"payunknown:line:{ln.position}:{key}:{magnitude}",
                                  effect=ln.amount,
                                  explanation=f"The vendor recorded a payment of {_money(magnitude, cur)}"
                                              f"{_on(ln.date)} that ShipMatch has no record of. Record it under "
                                              "Payments (or read payments from QuickBooks) if you made it.", **base))
                continue
            hint = _possible(pools[kind], ln)
            if kind == "credit":
                text = (f"The statement shows a credit of {_money(magnitude, cur)}{_on(ln.date)} that ShipMatch never "
                        "received. Ask the vendor for a copy of the credit note.")
            else:
                text = (f"The vendor billed {_money(magnitude, cur)}{_on(ln.date)}, but ShipMatch never received this "
                        "invoice. Request a copy, then check it like any other invoice.")
            if hint:
                text += f" {hint}"
            elif unread:
                text += (f" ShipMatch also has {unread} document{'s' if unread != 1 else ''} it couldn't read yet "
                         "(see Documents); it may be one of them.")
            specs.append(Spec(B.MISSING, f"missing:line:{ln.position}:{key}:{magnitude}", effect=ln.amount,
                              explanation=text, data={"kind": kind}, **base))
            continue
        match.matched = True
        specs.append(_matched_spec(ln, match, how, cur, base))

    _credits_applied(specs, pools["credit"], cur)

    settled_ids: set[int] = set()
    if not activity:
        settled_ids = _settled(pools["invoice"], pools["payment"])
    for i in scope:
        if i.matched:
            continue
        info = dict(i.info)
        fp = i.fingerprint
        if i.kind == "invoice":
            if i.settled or id(i) in settled_ids:
                specs.append(Spec(B.SETTLED, f"settled:{fp}", i.label, effect=-(i.owed or ZERO),
                                  shipmatch_amount=i.amount, item=i, data=info,
                                  explanation="Paid in full according to ShipMatch, so an open-items statement no "
                                              "longer lists it."))
            elif i.note == "rejected":
                specs.append(Spec(B.SETTLED, f"settled:{fp}", i.label, effect=ZERO, shipmatch_amount=i.amount,
                                  item=i, data=info, explanation="Its shipment was rejected in ShipMatch and the "
                                                                 "vendor doesn't bill it either."))
            elif i.amount is None:
                specs.append(Spec(B.NOT_ON_STATEMENT, f"notonstmt:{fp}", i.label, effect=ZERO, item=i, data=info,
                                  explanation=f"Not on the statement. It is in {i.currency} and there is no exchange "
                                              f"rate to {cur} in Settings, so it isn't in ShipMatch's balance."))
            else:
                specs.append(Spec(B.NOT_ON_STATEMENT, f"notonstmt:{fp}", i.label, effect=-i.owed,
                                  shipmatch_amount=i.amount, item=i, data=info,
                                  explanation=f"ShipMatch received this invoice ({_money(i.amount, cur)}{_on(i.day)}), "
                                              "but the vendor doesn't list it. If you paid it, record the payment; "
                                              "otherwise ask the vendor whether they cancelled it or are missing it."))
        elif i.kind == "credit":
            if i.amount is None:
                continue
            specs.append(Spec(B.CREDIT_NOT_APPLIED, f"credit:{fp}", i.label, effect=-i.owed, shipmatch_amount=i.amount,
                              item=i, data=info,
                              explanation=f"ShipMatch has this credit ({_money(i.amount, cur)}{_on(i.day)}), but the "
                                          "vendor's balance doesn't include it. Ask them to apply it"
                                          + (f" against invoice {i.original_text or i.original}" if i.original else "") + "."))
        else:
            if i.amount is None:
                continue
            if id(i) in settled_ids:
                specs.append(Spec(B.SETTLED, f"settled:{fp}", i.label, effect=-i.owed, shipmatch_amount=i.amount,
                                  item=i, data=info, explanation="Paid invoices that an open-items statement no "
                                                                 "longer lists."))
                continue
            transit = (day - i.day).days if i.day else None
            text = f"You paid {_money(i.amount, cur)}{_on(i.day)}, but the statement doesn't show it."
            text += (f" It was paid {transit} day{'s' if transit != 1 else ''} before the statement date and may still be"
                     " on its way." if transit is not None and transit <= 5 else
                     " Send the vendor the payment details and ask them to apply it.")
            specs.append(Spec(B.PAYMENT_NOT_APPLIED, f"payment:{fp}", i.label, effect=-i.owed,
                              shipmatch_amount=i.amount, item=i, data=info, explanation=text))

    # Balances
    lines_total = (stmt_opening or ZERO) + sum((ln.amount for ln in body), ZERO)
    statement_balance = statement.closing_balance if statement.closing_balance is not None else lines_total
    sm_opening = sum((i.owed for i in before if i.owed is not None), ZERO) if period_start else None
    shipmatch_balance = (sm_opening or ZERO) + sum((i.owed for i in scope if i.owed is not None), ZERO)
    if statement.closing_balance is not None and statement.closing_balance != lines_total:
        gap = statement.closing_balance - lines_total
        specs.append(Spec(B.ARITHMETIC, "arithmetic", "Statement total", effect=gap,
                          statement_amount=statement.closing_balance, shipmatch_amount=lines_total,
                          explanation=f"The statement's printed balance ({_money(statement.closing_balance, cur)}) is "
                                      f"not the sum of its lines ({_money(lines_total, cur)}). Check whether a line was "
                                      "missed when reading it, or ask the vendor."))
    if stmt_opening is not None:
        gap = stmt_opening - (sm_opening or ZERO)
        if gap:
            specs.append(Spec(B.OPENING, "opening", "Opening balance", effect=gap, statement_amount=stmt_opening,
                              shipmatch_amount=sm_opening or ZERO,
                              explanation=f"The statement starts from a balance of {_money(stmt_opening, cur)}; "
                                          f"ShipMatch's items before {_day(period_start)} add up to "
                                          f"{_money(sm_opening or ZERO, cur)}. Ask the vendor for an earlier statement "
                                          "to find the difference." if period_start else
                                          "The statement starts from a balance ShipMatch can't place in time."))
    difference = statement_balance - shipmatch_balance
    explained = sum((s.effect for s in specs), ZERO)
    counts, amounts = defaultdict(int), defaultdict(lambda: ZERO)
    for s in specs:
        counts[s.bucket] += 1
        amounts[s.bucket] += s.effect
    summary = {
        "currency": cur, "statement_date": day.isoformat(), "shape": "activity" if activity else "open_items",
        "period_start": period_start.isoformat() if period_start else "",
        "statement_balance": f"{statement_balance:.2f}", "shipmatch_balance": f"{shipmatch_balance:.2f}",
        "difference": f"{difference:.2f}", "explained": f"{explained:.2f}",
        "unexplained": f"{difference - explained:.2f}", "lines_total": f"{lines_total:.2f}",
        "statement_opening": f"{stmt_opening:.2f}" if stmt_opening is not None else "",
        "shipmatch_opening": f"{sm_opening:.2f}" if sm_opening is not None else "",
        "counts": dict(counts), "amounts": {k: f"{v:.2f}" for k, v in amounts.items()},
        "lines": len(body), "items": len(scope), "unread_documents": unread,
    }
    return specs, summary


def _day(day: date) -> str:
    return f"{day.day} {day:%b %Y}"


def _on(day: date | None) -> str:
    return f" on {_day(day)}" if day else ""


def _find(pool: list[Item], ln: StatementLine) -> tuple[Item | None, str]:
    key = norm_ref(ln.number)
    free = [i for i in pool if not i.matched]
    magnitude = abs(ln.amount)
    if key:
        same = [i for i in free if i.key and i.key == key]
        if same:
            same.sort(key=lambda i: (i.amount is None or abs(i.amount - magnitude) > TOLERANCE))
            return same[0], "number"
        digits = re.sub(r"\D", "", ln.number)
        if len(digits) >= 5:
            same = [i for i in free if i.digits == digits or (len(i.digits) >= 5 and
                                                              (i.digits.endswith(digits) or digits.endswith(i.digits)))]
            if len(same) == 1:
                return same[0], "digits"
    if ln.kind in (StatementLine.Kind.INVOICE, StatementLine.Kind.CREDIT):
        text = norm_ref(f"{ln.reference} {ln.number}")
        same = [i for i in free if i.bl and len(i.bl) >= 6 and i.bl in text and i.amount is not None
                and abs(i.amount - magnitude) <= TOLERANCE]
        if len(same) == 1:
            return same[0], "bl"
        if ln.kind == StatementLine.Kind.CREDIT:
            same = [i for i in free if i.dispute_id and i.amount is not None and abs(i.amount - magnitude) <= TOLERANCE]
            if len(same) == 1:
                return same[0], "amount"
    if ln.kind == StatementLine.Kind.PAYMENT:
        ref = norm_ref(ln.reference)
        same = [i for i in free if i.key and (i.key == ref or (ref and i.key in ref))]
        if len(same) == 1:
            return same[0], "reference"
        same = [i for i in free if i.amount is not None and abs(i.amount - magnitude) <= TOLERANCE
                and (not ln.date or not i.day or abs((ln.date - i.day).days) <= PAYMENT_WINDOW.days)]
        if same:
            same.sort(key=lambda i: abs((ln.date - i.day).days) if ln.date and i.day else 99)
            return same[0], "amount"
    return None, ""


def _same_content(body: list[StatementLine], ln: StatementLine, specs: list[Spec]) -> StatementLine | None:
    for other in body:
        if other.position >= ln.position:
            break
        if (other.kind == ln.kind and other.amount == ln.amount and other.date == ln.date and ln.date
                and norm_ref(other.reference) == norm_ref(ln.reference) and norm_ref(other.number) != norm_ref(ln.number)):
            return other
    return None


def _possible(pool: list[Item], ln: StatementLine) -> str:
    magnitude = abs(ln.amount)
    same = [i for i in pool if not i.matched and i.amount is not None and abs(i.amount - magnitude) <= TOLERANCE]
    if len(same) == 1:
        return f"ShipMatch has {same[0].label.lower()} for the same amount under another number; it may be this one."
    return ""


def _matched_spec(ln: StatementLine, item: Item, how: str, cur: str, base: dict) -> Spec:
    magnitude = abs(ln.amount)
    data = dict(item.info)
    data["how"] = how
    fp = item.fingerprint
    by = {"digits": " (number printed differently)", "bl": " (matched by B/L and amount; the numbers differ)",
          "reference": "", "amount": " (matched by amount and date)", "number": ""}.get(how, "")
    if item.amount is None:
        return Spec(B.AMOUNT_DIFFERS, f"differs:{fp}", item=item, data=data, effect=ln.amount,
                    explanation=f"Found in ShipMatch{by}, but it is in {item.currency} and there is no exchange rate to "
                                f"{cur} in Settings, so the amounts can't be compared.", **base)
    if item.note == "rejected":
        return Spec(B.AMOUNT_DIFFERS, f"differs:{fp}", item=item, data=data, shipmatch_amount=ZERO,
                    effect=ln.amount - (item.owed or ZERO),
                    explanation=f"You rejected shipment {data.get('shipment') or ''} in ShipMatch, so nothing is owed "
                                "on this invoice there, but the vendor still bills it. Tell the vendor why it was "
                                "rejected, or reopen the shipment.", **base)
    gap = magnitude - item.amount
    if abs(gap) <= TOLERANCE:
        return Spec(B.MATCHED, f"matched:{fp}", item=item, data=data, shipmatch_amount=item.amount, effect=ZERO,
                    explanation=f"Same {'amount' if how != 'amount' else 'amount and date'} in ShipMatch{by}.", **base)
    effect = ln.amount - (item.owed or ZERO)
    side = "more" if gap > 0 else "less"
    return Spec(B.AMOUNT_DIFFERS, f"differs:{fp}", item=item, data=data, shipmatch_amount=item.amount, effect=effect,
                explanation=f"The statement shows {_money(magnitude, cur)}, ShipMatch has {_money(item.amount, cur)}"
                            f"{by}: the vendor's figure is {_money(abs(gap), cur)} {side}. Ask for a corrected invoice "
                            "or a credit note, or correct the amount in ShipMatch if it was read wrong.", **base)


def _credits_applied(specs: list[Spec], credits: list[Item], cur: str) -> None:
    """An invoice on the statement that is lower by exactly a credit note naming it: the vendor applied it."""
    for s in specs:
        if s.bucket != B.AMOUNT_DIFFERS or s.item is None or s.item.kind != "invoice" or s.item.amount is None:
            continue
        if s.item.note == "rejected" or s.line is None:
            continue
        short = s.item.amount - abs(s.line.amount)
        if short <= 0:
            continue
        for c in credits:
            if c.matched or c.amount is None or not c.original or c.original != s.item.key:
                continue
            if abs(c.amount - short) <= TOLERANCE:
                c.matched = True
                s.bucket = B.MATCHED
                s.fingerprint = s.fingerprint.replace("differs:", "matched:")
                s.effect = s.line.amount - (s.item.owed or ZERO) + c.amount
                s.explanation = (f"The vendor applied {c.label.lower()} ({_money(c.amount, cur)}) to this invoice, "
                                 f"so it shows {_money(abs(s.line.amount), cur)} still open.")
                s.data["credit_applied"] = c.label
                break


def _settled(invoices: list[Item], payments: list[Item]) -> set[int]:
    """On an open-items statement: invoices ShipMatch knows were paid in full, with the payments that paid them."""
    out: set[int] = set()
    unmatched_invoices = [i for i in invoices if not i.matched and i.amount is not None]
    by_doc = {i.document.pk: i for i in unmatched_invoices if i.document is not None}
    by_key = {i.key: i for i in unmatched_invoices if i.key}
    for pay in payments:
        if pay.matched or pay.payment is None or pay.amount is None:
            continue
        allocs = pay.payment.allocations or []
        targets = []
        for a in allocs:
            target = by_doc.get(a.get("document_id")) or by_key.get(norm_ref(a.get("invoice_number")))
            if target is None:
                targets = []
                break
            targets.append(target)
        if not allocs:  # an unallocated payment that equals exactly one open invoice paid that invoice
            same = [i for i in unmatched_invoices if id(i) not in out and abs(i.amount - pay.amount) <= TOLERANCE
                    and (not i.day or not pay.day or i.day <= pay.day)]
            targets = same[:1] if len(same) == 1 else []
        if targets and abs(sum((t.amount for t in targets), ZERO) - pay.amount) <= TOLERANCE:
            out.add(id(pay))
            out.update(id(t) for t in targets)
    return out


# --------------------------------------------------------------------------- storing


@transaction.atomic
def run(statement: VendorStatement) -> VendorStatement:
    """Match the statement again and store the result. Items a person resolved keep their resolution while
    the same finding is still there."""
    org = statement.organization
    if not statement.vendor_key:
        statement.status = VendorStatement.Status.NEEDS_VENDOR
        statement.save(update_fields=["status", "updated_at"])
        return statement
    specs, summary = compare(org, statement)
    existing = {i.fingerprint: i for i in statement.items.all()}
    keep = set()
    for s in specs:
        fp = s.fingerprint[:200]
        if fp in keep:  # two findings with one fingerprint (same number twice): keep both apart
            fp = f"{fp}:{len(keep)}"[:200]
        keep.add(fp)
        values = dict(bucket=s.bucket, label=s.label[:200], line=s.line,
                      document=s.item.document if s.item else None, payment=s.item.payment if s.item else None,
                      statement_amount=s.statement_amount, shipmatch_amount=s.shipmatch_amount, effect=s.effect,
                      explanation=s.explanation[:600], data=_jsonable(s.data))
        item = existing.get(fp)
        if item is None:
            ReconItem.objects.create(statement=statement, fingerprint=fp, **values)
        else:
            for k, v in values.items():
                setattr(item, k, v)
            item.save()
    statement.items.exclude(fingerprint__in=keep).delete()
    statement.summary = summary
    statement.reconciled_at = timezone.now()
    statement.status = VendorStatement.Status.READY
    statement.save(update_fields=["summary", "reconciled_at", "status", "updated_at"])
    return statement


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# --------------------------------------------------------------------------- asking for a copy


def vendor_email(org, vk: str) -> str:
    """Where to ask a vendor for a copy: the billing contact remembered from disputes, else the address their
    documents were emailed from."""
    from apps.disputes.models import VendorContact

    contact = VendorContact.objects.filter(organization=org, vendor_key=vk).first()
    if contact and contact.email:
        return contact.email
    for d in (Document.objects.filter(organization=org, email__isnull=False, fields__name="vendor_name")
              .select_related("email").order_by("-received_at")[:500]):
        if vendor_key(d.field("vendor_name")) == vk and d.email.sender:
            m = re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", d.email.sender)
            if m:
                return m.group(0)
    return ""


def copy_request_mailto(org, statement: VendorStatement, item: ReconItem) -> str:
    """A mailto: link with a ready-to-send request for a copy of a missing invoice or credit note. Nothing is
    sent by ShipMatch: the person's own mail program opens the draft."""
    from apps.disputes.models import DisputeSettings

    line = item.line
    cur = (statement.currency or org.home_currency).upper()
    what = "credit note" if line and line.kind == StatementLine.Kind.CREDIT else "invoice"
    number = line.number if line and line.number else "(no number)"
    reply_to = DisputeSettings.objects.filter(organization=org).values_list("reply_to", flat=True).first() or ""
    details = [f"{what.capitalize()} number: {number}"]
    if line and line.date:
        details.append(f"Date: {_day(line.date)}")
    if line:
        details.append(f"Amount: {cur} {abs(line.amount):,.2f}")
    if line and line.reference:
        details.append(f"Reference: {line.reference}")
    stated = f" dated {_day(statement.statement_date)}" if statement.statement_date else ""
    body = (f"Hello,\n\nYour statement{stated} lists the following {what}, which we have not received:\n\n"
            + "\n".join(f"  {d}" for d in details)
            + f"\n\nPlease send us a copy{f' at {reply_to}' if reply_to else ''} so we can process it.\n\n"
              f"Thank you,\n{org.name}\n")
    subject = f"Copy of {what} {number} requested"
    to = vendor_email(org, statement.vendor_key)
    return f"mailto:{quote(to)}?subject={quote(subject)}&body={quote(body)}"
