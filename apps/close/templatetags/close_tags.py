"""Template helpers for Month-end pages, the sidebar link and the dashboard tile."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import template

from apps.core.permissions import has_perm

register = template.Library()

CONFIDENCE_CLASS = {"Actual": "neutral", "High": "ok", "Medium": "warn", "Low": "err", "None": "err",
                    "Set by a person": "info"}


@register.filter
def conf_class(label: str) -> str:
    return CONFIDENCE_CLASS.get(label or "", "neutral")


@register.filter
def signed(amount, currency: str = "") -> str:
    """+USD 125.00 / -USD 125.00 / USD 0.00, for differences."""
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return "–"
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}{currency} {abs(value):,.2f}".strip() if currency else f"{sign}{abs(value):,.2f}"


@register.filter
def isodate(value) -> str:
    """'2026-09-05' -> '5 Sep 2026' (dates stored as text in reports)."""
    from datetime import date

    try:
        d = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return value or ""
    return f"{d.day} {d:%b %Y}"


@register.filter
def whole(amount) -> str:
    """1234.5 -> '1,235' (amounts on tiles)."""
    try:
        return f"{Decimal(str(amount)):,.0f}"
    except (InvalidOperation, ValueError, TypeError):
        return "–"


@register.filter
def absval(amount):
    try:
        return abs(Decimal(str(amount)))
    except (InvalidOperation, ValueError, TypeError):
        return amount


@register.filter
def is_negative(amount) -> bool:
    try:
        return Decimal(str(amount)) < 0
    except (InvalidOperation, ValueError, TypeError):
        return False


@register.filter
def is_zero(amount) -> bool:
    try:
        return Decimal(str(amount)) == 0
    except (InvalidOperation, ValueError, TypeError):
        return False


@register.filter
def pct(value, total) -> float:
    try:
        total = Decimal(str(total))
        return float(abs(Decimal(str(value))) / abs(total) * 100) if total else 0.0
    except (InvalidOperation, ValueError, TypeError):
        return 0.0


@register.inclusion_tag("close/_tile.html", takes_context=True)
def month_end_tile(context):
    """Dashboard tile: last month's accruals, locked or not."""
    from ..models import AccrualSnapshot
    from ..services.accruals import previous_month_end

    request, org = context.get("request"), context.get("org")
    if request is None or org is None or not has_perm(request.user, org, "audit"):
        return {"show": False}
    period = previous_month_end()
    snap = AccrualSnapshot.objects.filter(organization=org, period_end=period).order_by("-version").first()
    return {"show": True, "period": period, "snap": snap, "org": org}
