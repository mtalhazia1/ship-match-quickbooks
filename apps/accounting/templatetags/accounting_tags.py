"""Template helpers for accounting includes: payment status on shipments, the shipment list and the dashboard."""
from __future__ import annotations

from django import template

from apps.accounting.models import PostedBill
from apps.accounting.services import payments, providers

register = template.Library()


@register.simple_tag
def ap_aging(org):
    return payments.aging(org) if org is not None else None


@register.simple_tag
def payment_filters():
    return payments.FILTERS


@register.simple_tag
def payment_cell(shipment, page=None):
    """The payment summary of one shipment in a list; the whole page is read in one query and cached on it."""
    rows = list(page.object_list if page is not None and hasattr(page, "object_list") else [shipment])
    cache = getattr(page, "_payment_summaries", None) if page is not None else None
    if cache is None:
        cache = payments.summaries(rows)
        if page is not None:
            page._payment_summaries = cache
    return cache.get(shipment.pk)


@register.simple_tag
def shipment_bills(shipment):
    """Posted bills and vendor credits of a shipment, with a link into the accounting system when there is one."""
    bills = list(PostedBill.objects.filter(shipment=shipment).exclude(qbo_bill_id="")
                 .select_related("document").order_by("kind", "pk"))
    for pb in bills:
        pb.link = providers.bill_link(pb)
    return bills


@register.simple_tag
def rule_for(mapping, system):
    account_id, name = providers.rule_account(mapping, system or "quickbooks")
    return {"id": account_id, "name": name}


@register.filter
def payment_tone(pb):
    key = pb.payment_key if pb else ""
    return {"paid": "ok", "partly_paid": "warn", "overdue": "err", "voided": "err", "deleted": "err"}.get(key, "neutral")


@register.filter
def system_label(key):
    return dict(PostedBill.System.choices).get(key, key)


@register.filter
def iso_day(value):
    """A stored 'YYYY-MM-DD' payment date as a date (for the date filter); '' when missing or unreadable."""
    from datetime import date

    try:
        return date.fromisoformat(str(value)[:10]) if value else ""
    except ValueError:
        return ""
