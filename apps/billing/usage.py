"""Documents received per billing month, and the limits that follow from the plan.

* 100% of the monthly allowance (soft limit): a banner for the team and one email to the admins per month.
* BILLING_HARD_LIMIT_PERCENT (default 120%): new uploads, emails and API uploads are refused with a message that
  says why and what to do. Nothing else is ever blocked: documents already received can still be reviewed,
  corrected, approved and posted.
* A trial that ended, or a canceled subscription, also pauses new documents (and only that).

What counts: every document a person can review. The files inside a ZIP and the invoices of a split PDF count
one each; the ZIP or batch PDF itself does not; a file received twice counts once.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.core.models import Membership
from apps.documents.models import Document
from apps.documents.services.ingest import RejectedFile

from .plans import Limits, account_for, limits_for

log = logging.getLogger(__name__)


class UsageLimitReached(RejectedFile):
    """New documents are paused for this organization (plan limit, trial ended, subscription canceled)."""


@dataclass
class Usage:
    used: int
    allowance: int | None        # None = no limit
    hard_limit: int | None
    period_start: datetime
    period_end: datetime
    trial: bool = False

    @property
    def percent(self) -> int | None:
        if not self.allowance:
            return None
        return int(round(100 * self.used / self.allowance))

    @property
    def soft_reached(self) -> bool:
        return bool(self.allowance) and self.used >= self.allowance

    @property
    def hard_reached(self) -> bool:
        return self.hard_limit is not None and self.used >= self.hard_limit

    @property
    def bar_percent(self) -> int:
        """Width of the meter (0-100), relative to the hard limit so the overage zone is visible."""
        if not self.hard_limit:
            return 0
        return min(100, int(round(100 * self.used / self.hard_limit)))

    @property
    def allowance_mark(self) -> int:
        """Where 100% of the allowance sits on the meter."""
        if not self.allowance or not self.hard_limit:
            return 100
        return int(round(100 * self.allowance / self.hard_limit))


def _month_bounds(org, now: datetime) -> tuple[datetime, datetime]:
    import zoneinfo

    try:
        tz = zoneinfo.ZoneInfo(org.timezone or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        tz = zoneinfo.ZoneInfo("UTC")
    local = now.astimezone(tz)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def billing_period(org, account=None, now: datetime | None = None) -> tuple[datetime, datetime]:
    """The subscription's current period from Stripe; a trial's own dates; otherwise the calendar month in the
    organization's time zone."""
    now = now or timezone.now()
    account = account if account is not None else account_for(org)
    if account is not None:
        start, end = account.current_period_start, account.current_period_end
        if account.has_subscription and start and end and start <= now < end:
            return start, end
        if account.status == account.Status.TRIALING and not account.has_subscription and account.trial_ends_at:
            start = account.created_at or account.trial_ends_at - timedelta(days=settings.BILLING_TRIAL_DAYS)
            if start <= now < account.trial_ends_at:
                return start, account.trial_ends_at
    return _month_bounds(org, now)


def documents_received(org, start: datetime, end: datetime) -> int:
    return (Document.objects.filter(organization=org, received_at__gte=start, received_at__lt=end)
            .exclude(status__in=Document.CONTAINER_STATUSES).count())


def hard_limit_for(allowance: int | None) -> int | None:
    if not allowance:
        return None
    return max(allowance, math.ceil(allowance * settings.BILLING_HARD_LIMIT_PERCENT / 100))


def usage_for(org, account=None, limits: Limits | None = None, now: datetime | None = None) -> Usage:
    account = account if account is not None else account_for(org)
    limits = limits or limits_for(org, account)
    start, end = billing_period(org, account, now)
    return Usage(used=documents_received(org, start, end), allowance=limits.documents,
                 hard_limit=hard_limit_for(limits.documents), period_start=start, period_end=end, trial=limits.trial)


def cached_usage(org) -> Usage | None:
    """For banners: at most one count per organization per minute."""
    key = f"billing:usage:{org.pk}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    account = account_for(org) if settings.BILLING_ENABLED else None
    if account is None or not account.is_billed:
        return None
    usage = usage_for(org, account)
    cache.set(key, usage, 60)
    return usage


def forget_cached_usage(org_id: int) -> None:
    cache.delete(f"billing:usage:{org_id}")


# --------------------------------------------------------------------------- intake gate


