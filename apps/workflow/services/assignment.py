"""Who works on a shipment: assigning by hand, and the automatic rules (taking turns, or by vendor).

Every change writes one audit row ("shipment.assigned" or "shipment.unassigned"); the assignee gets a
notification under the bell (and an email if they want one), and team channels subscribed to
"Assigned to you" get an alert.
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse

from apps.accounting.models import vendor_key
from apps.core.models import Membership
from apps.core.permissions import PERMISSION_TEXT, has_perm
from apps.core.utils import audit
from apps.shipments.models import Shipment

from ..models import Assignment, AssignmentRules, Notification, VendorRule
from .decisions import Outcome
from .notify import display, notify_user

log = logging.getLogger(__name__)

WORKING_ROLES = [Membership.Role.REVIEWER, Membership.Role.APPROVER, Membership.Role.ADMIN]
ACTIVE_STATUSES = [Shipment.Status.OPEN, Shipment.Status.NEEDS_REVIEW, Shipment.Status.READY]
AUTO_STATUSES = {Shipment.Status.NEEDS_REVIEW, Shipment.Status.READY}


def assignable(org):
    """Team members a shipment can be given to: active reviewers, approvers and admins of this organization."""
    return (get_user_model().objects.filter(is_active=True, memberships__organization=org,
                                            memberships__role__in=WORKING_ROLES)
            .order_by("first_name", "last_name", "username").distinct())


def current_assignee(shipment: Shipment):
    a = Assignment.objects.filter(shipment=shipment).select_related("assignee").first()
    return a.assignee if a else None


def assign(shipment: Shipment, assignee, actor=None, *, reason: str = Assignment.Reason.MANUAL,
           batch_id: str = "") -> Outcome:
    """Give the shipment to assignee (None = nobody). actor None means the automatic rules did it."""
    org = shipment.organization
    if actor is not None and not has_perm(actor, org, "edit"):
        return Outcome(shipment, False, [f"Your role in {org.name} does not allow you to {PERMISSION_TEXT['edit']}."])
    if assignee is not None and not assignable(org).filter(pk=assignee.pk).exists():
        return Outcome(shipment, False, [f"{display(assignee)} isn't a reviewer, approver or admin in {org.name}."])
    with transaction.atomic():
        a = (Assignment.objects.select_for_update(of=("self",)).filter(shipment=shipment)
             .select_related("assignee").first())
        previous = a.assignee if a else None
        if previous == assignee:
            who = f"assigned to {display(assignee)}" if assignee else "unassigned"
            return Outcome(shipment, False, [f"{shipment.reference} is already {who}."])
        if a is None:
            a = Assignment(organization=org, shipment=shipment)
        a.assignee, a.assigned_by, a.reason = assignee, actor, reason if assignee else Assignment.Reason.MANUAL
        a.save()
        data = {"previous": previous.get_username() if previous else "", "previous_name": display(previous)
                if previous else "", "reason": a.reason}
        if batch_id:
            data["batch_id"] = batch_id
        if assignee is not None:
            audit(org, "shipment.assigned", shipment, actor=actor, assignee=assignee.get_username(),
                  assignee_id=assignee.pk, assignee_name=display(assignee), **data)
        else:
            audit(org, "shipment.unassigned", shipment, actor=actor, **data)
    if assignee is not None:
        by = display(actor) if actor else "The assignment rules"
        notify_user(assignee, org, Notification.Kind.ASSIGNED, f"{by} assigned {shipment.reference} to you",
                    body=_summary(shipment), url=reverse("review:shipment", args=[shipment.pk]), actor=actor,
                    obj=shipment)
    return Outcome(shipment, True)


def _summary(shipment: Shipment) -> str:
    parts = [shipment.get_status_display()]
    if shipment.bl_number:
        parts.append(f"B/L {shipment.bl_number}")
    errors = shipment.issues.filter(resolved=False, severity="error").count()
    warnings = shipment.issues.filter(resolved=False, severity="warning").count()
    if errors:
        parts.append(f"{errors} error{'s' if errors != 1 else ''}")
    if warnings:
        parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
    return ", ".join(parts)


# --------------------------------------------------------------------------- automatic rules


def turn_pool(org, rules: AssignmentRules) -> list:
    roles = [Membership.Role.REVIEWER] + ([Membership.Role.APPROVER] if rules.include_approvers else [])
    return list(get_user_model().objects.filter(is_active=True, memberships__organization=org,
                                                memberships__role__in=roles).order_by("pk").distinct())


def next_in_turn(org, rules: AssignmentRules):
    """Round robin: the next person after the one who got the last shipment (by user id, wrapping)."""
    pool = turn_pool(org, rules)
    if not pool:
        return None
    last = rules.last_assigned_id or 0
    return next((u for u in pool if u.pk > last), pool[0])


def shipment_vendor_keys(shipment: Shipment) -> list[str]:
    """Vendors on the shipment's documents: payable invoices first, then the others."""
    docs = sorted(shipment.documents.prefetch_related("fields"), key=lambda d: (not d.posts_to_accounting, d.pk))
    keys = []
    for d in docs:
        k = vendor_key(d.field("vendor_name") or "")
        if k and k not in keys:
            keys.append(k)
    return keys


