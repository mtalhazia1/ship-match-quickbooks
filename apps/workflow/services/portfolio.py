"""The firm view: one row per client organization the person belongs to, and a "My work" list across them.

Scoping is by membership only. Platform superusers see other organizations only on the platform admin
side, never here. Organizations that require two-factor authentication are listed without their numbers
until the person turns it on, as the sign-in rules would refuse to open them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from django.db.models import Count, Min
from django.utils import timezone

from apps.accounting.models import PostedBill
from apps.core.models import Membership, Organization
from apps.shipments.models import Shipment, ValidationIssue
from apps.shipments.services.approval import approval_blockers, shipment_totals

from .decisions import mfa_missing

ACTIVE = [Shipment.Status.OPEN, Shipment.Status.NEEDS_REVIEW, Shipment.Status.READY]
WAITING = [Shipment.Status.NEEDS_REVIEW, Shipment.Status.READY]
APPROVER_ROLES = {Membership.Role.APPROVER, Membership.Role.ADMIN}


def member_orgs(user):
    """Organizations this person is a member of (superusers included: no implicit access here)."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return Organization.objects.none()
    return Organization.objects.filter(memberships__user=user).distinct()


def is_multi_org(user) -> bool:
    return Membership.objects.filter(user=user).count() > 1


@dataclass
class Row:
    org: Organization
    role: str
    role_label: str
    locked: bool = False          # requires two-factor and the person hasn't turned it on
    needs_review: int = 0
    ready: int = 0
    assigned_to_me: int = 0
    failed_postings: int = 0
    connection: str = "none"      # ok, expiring, reconnect, none
    connection_label: str = "Not connected"
    at_risk: dict = field(default_factory=dict)
    at_risk_home: Decimal | None = Decimal("0.00")
    oldest_waiting: datetime | None = None

    @property
    def oldest_hours(self) -> float | None:
        if self.oldest_waiting is None:
            return None
        return round((timezone.now() - self.oldest_waiting).total_seconds() / 3600, 1)

    @property
    def at_risk_sort(self) -> Decimal:
        if self.at_risk_home is not None:
            return self.at_risk_home
        return sum(self.at_risk.values(), Decimal("0"))

    @property
    def attention(self) -> int:
        return self.needs_review + self.ready + self.failed_postings + (1 if self.connection == "reconnect" else 0)


SORTS = {
    "name": lambda r: r.org.name.lower(),
    "needs_review": lambda r: r.needs_review,
    "ready": lambda r: r.ready,
    "assigned": lambda r: r.assigned_to_me,
    "failed": lambda r: r.failed_postings,
    "connection": lambda r: {"reconnect": 0, "none": 1, "expiring": 2, "ok": 3}[r.connection],
    "at_risk": lambda r: r.at_risk_sort,
    "oldest": lambda r: r.oldest_hours or 0,
}
DEFAULT_DESC = {"needs_review", "ready", "assigned", "failed", "at_risk", "oldest"}


