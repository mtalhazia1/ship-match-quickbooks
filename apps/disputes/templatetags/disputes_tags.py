from django import template

from ..evidence import disputable_groups
from ..models import Dispute
from ..reports import overdue_q

register = template.Library()


@register.simple_tag
def shipment_disputes(shipment):
    """Invoices on the shipment that can be disputed, and every dispute raised from it."""
    groups = disputable_groups(shipment)
    in_groups = {d.pk for g in groups for d in g.disputes}
    others = [d for d in Dispute.objects.filter(shipment=shipment).order_by("-created_at") if d.pk not in in_groups]
    return {"groups": groups, "others": others,
            "holding": [d for g in groups for d in g.disputes if d.holds_shipment] + [d for d in others if d.holds_shipment]}


@register.simple_tag
def overdue_dispute_count(org) -> int:
    if not org:
        return 0
    return Dispute.objects.filter(organization=org).filter(overdue_q(org)).count()


@register.simple_tag
def ai_wording_enabled() -> bool:
    from apps.documents.services import llm

    return llm.is_enabled()


@register.filter
def dispute_badge(status: str) -> str:
    return {
        "draft": "neutral", "sent": "warn", "acknowledged": "info", "credit_received": "ok", "resolved": "ok",
        "closed": "neutral",
    }.get(status, "neutral")
