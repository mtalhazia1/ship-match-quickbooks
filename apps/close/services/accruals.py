"""Month-end accruals: freight the organization owes for a period that isn't in the books yet.

For a period end date P the report has two kinds of lines.

Received, not booked. Every invoice and credit note ShipMatch has received that belongs to the period
but is not booked in it. It belongs to the period when it is dated on or before P (no date: received on
or before P), or when its shipment shipped on or before P (the service was in the period even if the
invoice is dated later). It is booked in the period when it was posted to QuickBooks with a bill date on
or before P (the bill date is the invoice date). Amount: the invoice itself (a credit note is negative).

Not yet invoiced. Every shipment that shipped on or before P (on-board date printed on the bill of
lading, else the B/L issue date) and within the look-back window, for each charge group the organization
expects (Settings: ocean freight, destination, delivery) that none of its freight invoices bills yet:
an estimate (see estimates.py) with its method, basis and confidence, or the amount a person entered.

Not counted, and listed with the reason: rejected shipments, possible duplicate invoices, shipments with
no bill of lading date, shipments older than the look-back window, charges a person marked as not needed.

The report is computed from what ShipMatch knows when it runs, so invoices that arrived after the period
end replace estimates with real amounts. Locking a period stores the report as it is (a version), and the
journal entry is exported from that stored version.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounting.models import PostedBill, VendorMapping, vendor_key
from apps.documents.models import Document
from apps.documents.services.normalize import parse_date
from apps.shipments.models import Shipment

from .. import groups
from ..models import AccrualAdjustment, AccrualSnapshot, CloseSettings
from . import estimates
from .history import Book, ShipmentFacts, dec, load

ZERO = Decimal("0.00")
RECEIVED, ESTIMATE = "received", "estimate"
KIND_LABELS = {RECEIVED: "Received, not booked", ESTIMATE: "Not yet invoiced"}
UNKNOWN_VENDOR = "Vendor not known yet"
STATUS_TEXT = {
    Shipment.Status.OPEN: "In review", Shipment.Status.NEEDS_REVIEW: "In review",
    Shipment.Status.READY: "Ready to approve", Shipment.Status.APPROVED: "Approved, not posted",
    Shipment.Status.POSTED: "Posted", Shipment.Status.REJECTED: "Rejected",
}


def _day(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def previous_month_end(today: date | None = None) -> date:
    today = today or timezone.localdate()
    return today.replace(day=1) - timedelta(days=1)


def recent_month_ends(count: int = 12, today: date | None = None) -> list[date]:
    out, day = [], previous_month_end(today)
    for _ in range(count):
        out.append(day)
        day = day.replace(day=1) - timedelta(days=1)
    return out


@dataclass
class Report:
    period_end: date
    currency: str
    lines: list[dict] = field(default_factory=list)
    left_out: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    generated_at: str = ""

    def as_dict(self) -> dict:
        lines = sorted(self.lines, key=lambda ln: (ln["kind"] != ESTIMATE, ln["vendor_name"].lower(),
                                                 ln.get("shipment_ref") or "", ln.get("document_name") or "",
                                                 ln["group"]))
        return {
            "period_end": self.period_end.isoformat(), "currency": self.currency, "generated_at": self.generated_at,
            "lines": lines, "totals": totals(lines), "by_vendor": by_vendor(lines), "by_account": by_account(lines),
            "left_out": self.left_out, "warnings": self.warnings, "settings": self.settings,
        }


# --------------------------------------------------------------------------- building


def build(org, period_end: date) -> dict:
    """The live accrual report for one period, as a JSON-ready dict."""
    cfg = CloseSettings.for_org(org)
    book = load(org)
    report = Report(period_end=period_end, currency=org.home_currency, settings=cfg.as_dict(),
                    generated_at=timezone.now().isoformat())
    accounts = Accounts(org, cfg)
    adjustments = {(a.shipment_id, a.group): a for a in
                   AccrualAdjustment.objects.filter(organization=org).select_related("created_by")}

    _received(book, report, accounts, period_end)
    _not_invoiced(book, report, accounts, cfg, adjustments, period_end)

    missing_fx = sorted({ln["currency"] for ln in report.lines if ln["amount"] is not None and ln["amount_home"] is None})
    if missing_fx:
        report.warnings.append(f"No exchange rate in Settings for {', '.join(missing_fx)}: those lines are listed "
                               "but not in the totals or the journal entry. Add the rate and run the report again.")
    no_amount = [ln for ln in report.lines if ln["amount"] is None]
    if no_amount:
        report.warnings.append(f"{len(no_amount)} line{'s have' if len(no_amount) != 1 else ' has'} no amount yet "
                               "(no basis to estimate, or the invoice total wasn't read). Enter an amount or mark the "
                               "charge as not needed.")
    return report.as_dict()


class Accounts:
    """Which account each line is debited to: the vendor's account where one is set (Settings > QuickBooks or
    the review screen), else the organization's default for freight or for goods."""

    def __init__(self, org, cfg: CloseSettings):
        self.cfg = cfg
        self.mappings = {m.vendor_key: m for m in VendorMapping.objects.filter(organization=org)}

    def for_vendor(self, vk: str, goods: bool = False) -> tuple[str, str, str]:
        m = self.mappings.get(vk)
        if m and (m.expense_account_name or m.expense_account_id):
            return (m.expense_account_name or f"QuickBooks account {m.expense_account_id}", m.expense_account_id,
                    "vendor")
        return (self.cfg.goods_account if goods else self.cfg.freight_account), "", "default"


