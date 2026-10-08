"""Payment status of posted bills: read back from QuickBooks or Xero and kept on each PostedBill.

`sync(org)` reads, in batches, every posted bill whose payment isn't settled yet (paid bills are read again
weekly for 90 days, in case a payment is reversed), stores what the system reports (unpaid, partly paid,
paid, voided, deleted; amounts, due date, payments) and writes an audit row for each change. A bill voided or
deleted in the accounting system raises the "bill voided or deleted" alert. The scheduled run
(apps.accounting.tasks.sync_all_payments) reads each organization at most once per PAYMENT_SYNC_HOURS; "Check
payments now" runs it on demand. Only one check per organization runs at a time, and a used-up daily API
allowance (Xero) pauses the organization's checks until Xero allows calls again.

`aging(org)` is the accounts payable aging summary for the dashboard, `summaries(shipments)` and
`filter_shipments(qs, key)` serve the shipment list.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from apps.accounting.models import PaymentSync, PostedBill
from apps.core.utils import audit

from .providers import PaymentInfo, provider_for

log = logging.getLogger(__name__)
P = PostedBill.Payment
OPEN = (P.UNPAID, P.PARTLY_PAID)
GONE = (P.VOIDED, P.DELETED)
MANUAL_COOLDOWN = timedelta(seconds=60)      # a second "Check payments now" within a minute does nothing
LOCK_TIMEOUT = timedelta(minutes=15)         # a check that died (worker killed) stops blocking after this
RECHECK_PAID_EVERY = timedelta(days=7)
RECHECK_PAID_FOR = timedelta(days=90)
MAX_BILLS_PER_CHECK = 1000                   # the least recently checked first; the rest wait for the next run


def auto_interval() -> timedelta:
    return timedelta(hours=max(1, int(getattr(settings, "PAYMENT_SYNC_HOURS", 1))))


# --------------------------------------------------------------------------- reading


def candidates(org, provider, shipment=None):
    """Posted bills to read from `provider`'s system (and the same company or organisation in it)."""
    qs = (PostedBill.objects.filter(organization=org, status=PostedBill.Status.POSTED, system=provider.key)
          .exclude(qbo_bill_id="").filter(Q(ledger_id=provider.ledger_id) | Q(ledger_id="")))
    if shipment is not None:
        return qs.filter(shipment=shipment).exclude(payment_status=P.DELETED)
    now = timezone.now()
    due = Q(payment_status__in=["", *OPEN]) | Q(
        payment_status=P.PAID, payment_changed_at__gte=now - RECHECK_PAID_FOR,
        payment_checked_at__lt=now - RECHECK_PAID_EVERY)
    return qs.filter(due).order_by(F("payment_checked_at").asc(nulls_first=True), "pk")


def sync(org, *, automatic: bool = False, actor=None, shipment=None, provider=None) -> dict:
    """Read payment statuses for one organization. Returns a summary; never raises for API problems."""
    summary = {"checked": 0, "changed": 0, "paid": 0, "gone": 0, "skipped": "", "error": "", "system": ""}
    state, _ = PaymentSync.objects.get_or_create(organization=org)
    now = timezone.now()
    if state.paused_until and state.paused_until > now:
        summary["skipped"] = "paused"
        summary["error"] = state.last_error
        return summary
    if automatic and state.last_auto_at and now - state.last_auto_at < auto_interval():
        summary["skipped"] = "recent"
        return summary
    if not automatic and shipment is None and state.last_started_at and now - state.last_started_at < MANUAL_COOLDOWN:
        summary["skipped"] = "just_checked"
        return summary
    stamps = {"running_since": now, "last_started_at": now}
    if automatic:
        stamps["last_auto_at"] = now
    claimed = (PaymentSync.objects.filter(pk=state.pk)
               .filter(Q(running_since__isnull=True) | Q(running_since__lt=now - LOCK_TIMEOUT)).update(**stamps))
    if not claimed:
        summary["skipped"] = "running"
        return summary

    paused_until, error = None, ""
    try:
        provider = provider or provider_for(org)
        if provider is None:
            summary["skipped"] = "not_connected"
        elif provider.conn.needs_reconnect:
            summary["skipped"] = "needs_reconnect"
            error = f"{provider.name} needs to be connected again before payments can be checked."
        else:
            summary["system"] = provider.name
            bills = list(candidates(org, provider, shipment).select_related("document", "shipment")[:MAX_BILLS_PER_CHECK])
            infos = provider.payment_statuses(bills) if bills else {}
            checked_at = timezone.now()
            for pb in bills:
                info = infos.get(pb.pk)
                if info is None:
                    continue
                summary["checked"] += 1
                if apply(pb, info, provider.name, checked_at):
                    summary["changed"] += 1
                    summary["paid"] += pb.payment_status == P.PAID
                    summary["gone"] += pb.is_gone
    except Exception as e:  # an API problem must never break the scheduler or the page
        errors = (*provider.stop_errors, *provider.errors) if provider is not None else ()
        if not errors or not isinstance(e, errors):
            log.exception("Payment check failed for %s", org.slug)
            error = f"The payment check stopped unexpectedly ({type(e).__name__}). It runs again at the next check."
        else:
            error = provider.explain(e)
            if getattr(e, "daily", False):
                paused_until = timezone.now() + timedelta(seconds=max(60.0, float(getattr(e, "retry_after", 0) or 0)))
        summary["error"] = error
    finally:
        PaymentSync.objects.filter(pk=state.pk).update(
            running_since=None, last_finished_at=timezone.now(), last_summary=summary, last_error=error[:500],
            paused_until=paused_until)
    return summary


