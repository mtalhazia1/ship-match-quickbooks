"""Review queue additions: the "Assigned to" filter, and the previous/next shipment for j and k."""
from __future__ import annotations

from django.db.models import Count, Q

from apps.shipments.models import Shipment

ASSIGNED_CHOICES = [("", "Everyone"), ("me", "Assigned to me"), ("none", "Unassigned")]


def filter_queue(request, qs):
    """Called by the review queue on its base queryset (so the tab counts follow the filter too)."""
    who = request.GET.get("assigned", "")
    if who == "me":
        qs = qs.filter(assignment__assignee=request.user)
    elif who == "none":
        qs = qs.filter(Q(assignment__isnull=True) | Q(assignment__assignee__isnull=True))
    elif who.isdigit():
        qs = qs.filter(assignment__assignee_id=int(who))
    return qs.select_related("assignment__assignee")


QUEUE_ORDER = ("-n_errors", "-n_warnings", "created_at", "-id")
NEIGHBOUR_LIMIT = 1000


def neighbours(shipment: Shipment) -> tuple[Shipment | None, Shipment | None]:
    """Previous and next shipment in the queue tab this one is in (default "most issues first" order)."""
    ids = list(Shipment.objects.filter(organization_id=shipment.organization_id, status=shipment.status)
               .annotate(n_errors=Count("issues", filter=Q(issues__resolved=False, issues__severity="error"),
                                        distinct=True),
                         n_warnings=Count("issues", filter=Q(issues__resolved=False, issues__severity="warning"),
                                          distinct=True))
               .order_by(*QUEUE_ORDER).values_list("pk", flat=True)[:NEIGHBOUR_LIMIT])
    if shipment.pk not in ids:
        return None, None
    i = ids.index(shipment.pk)
    prev_id = ids[i - 1] if i > 0 else None
    next_id = ids[i + 1] if i + 1 < len(ids) else None
    found = {s.pk: s for s in Shipment.objects.filter(pk__in=[p for p in (prev_id, next_id) if p])}
    return found.get(prev_id), found.get(next_id)
