"""Bulk actions on the review queue: approve, assign, post to accounting.

Each shipment goes through exactly the single-shipment path (services.decisions / services.assignment),
so every rule applies to each one on its own: permissions, approval limit (per shipment), maker-checker,
open errors, dispute holds, the two-factor policy. One audit row per shipment, each carrying the batch id:
the normal row when the action happened ("shipment.approved", "shipment.assigned",
"shipment.post_requested") or "shipment.bulk_skipped" with the reasons when it didn't. The results page
reads the batch back from the audit log.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from django.conf import settings

from apps.core.models import AuditEvent
from apps.core.utils import audit
from apps.shipments.models import Shipment

from . import assignment, decisions
from .decisions import Outcome

ACTIONS = {"approve": "approve", "assign": "edit", "post": "post"}  # action -> permission
DONE_ACTIONS = {"approve": "shipment.approved", "assign": "shipment.assigned", "post": "shipment.post_requested"}
UNASSIGN_ACTION = "shipment.unassigned"


def max_shipments() -> int:
    return max(1, int(getattr(settings, "BULK_MAX_SHIPMENTS", 100)))


def parse_ids(raw: list[str]) -> list[int]:
    out = []
    for v in raw:
        v = (v or "").strip()
        if v.isdigit() and int(v) not in out:
            out.append(int(v))
    return out


def selected(org, ids: list[int]):
    """The chosen shipments that really are in this organization, in reference order."""
    return list(Shipment.objects.filter(organization=org, pk__in=ids).select_related("organization")
                .order_by("reference", "pk"))


@dataclass
class Preview:
    shipment: Shipment
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons


def preview(action: str, user, shipments: list[Shipment]) -> list[Preview]:
    """What would happen, without changing anything (for the confirmation page)."""
    out = []
    for s in shipments:
        if action == "approve":
            out.append(Preview(s, decisions.approve_blockers(s, user)))
        elif action == "post":
            out.append(Preview(s, decisions.post_blockers(s, user)))
        else:
            out.append(Preview(s))
    return out


def run(action: str, org, user, shipments: list[Shipment], *, note: str = "", assignee=None,
        unassign: bool = False) -> tuple[str, list[Outcome]]:
    batch_id = uuid.uuid4().hex
    outcomes = []
    for s in shipments:
        if action == "approve":
            o = decisions.approve(s, user, note, batch_id=batch_id, via="bulk")
        elif action == "post":
            o = decisions.request_post(s, user, batch_id=batch_id)
        elif action == "assign":
            o = assignment.assign(s, None if unassign else assignee, user, batch_id=batch_id)
        else:
            raise ValueError(f"Unknown bulk action {action}")
        if not o.ok:
            audit(org, "shipment.bulk_skipped", o.shipment, actor=user, batch_id=batch_id, bulk_action=action,
                  bulk_verb={"approve": "approve", "post": "post", "assign": "assign"}[action], reasons=o.reasons)
        outcomes.append(o)
    return batch_id, outcomes


@dataclass
class BatchLine:
    shipment: Shipment | None
    reference: str
    ok: bool
    action: str
    reasons: list[str] = field(default_factory=list)
    detail: str = ""
    at: object = None


def batch_lines(org, user, batch_id: str) -> list[BatchLine]:
    """A batch read back from the audit log (only the person's own batches in this organization)."""
    events = list(AuditEvent.objects.filter(organization=org, actor=user, object_type="Shipment",
                                            data__batch_id=batch_id).order_by("id"))
    ships = {str(s.pk): s for s in Shipment.objects.filter(organization=org,
                                                            pk__in=[e.object_id for e in events if e.object_id.isdigit()])}
    lines = []
    for e in events:
        s = ships.get(e.object_id)
        ref = s.reference if s else f"Shipment {e.object_id}"
        data = e.data or {}
        if e.action == "shipment.bulk_skipped":
            lines.append(BatchLine(s, ref, False, data.get("bulk_action", ""), list(data.get("reasons") or []),
                                   at=e.created_at))
        else:
            action = {v: k for k, v in DONE_ACTIONS.items()}.get(e.action, "assign")
            detail = {"shipment.approved": "Approved", "shipment.post_requested": "Posting started",
                      "shipment.assigned": f"Assigned to {data.get('assignee_name', '')}",
                      UNASSIGN_ACTION: "Nobody assigned now"}.get(e.action, e.action)
            lines.append(BatchLine(s, ref, True, action, detail=detail, at=e.created_at))
    return lines