def apply(pb: PostedBill, info: PaymentInfo, system: str, now=None) -> bool:
    """Store what the accounting system reported. Returns True if the payment status changed."""
    now = now or timezone.now()
    before = pb.payment_status
    pb.payment_status = info.status
    if info.total is not None:
        pb.amount_total = info.total
    pb.amount_paid, pb.amount_due = info.paid, info.due
    if info.status in GONE:
        pb.amount_due = Decimal("0.00")
    pb.currency = (info.currency or pb.currency or "")[:3].upper()
    if info.due_date is not None:
        pb.due_date = info.due_date
    pb.paid_on = info.paid_on
    pb.payments = info.payments or []
    pb.payment_checked_at, pb.payment_error = now, ""
    changed = before != info.status
    if changed:
        pb.payment_changed_at = now
    pb.save(update_fields=["payment_status", "amount_total", "amount_paid", "amount_due", "currency", "due_date",
                           "paid_on", "payments", "payment_checked_at", "payment_error", "payment_changed_at"])
    if not changed or (not before and info.status == P.UNPAID):
        return changed   # the first check finding an unpaid bill isn't news
    what = "vendor credit" if pb.is_credit else "bill"
    data = {"system": system, "what": what, "number": pb.display_number, "status": pb.payment_label.lower(),
            "previous": before, "shipment": pb.shipment.reference, "amount_paid": pb.amount_paid,
            "paid_on": pb.paid_on, "currency": pb.currency}
    audit(pb.organization, "bill.payment_updated", pb.document, **data)
    if pb.is_gone and before not in GONE:
        audit(pb.organization, "bill.voided_in_accounting", pb.document, **data)
    return changed


# --------------------------------------------------------------------------- shipment list


FILTERS = [("", "All payments"), ("unpaid", "Not paid yet"), ("overdue", "Overdue"), ("paid", "Paid"),
           ("problem", "Voided or deleted"), ("not_checked", "Not checked yet")]
RANK = {"problem": 0, "overdue": 1, "partly_paid": 2, "unpaid": 3, "not_checked": 4, "paid": 5}
CELL = {
    "problem": ("Voided or deleted", "err"), "overdue": ("Overdue", "err"), "partly_paid": ("Partly paid", "warn"),
    "unpaid": ("Unpaid", "neutral"), "not_checked": ("Not checked yet", "neutral"), "paid": ("Paid", "ok"),
}


def _bills(**extra):
    return PostedBill.objects.filter(shipment=OuterRef("pk"), status=PostedBill.Status.POSTED,
                                     kind=PostedBill.Kind.BILL, **extra)


def filter_shipments(qs, key: str):
    """Narrow a Shipment queryset by the payment status of its posted bills (FILTERS keys)."""
    today = timezone.localdate()
    if key == "unpaid":
        return qs.filter(Exists(_bills(payment_status__in=OPEN)))
    if key == "overdue":
        return qs.filter(Exists(_bills(payment_status__in=OPEN, due_date__lt=today)))
    if key == "paid":
        return qs.filter(Exists(_bills())).exclude(Exists(_bills().exclude(payment_status=P.PAID)))
    if key == "problem":
        return qs.filter(Exists(_bills(payment_status__in=GONE)))
    if key == "not_checked":
        return qs.filter(Exists(_bills(payment_status="")))
    return qs


