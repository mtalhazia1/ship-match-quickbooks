"""Keeps the CaughtCharge ledger in step with validation issues that carry an amount at risk.

Validation deletes a shipment's open issues and creates the current ones on every run, inside
one transaction. The ledger follows along with two signal handlers:
  * an issue with an amount is saved   -> its catch is created or updated and points at it,
  * an issue is deleted                -> its catch is marked cleared (no longer found).
When the same catch is found again in the same run, the second step is undone by the first, so
outside the transaction a catch is "cleared" only when the check really stopped finding it.

Why it cleared is recorded when known: re-checks started from the Rates pages (a quote, an
approved extra charge or the tolerance changed) run inside `clearing_because(RATES)`; anything
else (a corrected field, a moved or replaced document, a rejected-and-reopened shipment) counts as
the invoice having been corrected or removed.
"""
from __future__ import annotations

import contextlib
import contextvars
from decimal import Decimal

from django.db.models.signals import post_delete, post_save
from django.utils import timezone

from apps.accounting.models import vendor_key

_reason: contextvars.ContextVar[str] = contextvars.ContextVar("rates_clear_reason", default="")


@contextlib.contextmanager
def clearing_because(reason: str):
    token = _reason.set(reason)
    try:
        yield
    finally:
        _reason.reset(token)


def scope_for(issue) -> str:
    if issue.document_id:
        return f"d{issue.document_id}"
    return f"s{issue.shipment_id or 0}"


def catch_key_for(issue) -> str:
    data = issue.data or {}
    detail = data.get("catch_key")
    if detail is None:
        detail = data.get("key", "")
    return f"{issue.code}:{detail}"[:160]


def record(issue) -> None:
    """Create or update the catch for one issue that has an amount at risk."""
    from .models import CaughtCharge

    amount = issue.amount_at_risk
    if amount is None or amount <= 0:
        return
    amount = Decimal(amount).quantize(Decimal("0.01"))
    vname = ""
    if issue.document_id:
        vname = issue.document.field("vendor_name") or issue.document.field("carrier_name") or ""
    now = timezone.now()
    catch, created = CaughtCharge.objects.get_or_create(
        organization_id=issue.organization_id, scope=scope_for(issue), catch_key=catch_key_for(issue),
        defaults={"code": issue.code, "charge_code": (issue.data or {}).get("charge_code", "")[:30],
                  "shipment_id": issue.shipment_id, "document_id": issue.document_id, "issue_id": issue.pk,
                  "vendor_name": str(vname)[:200], "vendor_key": vendor_key(str(vname))[:200],
                  "currency": (issue.currency or "")[:3], "amount_caught": amount, "amount_latest": amount,
                  "first_caught_at": issue.created_at or now, "last_seen_at": now})
    if created:
        return
    catch.issue_id = issue.pk
    catch.shipment_id = issue.shipment_id or catch.shipment_id
    if _reason.get() == CaughtCharge.Cleared.RATES:
        # The quote or tolerance changed, not the invoice: move the caught amount by the same
        # difference, so a drop never shows up as money prevented and earlier reductions stay.
        catch.amount_caught = max(amount, catch.amount_caught + (amount - catch.amount_latest))
    else:
        catch.amount_caught = max(catch.amount_caught, amount)
    catch.amount_latest = amount
    catch.currency = (issue.currency or catch.currency)[:3]
    if vname:
        catch.vendor_name, catch.vendor_key = str(vname)[:200], vendor_key(str(vname))[:200]
    catch.last_seen_at = now
    catch.cleared_at, catch.cleared_reason = None, ""
    catch.save()


def _on_issue_saved(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    record(instance)


def _on_issue_deleted(sender, instance, **kwargs):
    from .models import CaughtCharge

    CaughtCharge.objects.filter(issue_id=instance.pk).update(
        issue_id=None, cleared_at=timezone.now(), cleared_reason=_reason.get() or CaughtCharge.Cleared.INVOICE)


def connect() -> None:
    from apps.shipments.models import ValidationIssue

    post_save.connect(_on_issue_saved, sender=ValidationIssue, dispatch_uid="rates-ledger-save")
    post_delete.connect(_on_issue_deleted, sender=ValidationIssue, dispatch_uid="rates-ledger-delete")


def sync(org) -> int:
    """Add catches for issues created without the signal (bulk inserts, data loaded before this app)."""
    from apps.shipments.models import ValidationIssue

    from .models import CaughtCharge

    rows = CaughtCharge.objects.filter(organization=org)
    known = set(rows.values_list("scope", "catch_key"))
    linked = rows.filter(issue_id__isnull=False).values_list("issue_id", flat=True)
    added = 0
    for issue in (ValidationIssue.objects.filter(organization=org, amount_at_risk__gt=0)
                  .exclude(pk__in=linked).select_related("document").order_by("id")):
        key = (scope_for(issue), catch_key_for(issue))
        if key in known:
            continue
        record(issue)
        known.add(key)
        added += 1
    return added