def _date(value: datetime | None) -> str:
    if not value:
        return ""
    local = timezone.localtime(value)
    return f"{local.day} {local:%b %Y}"


def intake_block_reason(org) -> str:
    """'' when a new document may come in; otherwise the message to show (upload page, email log, API)."""
    if not settings.BILLING_ENABLED:
        return ""
    account = account_for(org)
    if account is None or not account.is_billed:
        return ""
    after =" Documents already received can still be reviewed, approved and posted."
    if not account.intake_open:
        if account.trial_over:
            return (f"New documents are paused: the free trial of {org.name} ended on {_date(account.trial_ends_at)}. "
                    "An admin can choose a plan in Settings, Billing." + after)
        return (f"New documents are paused: the subscription of {org.name} is canceled. "
                "An admin can choose a plan again in Settings, Billing." + after)
    usage = usage_for(org, account)
    if usage.hard_reached:
        _notify_once(account, "hard", usage)
        if usage.trial:
            return (f"New documents are paused: {org.name} has received {usage.used:,} documents, the most the free "
                    f"trial includes. An admin can choose a plan in Settings, Billing to keep them coming." + after)
        return (f"New documents are paused: {org.name} has received {usage.used:,} documents this billing month, "
                f"{usage.percent}% of the {usage.allowance:,} in its plan. Intake starts again on "
                f"{_date(usage.period_end)}, or straight away when an admin chooses a bigger plan in Settings, "
                "Billing." + after)
    return ""


def check_room(org, incoming: int) -> None:
    """A ZIP adds every file inside it: refuse it whole when they don't all fit under the plan's limit."""
    if incoming <= 1 or not settings.BILLING_ENABLED:
        return
    account = account_for(org)
    if account is None or not account.is_billed:
        return
    usage = usage_for(org, account)
    if usage.hard_limit is None:
        return
    room = max(0, usage.hard_limit - usage.used)
    if incoming > room:
        raise UsageLimitReached(
            f"This ZIP holds {incoming} documents, but {org.name} has room for {room} more "
            f"{'in the free trial' if usage.trial else 'this billing month'}. Send fewer files at a time, or an admin "
            "can choose a bigger plan in Settings, Billing.")


def check_intake(org) -> None:
    """Called by ingest_bytes for each new file (not for the files inside a ZIP or the parts of a split PDF)."""
    reason = intake_block_reason(org)
    if reason:
        raise UsageLimitReached(reason)


def seat_limit_message(org) -> str:
    """'' when another person may join the organization; otherwise why not."""
    limits = limits_for(org)
    if not limits.users:
        return ""
    members = Membership.objects.filter(organization=org, user__is_active=True).count()
    if members < limits.users:
        return ""
    plan = limits.plan.name if limits.plan else "current"
    return (f"The {plan} plan includes {limits.users} users and {org.name} has {members}. Remove someone first, "
            "or choose a bigger plan in Settings, Billing.")


# --------------------------------------------------------------------------- admin emails at 100% and at the stop


def _notify_once(account, which: str, usage: Usage) -> None:
    """Email the admins once per billing month (the update is the lock: two workers never both send)."""
    from .models import BillingAccount

    fieldname = "soft_notice_for" if which == "soft" else "hard_notice_for"
    updated = (BillingAccount.objects.filter(pk=account.pk).exclude(**{fieldname: usage.period_start})
               .update(**{fieldname: usage.period_start}))
    if not updated:
        return
    setattr(account, fieldname, usage.period_start)
    org_id = account.organization_id
    if which == "hard":
        from apps.core.utils import audit

        audit(account.organization, "billing.intake_paused", account,
              reason=f"{usage.used} documents this billing month, limit {usage.hard_limit}")

    def send():
        from .emails import usage_email

        try:
            usage_email(account.organization, which, usage)
        except Exception:
            log.exception("Could not email admins of organization %s about usage", org_id)

    transaction.on_commit(send)


def after_document_received(org) -> None:
    """A new document arrived: email the admins the first time this month's allowance is reached."""
    forget_cached_usage(org.pk)
    if not settings.BILLING_ENABLED:
        return
    account = account_for(org)
    if account is None or not account.is_billed:
        return
    usage = usage_for(org, account)
    if usage.soft_reached and account.soft_notice_for != usage.period_start:
        _notify_once(account, "soft", usage)