def _key(pb_status: str, due_date, today) -> str:
    if pb_status in GONE:
        return "problem"
    if pb_status in OPEN and due_date is not None and due_date < today:
        return "overdue"
    return pb_status or "not_checked"


def summaries(shipments) -> dict[int, dict]:
    """{shipment id: {key, label, tone, count}} for the shipments that have posted bills (one query)."""
    ids = [s.pk for s in shipments]
    if not ids:
        return {}
    today = timezone.localdate()
    worst: dict[int, str] = {}
    counts: dict[int, int] = {}
    for sid, status, due in (PostedBill.objects.filter(shipment_id__in=ids, status=PostedBill.Status.POSTED,
                                                       kind=PostedBill.Kind.BILL)
                             .values_list("shipment_id", "payment_status", "due_date")):
        key = _key(status, due, today)
        counts[sid] = counts.get(sid, 0) + 1
        if sid not in worst or RANK[key] < RANK[worst[sid]]:
            worst[sid] = key
    return {sid: {"key": key, "label": CELL[key][0], "tone": CELL[key][1], "count": counts[sid]}
            for sid, key in worst.items()}


# --------------------------------------------------------------------------- aging


BUCKETS = [("current", "Not due yet", 0, 0), ("1_30", "1 to 30 days", 1, 30), ("31_60", "31 to 60 days", 31, 60),
           ("61_90", "61 to 90 days", 61, 90), ("90_plus", "Over 90 days", 91, None)]


@dataclass
class Bucket:
    key: str
    label: str
    count: int = 0
    amount: Decimal = Decimal("0.00")    # home currency
    share: int = 0                       # percent of the total, for the bar


@dataclass
class Aging:
    currency: str
    buckets: list[Bucket]
    total: Decimal = Decimal("0.00")
    count: int = 0
    overdue: Decimal = Decimal("0.00")
    unconverted: dict = field(default_factory=dict)    # {currency: amount} with no exchange rate in Settings
    credits_open: dict = field(default_factory=dict)    # {currency: amount} of vendor credits not used yet
    not_checked: int = 0
    posted: int = 0                                     # posted bills and credits of any status
    last_checked: object = None
    error: str = ""
    paused_until: object = None


def aging(org, today=None) -> Aging:
    """Unpaid posted bills by days past their due date, in the home currency."""
    today = today or timezone.localdate()
    result = Aging(org.home_currency, [Bucket(k, label) for k, label, _, _ in BUCKETS])
    by_key = {b.key: b for b in result.buckets}
    posted = PostedBill.objects.filter(organization=org, status=PostedBill.Status.POSTED)
    for pb in posted.filter(kind=PostedBill.Kind.BILL, payment_status__in=OPEN).select_related("document"):
        amount = pb.amount_due if pb.amount_due is not None else pb.amount_total
        if amount is None or amount <= 0:
            continue
        currency = (pb.currency or org.home_currency).upper()
        days = (today - pb.due_date).days if pb.due_date else 0   # no due date: treated as not due yet
        key = next(k for k, _, low, high in BUCKETS if low <= max(days, 0) and (high is None or max(days, 0) <= high))
        bucket = by_key[key]
        home = org.to_home(amount, currency)
        if home is None:   # listed apart, so the buckets only add up what they can convert
            result.unconverted[currency] = result.unconverted.get(currency, Decimal("0.00")) + amount
            continue
        bucket.count += 1
        result.count += 1
        bucket.amount += home
        result.total += home
        if key != "current":
            result.overdue += home
    for pb in posted.filter(kind=PostedBill.Kind.VENDOR_CREDIT, payment_status__in=OPEN):
        if pb.amount_due:
            cur = (pb.currency or org.home_currency).upper()
            result.credits_open[cur] = result.credits_open.get(cur, Decimal("0.00")) + pb.amount_due
    top = max((b.amount for b in result.buckets), default=Decimal("0"))
    for b in result.buckets:
        b.share = int(round(100 * b.amount / top)) if top else 0
    result.not_checked = posted.filter(payment_status="").exclude(qbo_bill_id="").count()
    result.posted = posted.count()
    state = PaymentSync.objects.filter(organization=org).first()
    if state:
        result.last_checked, result.error = state.last_finished_at, state.last_error
        result.paused_until = state.paused_until if state.paused_until and state.paused_until > timezone.now() else None
    return result
