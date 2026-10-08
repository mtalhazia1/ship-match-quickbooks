"""Totals and aging for the disputes list and the daily digest."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from .models import Dispute
from .savings import org_today, recovered

AGE_BUCKETS = [("0-7", "Up to 7 days", 0, 7), ("8-30", "8 to 30 days", 8, 30), ("31-60", "31 to 60 days", 31, 60),
               ("61+", "Over 60 days", 61, None)]


@dataclass
class Money:
    by_currency: dict[str, Decimal] = field(default_factory=dict)
    home: Decimal | None = Decimal("0.00")
    missing_rates: list[str] = field(default_factory=list)
    count: int = 0

    def add(self, org, amount: Decimal, currency: str) -> None:
        cur = (currency or org.home_currency).upper()
        self.count += 1
        self.by_currency[cur] = self.by_currency.get(cur, Decimal("0.00")) + (amount or Decimal("0.00"))
        converted = org.to_home(amount or Decimal("0.00"), cur)
        if converted is None:
            if cur not in self.missing_rates:
                self.missing_rates.append(cur)
        elif self.home is not None:
            self.home += converted

    @property
    def complete(self) -> bool:
        return not self.missing_rates


def period_starts(org, today: date | None = None) -> tuple[date, date, date]:
    today = today or org_today(org)
    month = today.replace(day=1)
    quarter = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    return today, month, quarter


def age_bucket(days: int | None) -> str:
    if days is None:
        return ""
    for key, _, lo, hi in AGE_BUCKETS:
        if days >= lo and (hi is None or days <= hi):
            return key
    return ""


def filter_age(qs, key: str):
    """Waiting disputes sent between the bucket's bounds (in days ago)."""
    bucket = next((b for b in AGE_BUCKETS if b[0] == key), None)
    if not bucket:
        return qs
    now = timezone.now()
    _, _, lo, hi = bucket
    qs = qs.filter(status__in=Dispute.WAITING, sent_at__lte=now - timedelta(days=lo))
    if hi is not None:
        qs = qs.filter(sent_at__gt=now - timedelta(days=hi + 1))
    return qs


def overdue_q(org) -> Q:
    return Q(status__in=Dispute.WAITING, follow_up_on__lt=org_today(org))


@dataclass
class Summary:
    waiting: Money
    recovered_month: Decimal
    recovered_quarter: Decimal
    overdue: int
    drafts: int
    aging: list[dict]
    month_start: date
    quarter_start: date


def summary(org) -> Summary:
    base = Dispute.objects.filter(organization=org)
    waiting = Money()
    buckets = {key: {"key": key, "label": label, "count": 0, "money": Money()} for key, label, _, _ in AGE_BUCKETS}
    for d in base.filter(status__in=Dispute.WAITING).only("amount_disputed", "currency", "sent_at", "status",
                                                          "closed_at", "recovered_at"):
        waiting.add(org, d.amount_disputed, d.currency)
        key = age_bucket(d.days_open)
        if key:
            buckets[key]["count"] += 1
            buckets[key]["money"].add(org, d.amount_disputed, d.currency)
    today, month, quarter = period_starts(org)
    return Summary(
        waiting=waiting,
        recovered_month=recovered(org, month, today),
        recovered_quarter=recovered(org, quarter, today),
        overdue=base.filter(overdue_q(org)).count(),
        drafts=base.filter(status=Dispute.Status.DRAFT).count(),
        aging=list(buckets.values()),
        month_start=month,
        quarter_start=quarter,
    )
