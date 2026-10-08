"""Money caught by validation, and what happened to it.

    summary(org, start, end) -> Savings

Source: the CaughtCharge ledger (apps/rates/ledger.py). Every validation issue with an
`amount_at_risk` (over-quote charges, unapproved extra charges, duplicates, totals that don't add
up, unusual amounts ...) becomes one catch, identified by its document and subject, so a catch
survives validation re-creating its issue.

Outcome rules. Each catch's money lands in these buckets (amounts converted to the home
currency with the organization's exchange rates):

  1. Prevented: the money was not paid.
     a. The shipment was rejected: the whole amount caught.
     b. The check no longer finds the problem because the invoice was corrected, replaced or
        removed from the shipment: the whole amount caught.
     c. The amount went down while the issue stayed (a corrected invoice still over the quote):
        the reduction, amount_caught - amount_latest. The rest goes to 2 or 3.
  2. Still at risk: the issue is open and the shipment is not approved, posted or rejected:
     amount_latest.
  3. Accepted: a reviewer accepted the warning or an approver overrode the error, or the shipment
     was approved or posted with the issue still open (warnings don't block approval):
     amount_latest. The charge was paid as invoiced.
  4. Withdrawn: the check stopped finding it because a quote, an approved extra charge or the
     tolerance changed, not the invoice. Not counted as caught or saved; shown separately so a
     data-entry fix is never reported as money saved. (A reduction from an earlier invoice
     correction still counts as prevented.) While a catch stays open, a rate change moves its
     caught amount by the same difference, for the same reason.

  caught    = prevented + still at risk + accepted
  recovered = money returned after payment, reported by RECOVERY_SOURCES
  saved     = prevented + recovered

Overlaps. Several checks can flag the same money on one invoice; it is counted once:
  * A duplicate invoice counts for the whole invoice; other catches on that same document are
    listed but not added ("already counted in another check").
  * An unusual-amount warning counts only for the part not already caught by charge-level checks
    on the same invoice (charged more than the quote, extra charge not approved): if those add
    up to more, the warning adds nothing. Each catch's outcome split is scaled by the share
    that counts.

Period: a catch belongs to the period, and the month, in which it was first caught, so totals for
a past month only change when an outcome changes, never because a check ran again.

Known limit: when a reviewer corrects a value that was misread (not wrong on the invoice), the
issue it raised also counts as prevented. ShipMatch can't tell a reading fix from a vendor fix.

Extension point: a disputes app (or anything that gets money back after payment) registers a
callable with `register_recovery_source(fn)`. It is called as `fn(org, start, end)` and returns a
Decimal in the home currency or an iterable of Recovery (or dicts with the same keys). Report only
money returned for charges that were paid, so nothing is counted twice.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import prefetch_related_objects
from django.utils import timezone

from apps.shipments.labels import issue_title
from apps.shipments.models import Shipment, ValidationIssue

from . import ledger
from .models import CaughtCharge

log = logging.getLogger(__name__)
ZERO = Decimal("0.00")
OUTCOMES = ("prevented", "at_risk", "accepted", "withdrawn", "overlap")
OUTCOME_LABELS = {"prevented": "Prevented", "at_risk": "Still at risk", "accepted": "Accepted", "withdrawn": "Withdrawn",
                  "overlap": "Already counted in another check"}
WHOLE_INVOICE = {"duplicate_invoice"}
LINE_LEVEL = {"over_quote", "unapproved_accessorial"}
ESTIMATES = {"amount_outlier"}
PAID_STATUSES = {Shipment.Status.APPROVED, Shipment.Status.POSTED}


@dataclass
class Recovery:
    amount: Decimal
    currency: str = ""
    vendor_name: str = ""
    code: str = ""
    on: date | None = None
    label: str = ""


RECOVERY_SOURCES: list[Callable] = []


def register_recovery_source(fn: Callable) -> None:
    if fn not in RECOVERY_SOURCES:
        RECOVERY_SOURCES.append(fn)


@dataclass
class Row:
    key: str
    label: str
    caught: Decimal = ZERO
    prevented: Decimal = ZERO
    at_risk: Decimal = ZERO
    accepted: Decimal = ZERO
    count: int = 0

    def add(self, split: dict[str, Decimal]) -> None:
        self.prevented += split.get("prevented", ZERO)
        self.at_risk += split.get("at_risk", ZERO)
        self.accepted += split.get("accepted", ZERO)
        self.caught = self.prevented + self.at_risk + self.accepted
        self.count += 1

    def pct(self, part: str) -> float:
        return float(getattr(self, part) / self.caught * 100) if self.caught else 0.0


@dataclass
class CatchRow:
    catch: CaughtCharge
    title: str
    outcome: str
    split: dict[str, Decimal]        # home currency
    amount: Decimal | None           # caught, home currency (None = no exchange rate)
    issue: ValidationIssue | None = None
    resolved_note: str = ""
    share: Decimal = Decimal("1")    # part of the catch that counts (see Overlaps)

    @property
    def partly_counted(self) -> bool:
        return 0 < self.share < 1

    @property
    def outcome_label(self) -> str:
        if self.outcome != "prevented" and self.split.get("prevented"):
            return f"{OUTCOME_LABELS[self.outcome]}, partly prevented"
        return OUTCOME_LABELS[self.outcome]


@dataclass
class Savings:
    start: date
    end: date
    currency: str
    caught: Decimal = ZERO
    prevented: Decimal = ZERO
    at_risk: Decimal = ZERO
    accepted: Decimal = ZERO
    withdrawn: Decimal = ZERO
    recovered: Decimal = ZERO
    counts: dict = field(default_factory=lambda: dict.fromkeys(OUTCOMES, 0))
    by_code: list[Row] = field(default_factory=list)
    by_vendor: list[Row] = field(default_factory=list)
    by_month: list[Row] = field(default_factory=list)
    catches: list[CatchRow] = field(default_factory=list)
    unconverted: dict[str, Decimal] = field(default_factory=dict)
    recoveries: list[Recovery] = field(default_factory=list)
    recovery_sources: int = 0

    @property
    def saved(self) -> Decimal:
        return self.prevented + self.recovered

    @property
    def count(self) -> int:
        return self.counts["prevented"] + self.counts["at_risk"] + self.counts["accepted"]

    @property
    def top_vendors(self) -> list[Row]:
        return [r for r in self.by_vendor if r.caught > 0][:5]

    @property
    def month_max(self) -> Decimal:
        return max((r.caught for r in self.by_month), default=ZERO)

    @property
    def code_max(self) -> Decimal:
        return max((r.caught for r in self.by_code), default=ZERO)

    @property
    def vendor_max(self) -> Decimal:
        return max((r.caught for r in self.by_vendor), default=ZERO)


# --------------------------------------------------------------------------- outcome rules


def split_catch(c: CaughtCharge, issue: ValidationIssue | None, shipment_status: str | None) -> tuple[str, dict]:
    """(main outcome, {bucket: amount in the catch's currency}) following the rules above."""
    caught, latest = c.amount_caught, min(c.amount_latest, c.amount_caught)
    if shipment_status == Shipment.Status.REJECTED:
        return "prevented", {"prevented": caught}
    if issue is None:
        if c.cleared_reason == CaughtCharge.Cleared.RATES:
            split = {"withdrawn": latest}
            if caught > latest:  # an earlier invoice correction still counts
                split["prevented"] = caught - latest
            return "withdrawn", split
        return "prevented", {"prevented": caught}
    reduction = caught - latest
    if issue.resolved or shipment_status in PAID_STATUSES:
        rest = "accepted"
    else:
        rest = "at_risk"
    split = {rest: latest}
    if reduction > 0:
        split["prevented"] = reduction
    return (rest if latest > 0 else "prevented"), split


# --------------------------------------------------------------------------- periods

PERIODS = [
    ("this_month", "This month"), ("last_month", "Last month"), ("last_90", "Last 90 days"),
    ("this_year", "This year"), ("last_12", "Last 12 months"), ("custom", "Custom"),
]


def period_range(key: str, today: date | None = None, start: date | None = None, end: date | None = None
                 ) -> tuple[date, date, str]:
    """Start and end (inclusive) for a period name; 'custom' uses start/end."""
    today = today or timezone.localdate()
    first = today.replace(day=1)
    if key == "last_month":
        e = first - timedelta(days=1)
        return e.replace(day=1), e, "Last month"
    if key == "last_90":
        return today - timedelta(days=89), today, "Last 90 days"
    if key == "this_year":
        return today.replace(month=1, day=1), today, "This year"
    if key == "last_12":
        y, m = (first.year - 1, first.month + 1) if first.month < 12 else (first.year, 1)
        return date(y, m, 1), today, "Last 12 months"
    if key == "custom" and start and end:
        if start > end:
            start, end = end, start
        start = max(start, end - timedelta(days=366 * 5))
        return start, end, f"{start.day} {start:%b %Y} to {end.day} {end:%b %Y}"
    return first, today, "This month"


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    return (datetime.combine(start, time.min, tzinfo=tz), datetime.combine(end, time.max, tzinfo=tz))


def _months(start: date, end: date) -> list[date]:
    out, cur = [], start.replace(day=1)
    while cur <= end and len(out) < 61:
        out.append(cur)
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


# --------------------------------------------------------------------------- summary


def summary(org, start: date, end: date, *, limit: int | None = 25) -> Savings:
    """Money caught between start and end (inclusive, by first-caught date), split by outcome."""
    ledger.sync(org)
    s = Savings(start=start, end=end, currency=org.home_currency)
    lo, hi = _bounds(start, end)
    catches = list(CaughtCharge.objects.filter(organization=org, first_caught_at__gte=lo, first_caught_at__lte=hi)
                   .order_by("-first_caught_at", "-id"))
    statuses = dict(Shipment.objects.filter(pk__in={c.shipment_id for c in catches if c.shipment_id})
                    .values_list("pk", "status"))      # one small query instead of a Shipment object per catch
    issues = {i.pk: i for i in ValidationIssue.objects.filter(pk__in=[c.issue_id for c in catches if c.issue_id])
              .select_related("resolved_by")}
    by_code: dict[str, Row] = {}
    by_vendor: dict[str, Row] = {}
    months = {m: Row(m.isoformat(), f"{m:%b %Y}") for m in _months(start, end)}
    tz = timezone.get_current_timezone()
    rows: list[CatchRow] = []
    decided = [(c, *split_catch(c, issues.get(c.issue_id) if c.issue_id else None,
                                statuses.get(c.shipment_id) if c.shipment_id else None)) for c in catches]
    factors = _overlap_factors(decided)
    for c, outcome, split_native in decided:
        issue = issues.get(c.issue_id) if c.issue_id else None
        title = issue_title(c.code)
        factor = factors.get(c.pk, Decimal("1"))
        if factor == 0:
            outcome, split_native = "overlap", {}
        split: dict[str, Decimal] = {}
        convertible = True
        for bucket, amount in split_native.items():
            home = org.to_home(amount, c.currency or org.home_currency)
            if home is None:
                convertible = False
                break
            split[bucket] = (home * factor).quantize(Decimal("0.01"))
        if not convertible:
            cur = (c.currency or org.home_currency).upper()
            if outcome != "withdrawn":
                s.unconverted[cur] = s.unconverted.get(cur, ZERO) + (c.amount_caught * factor).quantize(Decimal("0.01"))
            rows.append(CatchRow(c, title, outcome, {}, None, issue))
            continue
        s.counts[outcome] += 1
        rows.append(CatchRow(c, title, outcome, split, sum(split.values(), ZERO), issue,
                             issue.resolution_note if issue is not None and issue.resolved else "", factor))
        if outcome == "overlap":
            continue
        s.withdrawn += split.get("withdrawn", ZERO)
        if outcome == "withdrawn" and not split.get("prevented"):
            continue
        s.prevented += split.get("prevented", ZERO)
        s.at_risk += split.get("at_risk", ZERO)
        s.accepted += split.get("accepted", ZERO)
        by_code.setdefault(c.code, Row(c.code, title)).add(split)
        vkey = c.vendor_key or "-"
        by_vendor.setdefault(vkey, Row(vkey, c.vendor_name or "Unknown vendor")).add(split)
        month = timezone.localtime(c.first_caught_at, tz).date().replace(day=1)
        if month in months:
            months[month].add(split)
    s.caught = s.prevented + s.at_risk + s.accepted
    s.by_code = sorted(by_code.values(), key=lambda r: (-r.caught, r.label))
    s.by_vendor = sorted(by_vendor.values(), key=lambda r: (-r.caught, r.label))
    s.by_month = list(months.values())
    s.catches = rows if limit is None else rows[:limit]
    # Documents are only shown for the rows on view, so load them for those rather than for every catch.
    prefetch_related_objects([r.catch for r in s.catches], "document", "shipment")
    _add_recoveries(org, s)
    return s


def _overlap_factors(decided: list[tuple]) -> dict[int, Decimal]:
    """Share of each catch that counts, so money flagged by two checks on one invoice counts once."""
    by_doc: dict[str, list[tuple]] = {}
    for c, outcome, split in decided:
        if c.scope.startswith("d") and outcome != "withdrawn":
            by_doc.setdefault(c.scope, []).append((c, outcome, split))
    factors: dict[int, Decimal] = {}
    for group in by_doc.values():
        if any(c.code in WHOLE_INVOICE for c, _, _ in group):
            for c, _, _ in group:
                if c.code not in WHOLE_INVOICE:
                    factors[c.pk] = Decimal("0")
            continue
        lines = sum((c.amount_caught for c, _, _ in group if c.code in LINE_LEVEL), ZERO)
        if not lines:
            continue
        for c, _, _ in group:
            if c.code in ESTIMATES and c.amount_caught > 0:
                factors[c.pk] = max(ZERO, c.amount_caught - lines) / c.amount_caught
    return factors


def _add_recoveries(org, s: Savings) -> None:
    s.recovery_sources = len(RECOVERY_SOURCES)
    for source in RECOVERY_SOURCES:
        try:
            result = source(org, s.start, s.end)
        except Exception:  # one broken source must not take the savings page down
            log.exception("Recovery source %r failed", source)
            continue
        for rec in _as_recoveries(result, org.home_currency):
            home = org.to_home(Decimal(str(rec.amount)), rec.currency or org.home_currency)
            if home is None:
                cur = (rec.currency or "").upper()
                s.unconverted[cur] = s.unconverted.get(cur, ZERO) + Decimal(str(rec.amount))
                continue
            s.recovered += home
            s.recoveries.append(rec)


def _as_recoveries(result, home: str) -> Iterable[Recovery]:
    if result is None:
        return []
    if isinstance(result, (int, float, Decimal)):
        return [Recovery(Decimal(str(result)), home)]
    out = []
    for item in result:
        if isinstance(item, Recovery):
            out.append(item)
        elif isinstance(item, dict):
            out.append(Recovery(**{k: v for k, v in item.items() if k in Recovery.__dataclass_fields__}))
        elif isinstance(item, (int, float, Decimal)):
            out.append(Recovery(Decimal(str(item)), home))
    return out


def this_month(org) -> Savings:
    start, end, _ = period_range("this_month")
    return summary(org, start, end, limit=0)
