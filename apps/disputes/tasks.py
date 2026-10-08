"""Scheduled work for disputes (Celery beat, see CELERY_BEAT_SCHEDULE)."""
from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction

from apps.core.utils import audit

from .evidence import money
from .models import Dispute, DisputeEvent
from .savings import org_today

log = logging.getLogger(__name__)


def flag_overdue(now_dates: dict | None = None) -> list[Dispute]:
    """Report each dispute once per follow-up date, the day after that date in the organization's time zone.

    Moving the follow-up date makes the dispute eligible again when the new date passes.
    """
    flagged = []
    today_by_org = dict(now_dates or {})
    candidates = (Dispute.objects.filter(status__in=Dispute.WAITING, follow_up_on__isnull=False)
                  .select_related("organization"))
    for d in candidates:
        org = d.organization
        today = today_by_org.setdefault(org.pk, org_today(org))
        if d.follow_up_on >= today or d.overdue_flagged_for == d.follow_up_on:
            continue
        with transaction.atomic():
            claimed = (Dispute.objects.filter(pk=d.pk, status__in=Dispute.WAITING, follow_up_on=d.follow_up_on)
                       .exclude(overdue_flagged_for=d.follow_up_on).update(overdue_flagged_for=d.follow_up_on))
            if not claimed:
                continue  # another worker got it, or the dispute moved on
            days = (today - d.follow_up_on).days
            DisputeEvent.objects.create(dispute=d, kind=DisputeEvent.Kind.OVERDUE,
                                        text=f"No answer by the follow-up date ({d.follow_up_on:%d %b %Y}).",
                                        data={"follow_up_on": d.follow_up_on.isoformat()})
            audit(org, "dispute.overdue", d, reference=d.reference, vendor=d.vendor_name,
                  amount=money(d.amount_disputed, d.currency), follow_up_on=d.follow_up_on.isoformat(),
                  days_overdue=days, shipment=d.shipment_reference)
        flagged.append(d)
    return flagged


@shared_task
def flag_overdue_disputes() -> int:
    flagged = flag_overdue()
    if flagged:
        log.info("Flagged %s overdue dispute(s)", len(flagged))
    return len(flagged)
