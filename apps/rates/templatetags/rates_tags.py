"""Template helpers for the Rates and Savings pages, and the dashboard tile."""
from __future__ import annotations

from decimal import Decimal

from django import template

from apps.core.permissions import has_perm

from .. import charges as charge_codes
from ..savings import this_month

register = template.Library()


@register.inclusion_tag("rates/_tile.html", takes_context=True)
def overcharge_tile(context):
    """'Overcharges caught this month' KPI tile for the main dashboard."""
    request = context.get("request")
    org = context.get("org")
    if request is None or org is None or not has_perm(request.user, org, "view"):
        return {"show": False}
    return {"show": True, "s": this_month(org), "org": org}


@register.filter
def charge_label(code: str) -> str:
    return charge_codes.label(code)


@register.filter
def pct_of(value, total) -> float:
    """value as a percentage of total, for bar widths."""
    try:
        total = Decimal(str(total))
        return float(Decimal(str(value)) / total * 100) if total else 0.0
    except Exception:
        return 0.0


@register.filter
def whole(amount) -> str:
    """1234.5 -> '1,235' (amounts on charts and tiles)."""
    try:
        return f"{Decimal(str(amount)):,.0f}"
    except Exception:
        return "–"
