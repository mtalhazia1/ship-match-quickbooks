"""Who may approve a shipment: role, open errors, maker-checker, and approval limits.

Fails closed: if a limit applies and an amount cannot be converted to the home currency,
the shipment needs an approver without a limit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db.models import Count

from apps.core.models import AuditEvent
from apps.core.permissions import has_perm, membership_for
from apps.documents.models import Document
from apps.shipments.models import Shipment, ValidationIssue

# Actions that make a person a "maker" (preparer) of a shipment.
MAKER_ACTIONS = {"field.corrected", "document.moved", "document.received", "document.type_changed", "issue.resolved"}

# Other apps add approval rules without editing this file: call register_approval_blocker(fn) from their
# AppConfig.ready(). fn(shipment, user) returns a list of reasons (empty = no objection).
APPROVAL_BLOCKERS = []
# Optional companions: prefetch(shipments) loads in bulk what a blocker would query per shipment, for pages that
# check many shipments at once (My work). It stores the result on the instances; the blocker reads it if present.
APPROVAL_PREFETCHERS = []


def register_approval_blocker(fn, prefetch=None) -> None:
    if fn not in APPROVAL_BLOCKERS:
        APPROVAL_BLOCKERS.append(fn)
    if prefetch is not None and prefetch not in APPROVAL_PREFETCHERS:
        APPROVAL_PREFETCHERS.append(prefetch)


def prefetch_approval(shipments) -> None:
    """Load in a fixed number of queries what approval_blockers would otherwise query for each shipment."""
    shipments = list(shipments)
    if not shipments:
        return
    ids = [s.pk for s in shipments]
    errors = dict(ValidationIssue.objects.filter(shipment_id__in=ids, resolved=False,
                                                 severity=ValidationIssue.Severity.ERROR)
                  .values("shipment_id").annotate(n=Count("id")).values_list("shipment_id", "n"))
    by_shipment = _makers_by_shipment(ids)
    for s in shipments:
        s._open_errors = errors.get(s.pk, 0)
        s._makers = by_shipment.get(s.pk, set())
    for prefetch in APPROVAL_PREFETCHERS:
        prefetch(shipments)


# Reasons an approved shipment's bills must not be posted yet (e.g. an invoice shared with shipments that
# aren't approved). fn(shipment) returns a list of reasons; register from AppConfig.ready().
POSTING_BLOCKERS = []


def register_posting_blocker(fn) -> None:
    if fn not in POSTING_BLOCKERS:
        POSTING_BLOCKERS.append(fn)


def posting_blockers(shipment: Shipment) -> list[str]:
    reasons: list[str] = []
    for blocker in POSTING_BLOCKERS:
        reasons.extend(blocker(shipment))
    return reasons


@dataclass
class Totals:
    by_currency: dict[str, Decimal] = field(default_factory=dict)  # invoices minus credit notes
    home: Decimal | None = Decimal("0.00")      # None if some currency has no exchange rate
    missing_rates: list[str] = field(default_factory=list)
    credits: dict[str, Decimal] = field(default_factory=dict)       # credit notes, as positive amounts


def shipment_totals(shipment: Shipment) -> Totals:
    """What the shipment costs: payable invoices net of credit notes, per currency and in the home currency."""
    org = shipment.organization
    t = Totals()
    for doc in shipment.documents.prefetch_related("fields"):
        if not doc.posts_to_accounting:
            continue
        try:
            amount = Decimal(str(doc.field("total_amount"))).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError, TypeError):
            continue
        cur = (doc.field("currency") or org.home_currency).upper()
        if doc.is_credit:
            amount = abs(amount)
            t.credits[cur] = t.credits.get(cur, Decimal("0.00")) + amount
            amount = -amount
        t.by_currency[cur] = t.by_currency.get(cur, Decimal("0.00")) + amount
    for cur, amount in t.by_currency.items():
        converted = org.to_home(amount, cur)
        if converted is None:
            t.missing_rates.append(cur)
        elif t.home is not None:
            t.home += converted
    if t.missing_rates:
        t.home = None
    return t


def makers(shipment: Shipment) -> set[int]:
    """User IDs who prepared this shipment (uploaded, edited, moved documents or accepted issues)."""
    cached = getattr(shipment, "_makers", None)
    if cached is not None:
        return cached
    doc_ids = [str(pk) for pk in Document.objects.filter(match__shipment=shipment).values_list("pk", flat=True)]
    issue_ids = [str(pk) for pk in ValidationIssue.objects.filter(shipment=shipment).values_list("pk", flat=True)]
    events = AuditEvent.objects.filter(action__in=MAKER_ACTIONS, actor__isnull=False).filter(
        object_type__in=["Document", "ValidationIssue"])
    ids = set()
    for e in events.filter(object_type="Document", object_id__in=doc_ids).values_list("actor_id", flat=True):
        ids.add(e)
    for e in events.filter(object_type="ValidationIssue", object_id__in=issue_ids).values_list("actor_id", flat=True):
        ids.add(e)
    return ids


def approval_blockers(shipment: Shipment, user) -> list[str]:
    """Reasons this user cannot approve this shipment now. Empty list = may approve."""
    org = shipment.organization
    reasons: list[str] = []
    if shipment.is_locked:
        return ["This shipment is already approved."]
    if shipment.status == Shipment.Status.REJECTED:
        reasons.append("This shipment was rejected. Reopen it first.")
    errors = getattr(shipment, "_open_errors", None)
    if errors is None:
        errors = shipment.issues.filter(resolved=False, severity=ValidationIssue.Severity.ERROR).count()
    if errors:
        reasons.append(f"Resolve {errors} open error{'s' if errors != 1 else ''} first.")
    for blocker in APPROVAL_BLOCKERS:
        reasons.extend(blocker(shipment, user))
    if not has_perm(user, org, "approve"):
        reasons.append("Only approvers and admins can approve shipments.")
        return reasons
    if org.maker_checker and user.pk in makers(shipment):
        reasons.append("You prepared this shipment, so another approver must approve it (maker-checker rule).")
    membership = membership_for(user, org)
    limit = membership.approval_limit if membership else None
    if limit is not None:
        totals = shipment_totals(shipment)
        if totals.home is None:
            reasons.append(f"No exchange rate set for {', '.join(totals.missing_rates)}, so your approval limit "
                           "cannot be checked. An admin can add the rate, or an approver without a limit can approve.")
        elif totals.home > limit:
            reasons.append(f"Shipment total {org.home_currency} {totals.home:,.2f} is above your approval limit "
                           f"of {org.home_currency} {limit:,.2f}.")
    return reasons


def _makers_by_shipment(shipment_ids: list[int]) -> dict[int, set[int]]:
    """makers() for many shipments in three queries."""
    docs = {str(pk): sid for pk, sid in Document.objects.filter(match__shipment_id__in=shipment_ids)
            .values_list("pk", "match__shipment_id")}
    issues = {str(pk): sid for pk, sid in ValidationIssue.objects.filter(shipment_id__in=shipment_ids)
              .values_list("pk", "shipment_id")}
    out: dict[int, set[int]] = {}
    events = AuditEvent.objects.filter(action__in=MAKER_ACTIONS, actor__isnull=False)
    for object_type, owners in (("Document", docs), ("ValidationIssue", issues)):
        if not owners:
            continue
        for object_id, actor_id in (events.filter(object_type=object_type, object_id__in=list(owners))
                                    .values_list("object_id", "actor_id")):
            out.setdefault(owners[object_id], set()).add(actor_id)
    return out
