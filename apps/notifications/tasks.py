"""Background work for alerts (Celery). With CELERY_TASK_ALWAYS_EAGER these run inline."""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import Delivery

log = logging.getLogger(__name__)

STALE_AFTER = timezone.timedelta(minutes=15)


def claim(delivery_id: int) -> Delivery | None:
    """Mark a delivery as being sent; None if another worker has it or it is finished."""
    got = Delivery.objects.filter(pk=delivery_id, status__in=[Delivery.Status.PENDING, Delivery.Status.RETRYING]
                                  ).update(status=Delivery.Status.SENDING, updated_at=timezone.now())
    if not got:
        return None
    return Delivery.objects.select_related("channel", "channel__organization").get(pk=delivery_id)


@shared_task(ignore_result=True)
def send_delivery(delivery_id: int) -> str:
    from .delivery import attempt

    d = claim(delivery_id)
    if d is None:
        return "skipped"
    outcome = attempt(d)
    # Without workers (eager mode) a retry runs inside the web request that raised the alert, so only quick
    # failures (429, 5xx) are retried at once; timeouts wait for retry_stalled_deliveries.
    inline = getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)
    if outcome.retry_in is not None and not (inline and outcome.slow):
        try:
            send_delivery.apply_async((delivery_id,), countdown=outcome.retry_in)
        except Exception:
            log.exception("Could not schedule retry of alert delivery %s; the sweeper will pick it up", delivery_id)
    return d.status


@shared_task(ignore_result=True)
def retry_stalled_deliveries() -> int:
    """Safety net: deliveries whose scheduled retry never ran (worker restarted, broker lost the task)."""
    now = timezone.now()
    stale = now - STALE_AFTER
    ids = list(Delivery.objects.filter(
        Q(status=Delivery.Status.PENDING, created_at__lt=stale)
        | Q(status=Delivery.Status.RETRYING, next_attempt_at__lt=stale)
        | Q(status=Delivery.Status.SENDING, updated_at__lt=stale)).values_list("pk", flat=True)[:500])
    # A delivery stuck in "sending" lost its worker mid-attempt; try it again.
    Delivery.objects.filter(pk__in=ids, status=Delivery.Status.SENDING).update(status=Delivery.Status.RETRYING)
    for pk in ids:
        try:
            send_delivery.delay(pk)
        except Exception:
            log.exception("Could not queue alert delivery %s", pk)
    return len(ids)


@shared_task(ignore_result=True)
def send_daily_digests() -> int:
    from .digest import send_due_digests

    return send_due_digests()
