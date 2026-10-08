"""The daily summary: what needs people today, sent once a day at each organization's chosen local hour."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from django.db.models import Min
from django.urls import reverse
from django.utils import timezone

from apps.accounting.models import PostedBill
from apps.disputes.models import Dispute
from apps.disputes.reports import overdue_q
from apps.disputes.savings import org_zone
from apps.shipments.models import Shipment, ValidationIssue

from . import events
from .events import Message, absolute
from .models import Channel, NotificationSettings

DIGEST_WINDOW_HOURS = 3  # if the scheduler was down at the chosen hour, still send within this window


@dataclass
class Digest:
    needs_review: int = 0
    ready: int = 0
    failed_postings: int = 0
    overdue_disputes: int = 0
    oldest_waiting_days: int | None = None
    at_risk: dict[str, Decimal] = field(default_factory=dict)
    at_risk_home: Decimal | None = Decimal("0.00")

    @property
    def empty(self) -> bool:
        return not (self.needs_review or self.ready or self.failed_postings or self.overdue_disputes or self.at_risk)


def build(org) -> Digest:
    ships = Shipment.objects.filter(organization=org)
    d = Digest(
        needs_review=ships.filter(status=Shipment.Status.NEEDS_REVIEW).count(),
        ready=ships.filter(status=Shipment.Status.READY).count(),
        failed_postings=PostedBill.objects.filter(organization=org, status=PostedBill.Status.FAILED).count(),
        overdue_disputes=Dispute.objects.filter(organization=org).filter(overdue_q(org)).count(),
    )
    oldest = ships.filter(status=Shipment.Status.NEEDS_REVIEW).aggregate(m=Min("created_at"))["m"]
    if oldest:
        d.oldest_waiting_days = (timezone.now() - oldest).days
    open_issues = ValidationIssue.objects.filter(
        organization=org, resolved=False, amount_at_risk__isnull=False,
        shipment__status__in=[Shipment.Status.OPEN, Shipment.Status.NEEDS_REVIEW, Shipment.Status.READY])
    for cur, amount in open_issues.values_list("currency", "amount_at_risk"):
        cur = (cur or org.home_currency).upper()
        d.at_risk[cur] = d.at_risk.get(cur, Decimal("0.00")) + amount
    for cur, amount in d.at_risk.items():
        converted = org.to_home(amount, cur)
        if converted is None:
            d.at_risk_home = None
        elif d.at_risk_home is not None:
            d.at_risk_home += converted
    return d


def message(org, d: Digest) -> Message:
    waiting = f" (oldest {d.oldest_waiting_days} day{'s' if d.oldest_waiting_days != 1 else ''})" \
        if d.needs_review and d.oldest_waiting_days else ""
    if not d.at_risk:
        risk = "None"
    elif d.at_risk_home is not None:
        risk = f"{org.home_currency} {d.at_risk_home:,.2f}"
    else:  # a currency without an exchange rate: show each currency
        risk = ", ".join(f"{k} {v:,.2f}" for k, v in d.at_risk.items())
    facts = [
        ["Shipments needing review", f"{d.needs_review}{waiting}"],
        ["Ready to approve", str(d.ready)],
        ["Bills that failed to post", str(d.failed_postings)],
        ["Disputes past follow-up", str(d.overdue_disputes)],
        ["Money at risk on open issues", risk],
    ]
    parts = []
    if d.needs_review:
        parts.append(f"{d.needs_review} to review")
    if d.ready:
        parts.append(f"{d.ready} to approve")
    if d.failed_postings:
        parts.append(f"{d.failed_postings} failed posting{'s' if d.failed_postings != 1 else ''}")
    if d.overdue_disputes:
        parts.append(f"{d.overdue_disputes} overdue dispute{'s' if d.overdue_disputes != 1 else ''}")
    text = ("Today: " + ", ".join(parts) + ".") if parts else "Nothing is waiting on your team today."
    tone = "error" if d.failed_postings else ("warning" if d.needs_review or d.overdue_disputes else "info")
    return Message(events.DIGEST, f"Daily summary for {org.name}", text, facts,
                   absolute(reverse("core:dashboard")), "Open the dashboard", tone, org.name)


def due(org, s: NotificationSettings, now: datetime | None = None) -> bool:
    local = timezone.localtime(now or timezone.now(), org_zone(org))
    in_window = s.digest_hour <= local.hour < s.digest_hour + DIGEST_WINDOW_HOURS
    return in_window and s.last_digest_on != local.date()


def send_due_digests(now: datetime | None = None) -> int:
    """Called hourly. Claims each organization's day before sending, so two workers never both send."""
    from .dispatch import notify

    sent = 0
    by_org: dict[int, list[Channel]] = {}
    for c in Channel.objects.filter(enabled=True).select_related("organization"):
        if events.DIGEST in (c.events or []):
            by_org.setdefault(c.organization_id, []).append(c)
    for channels in by_org.values():
        org = channels[0].organization
        s = NotificationSettings.for_org(org)
        if not due(org, s, now):
            continue
        today = timezone.localtime(now or timezone.now(), org_zone(org)).date()
        claimed = NotificationSettings.objects.filter(pk=s.pk).exclude(last_digest_on=today).update(last_digest_on=today)
        if not claimed:
            continue
        d = build(org)
        if d.empty:
            continue  # nothing to report: no message, no noise
        if notify(org, events.DIGEST, lambda o=org, dg=d: message(o, dg), "Organization", org.pk, channels=channels):
            sent += 1
    return sent
