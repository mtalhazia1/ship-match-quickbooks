"""Checks and approval rules for invoices shared by several shipments.

Registered from LandedConfig.ready():
  * `check_shared` (a shipment rule): each shipment with a share is checked against its share: the split
    must add up to the invoice total, and a person must confirm it. (Errors on the invoice itself are
    raised on the shipment it is matched to; the shared panel shows them live on the others.)
  * `approval_blockers`: no shipment in a split is approved before the split is confirmed; the invoice's
    own shipment is approved last, because the invoice is posted once, with it; approval limits count
    the shares a shipment carries.
"""
from __future__ import annotations

from decimal import Decimal

from apps.core.permissions import membership_for
from apps.shipments.models import Shipment
from apps.shipments.services.validation import ERROR, WARNING, IssueSpec

from ..models import InvoiceAllocation, SharedInvoice
from . import allocation
from .charges import invoice_number


def check_shared(shipment: Shipment, docs):
    own = {d.pk for d in docs}
    for si in allocation.for_shipment(shipment):
        doc = si.document
        if si.status != SharedInvoice.Status.ACTIVE:
            continue
        is_primary = doc.pk in own
        number = invoice_number(doc)
        locked = (si.detected or {}).get("locked") or []
        if is_primary and locked:
            refs = ", ".join(item.get("reference", "") for item in locked)
            yield IssueSpec("shared_invoice_locked_ref", WARNING,
                            f"Invoice {number} also names {refs}, which {'is' if len(locked) == 1 else 'are'} already "
                            "approved, so no share was given to "
                            f"{'it' if len(locked) == 1 else 'them'}. The whole invoice stays here unless "
                            f"{'it is' if len(locked) == 1 else 'they are'} reopened.",
                            doc, {"key": f"{doc.pk}:{','.join(str(i.get('id')) for i in locked)}", "invoice": doc.pk})
        rows = allocation.allocations(doc)
        if len(rows) < 2:
            continue
        mine = next((r for r in rows if r.shipment_id == shipment.pk), None)
        if mine is None and not is_primary:
            continue
        total, currency = allocation.invoice_total(doc)
        assigned = sum((r.amount for r in rows), Decimal("0.00"))
        others = ", ".join(r.shipment.reference for r in rows if r.shipment_id != shipment.pk)
        if not allocation.adds_up(doc, rows):
            if any(r.currency != currency for r in rows):
                message = (f"Invoice {number} is in {currency}, but its shares were split in "
                           f"{', '.join(sorted({r.currency for r in rows}))}. Split it again.")
            else:
                message = (f"The shares of invoice {number} add up to {currency} {assigned:,.2f}, but the invoice "
                           f"total is {currency} {total:,.2f}." if total is not None else
                           f"Invoice {number} has no total, so its shares can't be checked.")
            yield IssueSpec("shared_split_mismatch", ERROR, message, doc if is_primary else None,
                            {"key": str(doc.pk), "invoice": doc.pk},
                            amount_at_risk=(assigned - total) if total is not None and assigned > total else None,
                            currency=currency)
        elif not allocation.is_confirmed(si, rows):
            share = mine.amount if mine else Decimal("0.00")
            yield IssueSpec("shared_invoice_split", WARNING,
                            f"Invoice {number} ({currency} {total:,.2f}) also covers {others}. This shipment's share is "
                            f"{currency} {share:,.2f}, split {si.get_basis_display().lower()}. Check the split and "
                            "confirm it.", doc if is_primary else None,
                            {"key": f"{doc.pk}:{allocation.split_hash(total, currency, rows)}", "invoice": doc.pk})


def _active_splits(shipment: Shipment):
    """(shared invoice, its document, the split rows, whether it is posted with this shipment) for every shared
    invoice this shipment carries a share of."""
    for si in allocation.for_shipment(shipment):
        if si.status != SharedInvoice.Status.ACTIVE:
            continue
        doc = si.document
        rows = allocation.allocations(doc)
        if len(rows) < 2:
            continue
        is_primary = hasattr(doc, "match") and doc.match.shipment_id == shipment.pk
        if not is_primary and not any(r.shipment_id == shipment.pk for r in rows):
            continue
        yield si, doc, rows, is_primary


def unconfirmed_splits(shipment: Shipment) -> list[str]:
    """Numbers of the shared invoices whose split still has to be confirmed before this shipment can be approved.
    Until then the other shipments' containers on the invoice show up as errors; confirming clears them."""
    return [invoice_number(doc) for si, doc, rows, _ in _active_splits(shipment)
            if allocation.adds_up(doc, rows) and not allocation.is_confirmed(si, rows)]


def prefetch_approval(shipments) -> None:
    """Marks the shipments that have nothing to do with a shared invoice, so approval_blockers can skip them."""
    ids = [s.pk for s in shipments]
    involved = set(InvoiceAllocation.objects.filter(shipment_id__in=ids).values_list("shipment_id", flat=True))
    involved |= set(SharedInvoice.objects.filter(document__match__shipment_id__in=ids)
                    .values_list("document__match__shipment_id", flat=True))
    for s in shipments:
        s._no_shared_invoices = s.pk not in involved


def approval_blockers(shipment: Shipment, user) -> list[str]:
    if getattr(shipment, "_no_shared_invoices", False):
        return []
    reasons = []
    for si, doc, rows, is_primary in _active_splits(shipment):
        number = invoice_number(doc)
        if allocation.adds_up(doc, rows) and not allocation.is_confirmed(si, rows):
            reasons.append(f"Confirm the split of shared invoice {number} first (Shared invoices, on this page).")
        if is_primary:
            waiting = [r.shipment.reference for r in rows
                       if r.shipment_id != shipment.pk and r.shipment.status not in allocation.DONE]
            if waiting:
                reasons.append(f"Approve {allocation._and(waiting)} first: {'it carries a share' if len(waiting) == 1 else 'they carry shares'} "
                               f"of invoice {number}, which is posted once, with this shipment.")
    reasons += _limit_with_shares(shipment, user)
    return reasons


def _limit_with_shares(shipment: Shipment, user) -> list[str]:
    """Approval limits count the shares of invoices matched to other shipments that this shipment carries."""
    from apps.shipments.services.approval import shipment_totals

    org = shipment.organization
    membership = membership_for(user, org)
    limit = membership.approval_limit if membership else None
    if limit is None:
        return []
    shares = [r for d in allocation.shared_with(shipment, exclude=shipment.documents.values_list("pk", flat=True))
              for r in allocation.allocations(d) if r.shipment_id == shipment.pk]
    if not shares:
        return []
    base = shipment_totals(shipment)
    if base.home is None:
        return []  # the approval rules already say the limit can't be checked
    extra = Decimal("0.00")
    for r in shares:
        home = org.to_home(r.amount, r.currency)
        if home is None:
            return [f"No exchange rate set for {r.currency}, so your approval limit can't be checked with this "
                    "shipment's share of a shared invoice. An admin can add the rate, or an approver without a limit "
                    "can approve."]
        extra += home
    if base.home <= limit < base.home + extra:
        return [f"With its share of shared invoices, this shipment's total is {org.home_currency} "
                f"{base.home + extra:,.2f}, above your approval limit of {org.home_currency} {limit:,.2f}."]
    return []