def _line(**kw) -> dict:
    base = {
        "kind": ESTIMATE, "shipment_id": None, "shipment_ref": "", "bl_number": "", "ship_date": "",
        "document_id": None, "document_name": "", "doc_type": "", "invoice_number": "", "invoice_date": "",
        "vendor_name": UNKNOWN_VENDOR, "vendor_key": "", "group": "", "group_label": "", "currency": "",
        "amount": None, "amount_home": None, "method": "", "method_label": "", "confidence": None,
        "confidence_label": "", "basis": "", "account_name": "", "account_id": "", "account_source": "",
        "status": "", "adjustment_id": None,
    }
    base.update(kw)
    for key in ("amount", "amount_home"):
        if isinstance(base[key], Decimal):
            base[key] = f"{base[key]:.2f}"
    base["group_label"] = base["group_label"] or groups.label(base["group"])
    base["method_label"] = base["method_label"] or estimates.METHOD_LABELS.get(base["method"], "")
    base["confidence_label"] = estimates.confidence_label(base["confidence"], base["method"])
    return base


def _bill_date(doc: Document, data: dict) -> date:
    return parse_date(data.get("invoice_date")) or timezone.localdate(doc.received_at)


def _booked_in_period(doc: Document, data: dict, period_end: date) -> tuple[bool, str]:
    """(booked on or before the period end, status text)."""
    pb = getattr(doc, "posted_bill", None) if _has_posted_bill(doc) else None
    if pb is None or pb.status != PostedBill.Status.POSTED:
        return False, "Posting failed" if pb is not None and pb.status == PostedBill.Status.FAILED else ""
    txn = parse_date(data.get("invoice_date")) or (timezone.localdate(pb.posted_at) if pb.posted_at else None)
    if txn and txn <= period_end:
        return True, ""
    return False, f"Posted to QuickBooks with bill date {txn.day} {txn:%b %Y}" if txn else "Posted to QuickBooks"


def _has_posted_bill(doc: Document) -> bool:
    try:
        return doc.posted_bill is not None
    except PostedBill.DoesNotExist:
        return False


