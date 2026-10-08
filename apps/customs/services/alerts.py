"""Free time alerts, sent through apps.notifications (registered in CustomsConfig.ready()):

  * "Last free day in N days": when a container's last free day (pick up for demurrage, or return empty for
    detention) is within the organization's lead days, once per container per last free day;
  * "Free time passed": the day after, once per container per last free day, with the estimated daily cost from
    the vendor's approved rates in Rates when there are any.

check_all() runs hourly from Celery beat (apps.customs.tasks.check_free_time). Each organization is checked in its
own time zone, from CUSTOMS_ALERT_HOUR local time, so alerts arrive in the morning. Each alert writes an audit row
(free_time.lfd_soon / free_time.accruing); the notifications app turns those into Slack, Teams or email messages.
A new last free day (an updated notice) can alert again; a returned container never alerts.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from django.conf import settings
from django.db import transaction
from django.urls import reverse

from apps.core.models import Organization
from apps.core.utils import audit

from ..models import ContainerFreeTime, CustomsSettings
from .entry import day_text
from .freetime import daily_rate, org_now, status_for

log = logging.getLogger(__name__)

SOON_ACTION, LATE_ACTION = "free_time.lfd_soon", "free_time.accruing"
EVENT_SOON, EVENT_LATE = "customs.lfd_soon", "customs.free_time_passed"


def _claim(row: ContainerFreeTime, field: str, lfd: date) -> bool:
    """Mark the alert as sent for this last free day; False if another worker already did."""
    return bool(ContainerFreeTime.objects.filter(pk=row.pk).exclude(**{field: lfd}).update(**{field: lfd}))


def check_org(org: Organization, now: datetime | None = None, force: bool = False) -> int:
    local = org_now(org, now)
    if not force and local.hour < int(getattr(settings, "CUSTOMS_ALERT_HOUR", 7)):
        return 0
    today, cfg, sent = local.date(), CustomsSettings.for_org(org), 0
    for row in ContainerFreeTime.objects.filter(organization=org, returned_on__isnull=True).select_related("shipment"):
        st = status_for(row, today, cfg.lfd_alert_days, with_cost=False)
        if st.lfd is None or st.days_left is None:
            continue
        if 0 <= st.days_left <= cfg.lfd_alert_days:
            field, action, days = f"alerted_{st.stage}_soon", SOON_ACTION, st.days_left
        elif st.days_left < 0:
            field, action, days = f"alerted_{st.stage}_late", LATE_ACTION, -st.days_left
        else:
            continue
        if getattr(row, field) == st.lfd:
            continue
        with transaction.atomic():
            if not _claim(row, field, st.lfd):
                continue
            audit(org, action, row, container=row.container_number, shipment=row.shipment.reference,
                  stage=st.stage, last_free_day=st.lfd.isoformat(), days=days, terminal=row.terminal,
                  carrier=row.carrier_name)
        sent += 1
    return sent


def check_all(now: datetime | None = None, force: bool = False) -> int:
    """Every organization with containers not yet returned. One failing organization never stops the others."""
    sent = 0
    orgs = Organization.objects.filter(free_time__returned_on__isnull=True).distinct()
    for org in orgs:
        try:
            sent += check_org(org, now, force)
        except Exception:
            log.exception("Free time check failed for %s", org.slug)
    return sent


# --------------------------------------------------------------------------- messages


def _row(e) -> ContainerFreeTime | None:
    return (ContainerFreeTime.objects.filter(pk=e.object_id, organization=e.organization)
            .select_related("shipment").first())


def _facts(row: ContainerFreeTime, lfd: date) -> list[list[str]]:
    facts = [["Shipment", row.shipment.reference], ["B/L", row.shipment.bl_number or "Not received"],
             ["Container", row.container_number], ["Last free day", day_text(lfd)]]
    if row.terminal:
        facts.append(["Terminal", row.terminal])
    if row.carrier_name:
        facts.append(["Carrier", row.carrier_name])
    return facts


def _url(row: ContainerFreeTime) -> str:
    from apps.notifications.events import absolute

    return absolute(reverse("review:shipment", args=[row.shipment_id]) + "#free-time")


def soon_message(e):
    from apps.notifications.events import Message

    row = _row(e)
    if row is None:
        return None
    data = e.data or {}
    stage, days = data.get("stage", "demurrage"), int(data.get("days") or 0)
    lfd = date.fromisoformat(data["last_free_day"])
    when = "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
    if stage == "detention":
        text = f"Return the empty container {row.container_number} by {day_text(lfd)} to avoid detention."
    else:
        text = (f"Pick up {row.container_number} from {row.terminal or 'the terminal'} by {day_text(lfd)} "
                "to avoid demurrage.")
    facts = _facts(row, lfd)
    rate = daily_rate(row, stage, lfd)
    if rate:
        facts.append(["After that", f"about {rate[1]} {rate[0]:,.2f} a day ({rate[2]})"])
    return Message(EVENT_SOON, f"Last free day {when}: {row.container_number}", text, facts, _url(row),
                   "Open the shipment", "warning", e.organization.name)


def late_message(e):
    from apps.notifications.events import Message

    row = _row(e)
    if row is None:
        return None
    data = e.data or {}
    stage, days = data.get("stage", "demurrage"), int(data.get("days") or 1)
    lfd = date.fromisoformat(data["last_free_day"])
    word = "detention" if stage == "detention" else "demurrage"
    missed = "returned empty" if stage == "detention" else "picked up"
    text = f"{row.container_number} wasn't {missed} by its last free day, {day_text(lfd)}. "
    facts = _facts(row, lfd)
    rate = daily_rate(row, stage, lfd)
    if rate:
        so_far = rate[0] * days
        text += (f"Estimated cost: {rate[1]} {rate[0]:,.2f} a day ({rate[2]}), {rate[1]} {so_far:,.2f} so far.")
        facts.append(["Estimated daily cost", f"{rate[1]} {rate[0]:,.2f}"])
    else:
        text += f"No approved {word} rate is on file for {row.carrier_name or 'this vendor'}, so the cost isn't estimated."
    facts.append(["Days past", str(days)])
    return Message(EVENT_LATE, f"Free time passed: {word} is accruing on {row.container_number}", text, facts,
                   _url(row), "Open the shipment", "error", e.organization.name)
