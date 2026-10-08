"""Background work for outgoing webhooks (Celery). With CELERY_TASK_ALWAYS_EAGER the first attempt runs inline
and retries wait for the minute sweep (retry_due_deliveries, Celery beat) so no web request sleeps."""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import WebhookDelivery

log = logging.getLogger(__name__)

STALE_SENDING = timezone.timedelta(minutes=15)
LOST_PENDING = timezone.timedelta(minutes=2)


def claim(delivery_id: int) -> WebhookDelivery | None:
    """Mark a delivery as being sent; None if another worker has it, it's finished, or its retry isn't due yet
    (a late scheduled task must not jump ahead of the backoff)."""
    due = timezone.now() + timezone.timedelta(seconds=5)
    got = WebhookDelivery.objects.filter(pk=delivery_id).filter(
        Q(status=WebhookDelivery.Status.PENDING)
        | Q(status=WebhookDelivery.Status.RETRYING, next_attempt_at__isnull=True)
        | Q(status=WebhookDelivery.Status.RETRYING, next_attempt_at__lte=due)
    ).update(status=WebhookDelivery.Status.SENDING, updated_at=timezone.now())
    if not got:
        return None
    return WebhookDelivery.objects.select_related("endpoint", "endpoint__organization", "event").get(pk=delivery_id)


@shared_task(ignore_result=True)
def deliver(delivery_id: int) -> str:
    from .delivery import attempt

    d = claim(delivery_id)
    if d is None:
        return "skipped"
    outcome = attempt(d)
    if outcome.retry_in is not None and not getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        try:
            deliver.apply_async((delivery_id,), countdown=outcome.retry_in)
        except Exception:
            log.exception("Could not schedule retry of webhook delivery %s; the sweep will pick it up", delivery_id)
    return d.status


@shared_task(ignore_result=True)
def retry_due_deliveries() -> int:
    """Retries that are due, deliveries whose queued task was lost, and attempts a dead worker left behind."""
    now = timezone.now()
    ids = list(WebhookDelivery.objects.filter(
        Q(status=WebhookDelivery.Status.RETRYING, next_attempt_at__lte=now)
        | Q(status=WebhookDelivery.Status.PENDING, created_at__lt=now - LOST_PENDING)
        | Q(status=WebhookDelivery.Status.SENDING, updated_at__lt=now - STALE_SENDING)
    ).order_by("next_attempt_at", "id").values_list("pk", flat=True)[:500])
    WebhookDelivery.objects.filter(pk__in=ids, status=WebhookDelivery.Status.SENDING).update(
        status=WebhookDelivery.Status.RETRYING)
    for pk in ids:
        try:
            deliver.delay(pk)
        except Exception:
            log.exception("Could not queue webhook delivery %s", pk)
    return len(ids)