def _received(book: Book, report: Report, accounts: Accounts, period_end: date) -> None:
    org = book.org
    include_goods = accounts.cfg.include_goods
    for doc in book.documents:
        if not doc.posts_to_accounting:
            continue
        link = doc.match if hasattr(doc, "match") else None
        shipment = link.shipment if link else None
        facts = book.facts.get(shipment.pk) if shipment else None
        data = doc.data()
        bill_date = _bill_date(doc, data)
        shipped_in_period = bool(facts and facts.ship_date and facts.ship_date <= period_end)
        if bill_date > period_end and not shipped_in_period:
            continue
        booked, posted_text = _booked_in_period(doc, data, period_end)
        if booked:
            continue
        recent = bill_date >= period_end - timedelta(days=accounts.cfg.lookback_days)
        if shipment and shipment.pk in book.rejected:
            if recent:
                report.left_out.append(_left(shipment, doc, "Shipment rejected",
                                             "Its invoices are not owed as received."))
            continue
        if doc.pk in book.duplicates:
            if recent:
                report.left_out.append(_left(shipment, doc, "Possible duplicate invoice",
                                             "An open check says this may be a copy of an earlier invoice."))
            continue
        name = (data.get("vendor_name") or "").strip()
        vk = vendor_key(name)
        amount = dec(data.get("total_amount"))
        if amount is not None and doc.is_credit:
            amount = -abs(amount)
        cur = (data.get("currency") or org.home_currency).upper()
        home = org.to_home(amount, cur) if amount is not None else None
        goods = doc.doc_type == Document.DocType.COMMERCIAL_INVOICE
        if goods and not include_goods:
            continue
        account, account_id, source = accounts.for_vendor(vk, goods=goods)
        status = posted_text or (STATUS_TEXT.get(shipment.status, "") if shipment else "Not in a shipment yet")
        number = data.get("credit_note_number") if doc.is_credit else data.get("invoice_number")
        group = groups.GOODS if goods else _doc_group(facts, doc)
        if doc.is_credit:
            basis = "Credit note received and not booked in this period."
        elif bill_date > period_end:
            basis = (f"Invoice dated {_day(bill_date)}, after the period end, for a shipment that shipped "
                     f"{_day(facts.ship_date)}.")
        else:
            basis = "Invoice received and not booked in this period."
        if amount is None:
            basis += " The total wasn't read; open the document and enter it."
        report.lines.append(_line(
            kind=RECEIVED, shipment_id=shipment.pk if shipment else None,
            shipment_ref=shipment.reference if shipment else "", bl_number=shipment.bl_number if shipment else "",
            ship_date=facts.ship_date.isoformat() if facts and facts.ship_date else "",
            document_id=doc.pk, document_name=doc.original_filename, doc_type=doc.get_doc_type_display(),
            invoice_number=str(number or ""), invoice_date=bill_date.isoformat(),
            vendor_name=name or UNKNOWN_VENDOR, vendor_key=vk, group=group, currency=cur, amount=amount,
            amount_home=home, method=estimates.RECEIVED,
            method_label="Credit note received" if doc.is_credit else "Invoice received",
            confidence=1.0, basis=basis, account_name=account, account_id=account_id, account_source=source,
            status=status))


def _doc_group(facts: ShipmentFacts | None, doc: Document) -> str:
    """The charge group an invoice mostly bills (by amount), for the line's label."""
    if facts is None:
        return groups.OTHER if doc.is_credit else groups.FREIGHT
    parts = [p for p in facts.parts if p.document.pk == doc.pk]
    if not parts:
        return groups.OTHER if doc.is_credit else groups.FREIGHT
    return max(parts, key=lambda p: abs(p.amount)).group


def _left(shipment, doc, reason: str, detail: str = "") -> dict:
    return {"shipment_id": shipment.pk if shipment else None, "shipment_ref": shipment.reference if shipment else "",
            "document_id": doc.pk if doc else None, "document_name": doc.original_filename if doc else "",
            "reason": reason, "detail": detail}


def expected_groups(cfg: CloseSettings, facts: ShipmentFacts, book: Book) -> list[tuple[str, str]]:
    """(group, why) for each charge group this shipment should carry."""
    out = []
    for group in groups.EXPECTABLE:
        mode = cfg.expectation(group)
        if mode == CloseSettings.Expect.ALWAYS:
            out.append((group, "expected on every shipment"))
        elif mode == CloseSettings.Expect.USUAL:
            share, how = book.history.share(group, facts.destination_key, max(1, cfg.min_history))
            if share is not None and share >= 0.5:
                out.append((group, f"usual for similar shipments ({how})"))
    return out