def build(user) -> list[Row]:
    memberships = {m.organization_id: m for m in Membership.objects.filter(user=user).select_related("organization")}
    orgs = [m.organization for m in memberships.values()]
    roles = dict(Membership.Role.choices)
    rows = {o.pk: Row(o, memberships[o.pk].role, roles.get(memberships[o.pk].role, ""), locked=mfa_missing(user, o))
            for o in orgs}
    open_ids = [pk for pk, r in rows.items() if not r.locked]

    for org_id, status, n in (Shipment.objects.filter(organization_id__in=open_ids, status__in=WAITING)
                              .values_list("organization_id", "status").annotate(n=Count("id"))):
        if status == Shipment.Status.NEEDS_REVIEW:
            rows[org_id].needs_review = n
        else:
            rows[org_id].ready = n
    for org_id, oldest in (Shipment.objects.filter(organization_id__in=open_ids, status__in=WAITING)
                           .values("organization_id").annotate(m=Min("created_at")).values_list("organization_id", "m")):
        rows[org_id].oldest_waiting = oldest
    for org_id, n in (Shipment.objects.filter(organization_id__in=open_ids, status__in=ACTIVE,
                                              assignment__assignee=user)
                      .values("organization_id").annotate(n=Count("id")).values_list("organization_id", "n")):
        rows[org_id].assigned_to_me = n
    for org_id, n in (PostedBill.objects.filter(organization_id__in=open_ids, status=PostedBill.Status.FAILED)
                      .values("organization_id").annotate(n=Count("id")).values_list("organization_id", "n")):
        rows[org_id].failed_postings = n
    from apps.accounting.services.providers import active_connection

    for org_id in open_ids:
        r = rows[org_id]
        conn = active_connection(r.org)
        if conn is None:
            continue
        if conn.needs_reconnect:
            r.connection, r.connection_label = "reconnect", "Needs reconnecting"
        elif conn.reconnect_due_soon:
            r.connection, r.connection_label = "expiring", "Reconnect soon"
        else:
            r.connection, r.connection_label = "ok", "Connected"
    for org_id, cur, amount in (ValidationIssue.objects.filter(
            organization_id__in=open_ids, resolved=False, amount_at_risk__isnull=False, shipment__status__in=ACTIVE)
            .values_list("organization_id", "currency", "amount_at_risk")):
        r = rows[org_id]
        cur = (cur or r.org.home_currency).upper()
        r.at_risk[cur] = r.at_risk.get(cur, Decimal("0.00")) + amount
    for r in rows.values():
        for cur, amount in r.at_risk.items():
            converted = r.org.to_home(amount, cur)
            if converted is None:
                r.at_risk_home = None
            elif r.at_risk_home is not None:
                r.at_risk_home += converted
    return list(rows.values())


def sort_rows(rows: list[Row], key: str, direction: str) -> tuple[list[Row], str, str]:
    key = key if key in SORTS else "attention"
    if key == "attention":
        return sorted(rows, key=lambda r: (r.locked, -r.attention, r.org.name.lower())), key, "desc"
    direction = direction if direction in {"asc", "desc"} else ("desc" if key in DEFAULT_DESC else "asc")
    ordered = sorted(rows, key=lambda r: (SORTS[key](r), r.org.name.lower()), reverse=direction == "desc")
    return sorted(ordered, key=lambda r: r.locked), key, direction  # stable: locked rows last


# --------------------------------------------------------------------------- my work


@dataclass
class WorkItem:
    shipment: Shipment
    assigned: bool = False
    can_approve: bool = False
    blockers: list = field(default_factory=list)
    totals: object = None

    @property
    def org(self) -> Organization:
        return self.shipment.organization

    @property
    def waiting_since(self) -> datetime:
        return self.shipment.created_at


def add_totals(items) -> None:
    """Payable totals for the rows about to be shown. Done after paging: a person with a few hundred open
    shipments sees 25 at a time, and working out the other hundreds made the page take seconds."""
    for i in items:
        i.totals = shipment_totals(i.shipment)


def my_work(user, *, client: str = "", kind: str = "", limit: int = 300) -> tuple[list[WorkItem], list]:
    """Shipments assigned to this person or ready for their approval, across their organizations.
    Returns (items, organizations hidden because they require two-factor)."""
    orgs = list(member_orgs(user))
    hidden = [o for o in orgs if mfa_missing(user, o)]
    orgs = [o for o in orgs if o not in hidden]
    if client:
        orgs = [o for o in orgs if o.slug == client]
    roles = dict(Membership.objects.filter(user=user, organization__in=orgs).values_list("organization_id", "role"))
    items: dict[int, WorkItem] = {}
    if kind in ("", "assigned"):
        for s in (Shipment.objects.filter(organization__in=orgs, status__in=ACTIVE, assignment__assignee=user)
                  .select_related("organization").order_by("created_at")[:limit]):
            items[s.pk] = WorkItem(s, assigned=True)
    if kind in ("", "approve"):
        approver_orgs = [o for o in orgs if roles.get(o.pk) in APPROVER_ROLES]
        for s in (Shipment.objects.filter(organization__in=approver_orgs, status=Shipment.Status.READY)
                  .select_related("organization").order_by("created_at")[:limit]):
            item = items.get(s.pk) or WorkItem(s)
            item.blockers = approval_blockers(s, user)
            item.can_approve = not item.blockers
            if item.can_approve or item.assigned:
                items[s.pk] = item
    out = sorted(items.values(), key=lambda i: (i.waiting_since, i.shipment.pk))
    return out, hidden   # totals (a query or more per shipment) are added by add_totals for the rows on show only
