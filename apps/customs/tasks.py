"""Scheduled work for free time (Celery beat, see CELERY_BEAT_SCHEDULE in config/settings.py)."""
from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def check_free_time() -> int:
    """Hourly: last free day alerts, each organization in its own time zone from CUSTOMS_ALERT_HOUR."""
    from .services.alerts import check_all

    sent = check_all()
    if sent:
        log.info("Sent %s free time alert(s)", sent)
    return sent
