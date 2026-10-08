"""Money recovered through disputes, for savings reports.

`recovered(org, start, end)` is the hook for the savings dashboard (RECOVERY_SOURCES). It returns the
amount credited by vendors in the organization's home currency, counted on the day the credit was
recorded. Each credit is converted at the exchange rate set when it was recorded; credits recorded
before a rate existed are converted at today's rate, and left out if there is still no rate.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone

from .models import Dispute


def org_zone(org) -> ZoneInfo:
    try:
        return ZoneInfo(org.timezone or "UTC")
    except (ValueError, KeyError, OSError):  # unknown zone name
        return ZoneInfo("UTC")


def org_today(org) -> date:
    return timezone.localtime(timezone.now(), org_zone(org)).date()


def _bounds(org, start, end) -> tuple[datetime, datetime]:
    """Dates are whole days in the organization's time zone, `end` included.
    Datetimes are used as given, `start` included and `end` excluded."""
    tz = org_zone(org)
    if isinstance(start, datetime):
        lo = start if timezone.is_aware(start) else timezone.make_aware(start, tz)
    else:
        lo = datetime.combine(start, time.min, tzinfo=tz)
    if isinstance(end, datetime):
        hi = end if timezone.is_aware(end) else timezone.make_aware(end, tz)
    else:
        hi = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz)
    return lo, hi


def recovered_disputes(org, start: date | datetime, end: date | datetime):
    lo, hi = _bounds(org, start, end)
    return Dispute.objects.filter(organization=org, status__in=Dispute.RECOVERED, recovered_at__gte=lo,
                                  recovered_at__lt=hi, amount_recovered__gt=0)


def recovered(org, start: date | datetime, end: date | datetime) -> Decimal:
    """Total recovered from vendors between start and end, in the home currency."""
    total = Decimal("0.00")
    for d in recovered_disputes(org, start, end).only("amount_recovered", "amount_recovered_home", "currency"):
        value = d.amount_recovered_home
        if value is None:
            value = org.to_home(d.amount_recovered, d.currency)
        if value is not None:
            total += value
    return total.quantize(Decimal("0.01"))


def recovered_by_currency(org, start, end) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for d in recovered_disputes(org, start, end).only("amount_recovered", "currency"):
        cur = d.currency or org.home_currency
        out[cur] = out.get(cur, Decimal("0.00")) + d.amount_recovered
    return out


def recovery_items(org, start, end) -> list[dict]:
    """One entry per recovered dispute, for the Savings page (registered in DisputesConfig.ready)."""
    out = []
    for d in recovered_disputes(org, start, end).only("reference", "vendor_name", "amount_recovered", "currency",
                                                      "recovered_at"):
        out.append({"amount": d.amount_recovered, "currency": d.currency or org.home_currency,
                    "vendor_name": d.vendor_name, "code": "dispute",
                    "on": d.recovered_at.date() if d.recovered_at else None,
                    "label": f"{d.reference} credit from {d.vendor_name}"})
    return out
