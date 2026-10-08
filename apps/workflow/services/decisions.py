"""Approve, reject and post one shipment, with exactly the rules of the shipment page.

Used by bulk actions, the focused approval page (the link in "ready for approval" alerts) and anything
else that decides outside the shipment page. The rules themselves are not copied: permissions come from
apps.core.permissions and every approval check (open errors, rejected, dispute holds and other registered
blockers, role, maker-checker, approval limit, the organization's two-factor policy) from
apps.shipments.services.approval.approval_blockers. The writes match apps.shipments.views.approve/reject:
an Approval row, the status change and one audit row.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Profile
from apps.core.permissions import PERMISSION_TEXT, has_perm
from apps.core.utils import audit
from apps.shipments.models import Approval, Shipment
from apps.shipments.services.approval import approval_blockers, shipment_totals

NOTE_MAX = 500


@dataclass
class Outcome:
    shipment: Shipment
    ok: bool
    reasons: list[str] = field(default_factory=list)


def _no_permission(org, perm: str) -> str:
    return f"Your role in {org.name} does not allow you to {PERMISSION_TEXT[perm]}."


def mfa_missing(user, org) -> bool:
    """The organization requires two-factor authentication and this person hasn't turned it on."""
    if not org.require_mfa:
        return False
    profile = Profile.objects.filter(user=user).first()
    return not (profile and profile.mfa_enabled)


def mfa_blocker(shipment: Shipment, user) -> list[str]:
    """Registered as an approval rule: the sign-in check only looks at the organization in use, so an
    approval reached from another organization (a firm's cross-client list, an alert link) checks again."""
    org = shipment.organization
    if mfa_missing(user, org):
        return [f"{org.name} requires two-factor authentication. Turn it on under Security and sign-in, "
                "then approve."]
    return []


def approve_blockers(shipment: Shipment, user) -> list[str]:
    """Every reason this person can't approve this shipment now (empty = may approve)."""
    if not has_perm(user, shipment.organization, "approve"):
        return [_no_permission(shipment.organization, "approve")]
    return approval_blockers(shipment, user)


def approve(shipment: Shipment, user, note: str = "", *, batch_id: str = "", via: str = "") -> Outcome:
    note = (note or "").strip()[:NOTE_MAX]
    with transaction.atomic():
        s = (Shipment.objects.select_for_update(of=("self",)).select_related("organization").filter(pk=shipment.pk).first())
        if s is None:
            return Outcome(shipment, False, ["This shipment no longer exists."])
        reasons = approve_blockers(s, user)
        if reasons:
            return Outcome(s, False, reasons)
        totals = shipment_totals(s)
        Approval.objects.create(shipment=s, user=user, decision=Approval.Decision.APPROVE, note=note)
        s.status, s.approved_by, s.approved_at = Shipment.Status.APPROVED, user, timezone.now()
        s.save()
        extra = {k: v for k, v in (("batch_id", batch_id), ("via", via)) if v}
        audit(s.organization, "shipment.approved", s, actor=user, note=note, total_home=totals.home,
              totals={k: str(v) for k, v in totals.by_currency.items()}, **extra)
    return Outcome(s, True)


def reject_blockers(shipment: Shipment, user, note: str) -> list[str]:
    from apps.shipments.views import REJECT_NOTE_MIN

    if not has_perm(user, shipment.organization, "approve"):
        return [_no_permission(shipment.organization, "approve")]
    if shipment.is_locked:
        return [f"{shipment.reference} is already {shipment.get_status_display().lower()}. Reopen it first."]
    if shipment.status == Shipment.Status.REJECTED:
        return [f"{shipment.reference} is already rejected."]
    if len((note or "").strip()) < REJECT_NOTE_MIN:
        return ["Give a reason for rejecting, so the team knows what to fix."]
    return []


def reject(shipment: Shipment, user, note: str, *, via: str = "") -> Outcome:
    note = (note or "").strip()[:NOTE_MAX]
    with transaction.atomic():
        s = (Shipment.objects.select_for_update(of=("self",)).select_related("organization").filter(pk=shipment.pk).first())
        if s is None:
            return Outcome(shipment, False, ["This shipment no longer exists."])
        reasons = reject_blockers(s, user, note)
        if reasons:
            return Outcome(s, False, reasons)
        Approval.objects.create(shipment=s, user=user, decision=Approval.Decision.REJECT, note=note)
        s.status = Shipment.Status.REJECTED
        s.save(update_fields=["status", "updated_at"])
        audit(s.organization, "shipment.rejected", s, actor=user, note=note, **({"via": via} if via else {}))
    return Outcome(s, True)


def post_blockers(shipment: Shipment, user) -> list[str]:
    org = shipment.organization
    if not has_perm(user, org, "post"):
        return [_no_permission(org, "post")]
    if mfa_missing(user, org):
        return [f"{org.name} requires two-factor authentication. Turn it on under Security and sign-in first."]
    if shipment.status == Shipment.Status.POSTED:
        return [f"{shipment.reference} is already posted."]
    if shipment.status != Shipment.Status.APPROVED:
        return [f"Only approved shipments can be posted; {shipment.reference} is "
                f"{shipment.get_status_display().lower()}."]
    from apps.accounting.services.providers import active_connection
    from apps.shipments.services.approval import posting_blockers

    conn = active_connection(org)
    if conn is None:
        return ["Connect QuickBooks or Xero in Settings > Accounting before posting."]
    if conn.needs_reconnect:
        return [f"{conn.system_name} needs to be connected again by an admin before anything can be posted."]
    return posting_blockers(shipment)


def request_post(shipment: Shipment, user, *, batch_id: str = "") -> Outcome:
    """Queue posting to the connected accounting system, as the Post bills button does."""
    from apps.accounting.tasks import post_shipment_task

    s = Shipment.objects.select_related("organization").filter(pk=shipment.pk).first()
    if s is None:
        return Outcome(shipment, False, ["This shipment no longer exists."])
    reasons = post_blockers(s, user)
    if reasons:
        return Outcome(s, False, reasons)
    post_shipment_task.delay(s.pk, user.pk)
    audit(s.organization, "shipment.post_requested", s, actor=user, **({"batch_id": batch_id} if batch_id else {}))
    return Outcome(s, True)