def _not_invoiced(book: Book, report: Report, accounts: Accounts, cfg: CloseSettings,
                  adjustments: dict, period_end: date) -> None:
    org = book.org
    oldest = period_end - timedelta(days=cfg.lookback_days)
    for facts in sorted(book.facts.values(), key=lambda f: f.shipment.pk):
        s = facts.shipment
        if s.pk in book.rejected:
            if facts.ship_date and oldest <= facts.ship_date <= period_end and not any(
                    x["shipment_id"] == s.pk for x in report.left_out):
                report.left_out.append(_left(s, None, "Shipment rejected"))
            continue
        if not facts.ship_date:
            first = min((timezone.localdate(d.received_at) for d in facts.docs), default=None)
            near = first is not None and oldest <= first <= period_end + timedelta(days=31)
            gaps = [g for g, _ in expected_groups(cfg, facts, book) if g not in facts.invoiced_groups]
            if near and gaps and s.status != Shipment.Status.POSTED:
                report.left_out.append(_left(s, None, "No shipping date",
                                             "No bill of lading with an on-board or issue date, so ShipMatch can't "
                                             "tell whether it shipped by the period end. Not invoiced yet: "
                                             f"{', '.join(groups.label(g).lower() for g in gaps)}. Its received "
                                             "invoices are still accrued by their dates."))
            continue
        if facts.ship_date > period_end:
            continue
        missing = [(g, why) for g, why in expected_groups(cfg, facts, book) if g not in facts.invoiced_groups]
        if not missing:
            continue
        if facts.ship_date < oldest:
            report.left_out.append(_left(s, None, f"Shipped {_day(facts.ship_date)}, more than "
                                                  f"{cfg.lookback_days} days before the period end",
                                         f"Not invoiced yet: {', '.join(groups.label(g).lower() for g, _ in missing)}."
                                         " Ask the vendor whether a bill is still coming."))
            continue
        for group, why in missing:
            adj = adjustments.get((s.pk, group))
            common = dict(shipment_id=s.pk, shipment_ref=s.reference, bl_number=s.bl_number,
                          ship_date=facts.ship_date.isoformat(), group=group)
            if adj and adj.action == AccrualAdjustment.Action.EXCLUDE:
                who = adj.created_by.get_full_name() or adj.created_by.get_username() if adj.created_by else "someone"
                report.left_out.append({**_left(s, None, f"{groups.label(group)}: marked as not needed",
                                                f"{adj.note} ({who})"), "adjustment_id": adj.pk, "group": group})
                continue
            if adj and adj.action == AccrualAdjustment.Action.AMOUNT and adj.amount is not None:
                cur = adj.currency or org.home_currency
                vk = vendor_key(adj.vendor_name)
                account, account_id, source = accounts.for_vendor(vk)
                who = adj.created_by.get_full_name() or adj.created_by.get_username() if adj.created_by else "someone"
                report.lines.append(_line(
                    **common, vendor_name=adj.vendor_name or UNKNOWN_VENDOR, vendor_key=vk, currency=cur,
                    amount=adj.amount, amount_home=org.to_home(adj.amount, cur), method=estimates.MANUAL,
                    confidence=1.0, basis=f"Entered by {who} on {_day(timezone.localdate(adj.created_at))}: "
                                          f"{adj.note}", account_name=account, account_id=account_id,
                    account_source=source, status=_why_missing(facts, why), adjustment_id=adj.pk))
                continue
            est = estimates.estimate(org, facts, group, book.history, max(1, cfg.min_history))
            account, account_id, source = accounts.for_vendor(est.vendor_key)
            report.lines.append(_line(
                **common, vendor_name=est.vendor_name or UNKNOWN_VENDOR, vendor_key=est.vendor_key,
                currency=est.currency or org.home_currency, amount=est.amount, amount_home=est.amount_home,
                method=est.method, confidence=est.confidence if est.method != estimates.NONE else None,
                basis=est.basis, account_name=account, account_id=account_id, account_source=source,
                status=_why_missing(facts, why)))


def _why_missing(facts: ShipmentFacts, why: str) -> str:
    billed = sorted(facts.invoiced_groups - {groups.OTHER})
    if billed:
        return f"Partly invoiced: {', '.join(groups.label(g).lower() for g in billed)} billed so far"
    return "No freight invoice yet"


# --------------------------------------------------------------------------- totals


def _sum(lines, key="amount_home") -> Decimal:
    return sum((Decimal(ln[key]) for ln in lines if ln.get(key) is not None), ZERO)


def counted(lines: list[dict]) -> list[dict]:
    """Lines that go into totals and the journal entry: they have an amount in the home currency."""
    return [ln for ln in lines if ln.get("amount_home") is not None]


