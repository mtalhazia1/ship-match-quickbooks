"""History of a shipment for the timeline: every audit event on it, its documents and its issues."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q

from apps.core.models import AuditEvent
from apps.shipments.labels import describe_action
from apps.shipments.models import Shipment

QUIET = {"shipment.validated", "document.extracted"}  # system noise; kept in the audit log


@dataclass
class Entry:
    at: datetime
    who: str
    text: str
    action: str
    note: str = ""


def timeline(shipment: Shipment, include_system: bool = False) -> list[Entry]:
    doc_ids = [str(d.pk) for d in shipment.documents]
    issue_ids = [str(pk) for pk in shipment.issues.values_list("pk", flat=True)]
    q = (Q(object_type="Shipment", object_id=str(shipment.pk))
         | Q(object_type="Document", object_id__in=doc_ids)
         | Q(object_type="ValidationIssue", object_id__in=issue_ids))
    events = AuditEvent.objects.filter(q).select_related("actor").order_by("-created_at", "-id")[:200]
    out = []
    for e in events:
        if e.action in QUIET and not include_system:
            continue
        who = (e.actor.get_full_name() or e.actor.get_username()) if e.actor else "ShipMatch"
        note = (e.data or {}).get("note", "") if e.action in {"issue.resolved", "shipment.rejected", "shipment.approved"} else ""
        out.append(Entry(at=e.created_at, who=who, text=describe_action(e.action, e.data), action=e.action, note=note))
    return out