def vendor_choice(org, shipment: Shipment):
    rules = {r.vendor_key: r for r in VendorRule.objects.filter(organization=org).select_related("assignee")}
    if not rules:
        return None
    allowed = set(assignable(org).values_list("pk", flat=True))
    for k in shipment_vendor_keys(shipment):
        r = rules.get(k)
        if r and r.assignee_id in allowed:
            return r.assignee
    return None


def auto_assign(shipment: Shipment) -> Outcome | None:
    """Apply the organization's rules to a shipment nobody has touched yet. None = nothing to do."""
    if Assignment.objects.filter(shipment=shipment).exists() or shipment.status not in AUTO_STATUSES:
        return None
    org = shipment.organization
    with transaction.atomic():
        rules = AssignmentRules.objects.select_for_update().filter(organization=org).first()
        if rules is None or rules.mode == AssignmentRules.Mode.OFF:
            return None
        person, reason = None, Assignment.Reason.ROUND_ROBIN
        if rules.mode == AssignmentRules.Mode.VENDOR:
            person, reason = vendor_choice(org, shipment), Assignment.Reason.VENDOR
            if person is None and rules.vendor_fallback:
                person, reason = next_in_turn(org, rules), Assignment.Reason.ROUND_ROBIN
        else:
            person = next_in_turn(org, rules)
        if person is None:
            return None
        if reason == Assignment.Reason.ROUND_ROBIN:
            rules.last_assigned = person
            rules.save(update_fields=["last_assigned", "updated_at"])
        return assign(shipment, person, None, reason=reason)


def assign_waiting(org) -> int:
    """Run the rules on every shipment waiting for review or approval that nobody was ever given."""
    done = 0
    for s in (Shipment.objects.filter(organization=org, status__in=AUTO_STATUSES, assignment__isnull=True)
              .select_related("organization").order_by("created_at", "pk")[:500]):
        outcome = auto_assign(s)
        if outcome is not None and outcome.ok:
            done += 1
    return done


# --------------------------------------------------------------------------- signal handlers


def before_shipment_save(sender, instance, update_fields=None, **kwargs) -> None:
    if instance.pk is None or (update_fields is not None and "status" not in update_fields):
        instance._workflow_old_status = None
        return
    try:
        instance._workflow_old_status = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    except Exception:
        instance._workflow_old_status = None


def on_shipment_saved(sender, instance, created: bool, update_fields=None, **kwargs) -> None:
    old = getattr(instance, "_workflow_old_status", None)
    if created or old is None or old == instance.status or instance.status not in AUTO_STATUSES:
        return
    pk = instance.pk

    def run():
        try:
            s = Shipment.objects.select_related("organization").filter(pk=pk).first()
            if s is not None:
                auto_assign(s)
        except Exception:
            log.exception("Automatic assignment failed for shipment %s", pk)

    try:
        transaction.on_commit(run)
    except Exception:
        log.exception("Could not schedule automatic assignment for shipment %s", pk)