def totals(lines: list[dict]) -> dict:
    c = counted(lines)
    return {
        "total": f"{_sum(c):.2f}",
        "received": f"{_sum([ln for ln in c if ln['kind'] == RECEIVED]):.2f}",
        "estimated": f"{_sum([ln for ln in c if ln['kind'] == ESTIMATE]):.2f}",
        "lines": len(lines), "counted": len(c),
        "received_lines": sum(1 for ln in lines if ln["kind"] == RECEIVED),
        "estimated_lines": sum(1 for ln in lines if ln["kind"] == ESTIMATE),
        "not_counted": len(lines) - len(c),
        "shipments": len({ln["shipment_id"] for ln in lines if ln["shipment_id"]}),
    }


def by_vendor(lines: list[dict]) -> list[dict]:
    rows: dict[str, dict] = {}
    for ln in counted(lines):
        key = ln["vendor_key"] or ln["vendor_name"]
        r = rows.setdefault(key, {"vendor_name": ln["vendor_name"], "received": ZERO, "estimated": ZERO, "lines": 0})
        r["received" if ln["kind"] == RECEIVED else "estimated"] += Decimal(ln["amount_home"])
        r["lines"] += 1
    out = [{**r, "total": f"{r['received'] + r['estimated']:.2f}", "received": f"{r['received']:.2f}",
            "estimated": f"{r['estimated']:.2f}"} for r in rows.values()]
    return sorted(out, key=lambda r: (-Decimal(r["total"]), r["vendor_name"]))


def by_account(lines: list[dict]) -> list[dict]:
    rows: dict[tuple, dict] = {}
    for ln in counted(lines):
        key = (ln["account_name"], ln["account_id"])
        r = rows.setdefault(key, {"account_name": ln["account_name"], "account_id": ln["account_id"],
                                  "account_source": ln["account_source"], "total": ZERO, "lines": 0})
        r["total"] += Decimal(ln["amount_home"])
        r["lines"] += 1
    out = [{**r, "total": f"{r['total']:.2f}"} for r in rows.values()]
    return sorted(out, key=lambda r: (-Decimal(r["total"]), r["account_name"]))


# --------------------------------------------------------------------------- locking


class LockError(RuntimeError):
    pass


def latest(org, period_end: date) -> AccrualSnapshot | None:
    return AccrualSnapshot.objects.filter(organization=org, period_end=period_end).order_by("-version").first()


@transaction.atomic
def lock(org, period_end: date, user, note: str = "", expected_version: int | None = None) -> AccrualSnapshot:
    """Store the report as the next version for this period. `expected_version` is the latest version the
    person saw; if someone locked in the meantime the lock is refused instead of silently stacking."""
    from apps.core.utils import audit
    from apps.shipments.services.matching import lock_org

    lock_org(org.pk)  # one lock at a time per organization
    current = latest(org, period_end)
    current_version = current.version if current else 0
    if expected_version is not None and expected_version != current_version:
        raise LockError(f"Version {current_version} of this period was locked while you were looking at it. "
                        "Check the new version before locking again.")
    note = (note or "").strip()
    if current and not note:
        raise LockError(f"Version {current_version} is already locked. Say why the booked amount changes, so "
                        "auditors can follow it.")
    report = build(org, period_end)
    snap = AccrualSnapshot.objects.create(
        organization=org, period_end=period_end, version=current_version + 1, report=report,
        total=Decimal(report["totals"]["total"]), currency=report["currency"], line_count=len(report["lines"]),
        note=note[:500], locked_by=user)
    audit(org, "close.period_locked", snap, actor=user, period=period_end.isoformat(), version=snap.version,
          total=f"{snap.currency} {snap.total:,.2f}", lines=snap.line_count, checksum=snap.checksum,
          previous_total=f"{current.total:.2f}" if current else None, note=note[:300])
    return snap


def compare(old: dict, new: dict) -> dict:
    """What changed between two reports, by line key, for the 'since version N' summary."""
    def key(ln):
        return (ln["kind"], ln.get("document_id"), ln.get("shipment_id"), ln["group"])

    a = {key(ln): ln for ln in old.get("lines", [])}
    b = {key(ln): ln for ln in new.get("lines", [])}
    added = [b[k] for k in b if k not in a]
    removed = [a[k] for k in a if k not in b]
    changed = [(a[k], b[k]) for k in a.keys() & b.keys() if a[k].get("amount_home") != b[k].get("amount_home")]
    return {
        "added": len(added), "removed": len(removed), "changed": len(changed),
        "difference": f"{Decimal(new['totals']['total']) - Decimal(old['totals']['total']):.2f}",
    }
