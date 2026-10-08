"""Decide who gets an alert and queue the deliveries.

Everything here is best effort: an alert must never break the action that caused it, so errors are
logged and swallowed. Deliveries are queued only after the triggering transaction commits.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from django.db import transaction

from apps.core.models import AuditEvent

from . import events
from .events import Message
from .models import Channel, Delivery

log = logging.getLogger(__name__)


def channels_for(org, event: str) -> list[Channel]:
    return [c for c in Channel.objects.filter(organization=org, enabled=True) if event in (c.events or [])]


def enqueue(delivery_id: int) -> None:
    from .tasks import send_delivery

    try:
        send_delivery.delay(delivery_id)
    except Exception:  # broker down, or an eager task blew up: the sweeper retries pending deliveries
        log.exception("Could not queue alert delivery %s", delivery_id)


def notify(org, event: str, build: Callable[[], Message | None], object_type: str = "", object_id: str = "",
           channels: list[Channel] | None = None) -> list[Delivery]:
    """Create one delivery per subscribed channel and queue them. Returns the deliveries."""
    try:
        targets = channels if channels is not None else channels_for(org, event)
        if not targets:
            return []
        msg = build()
        if msg is None:
            return []
        out = [Delivery.objects.create(organization=org, channel=c, event=event, title=msg.title[:250],
                                       message=msg.as_dict(), object_type=object_type, object_id=str(object_id)[:64])
               for c in targets]
    except Exception:
        log.exception("Could not prepare %s alert for %s", event, getattr(org, "slug", org))
        return []
    for d in out:
        enqueue(d.pk)
    return out


def _safely(fn: Callable[[], object]) -> Callable[[], None]:
    def run():
        try:
            fn()
        except Exception:
            log.exception("Alert handler failed")
    return run


# --------------------------------------------------------------------------- signal handlers


def on_audit_event(sender, instance: AuditEvent, created: bool, **kwargs) -> None:
    if not created or instance.action not in events.AUDIT_EVENTS or instance.organization_id is None:
        return
    event, pk = events.AUDIT_EVENTS[instance.action], instance.pk

    def fire():
        e = AuditEvent.objects.select_related("organization").filter(pk=pk).first()
        if e is not None:
            notify(e.organization, event, lambda: events.audit_message(event, e), e.object_type, e.object_id)

    try:
        transaction.on_commit(_safely(fire))
    except Exception:
        log.exception("Could not schedule alert for audit event %s", pk)


STATUS_EVENTS = {"needs_review": events.NEEDS_REVIEW, "ready": events.READY}


def before_shipment_save(sender, instance, update_fields=None, **kwargs) -> None:
    """Remember the stored status so a change can be detected after saving."""
    if instance.pk is None or (update_fields is not None and "status" not in update_fields):
        instance._alert_old_status = None
        return
    try:
        instance._alert_old_status = (sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first())
    except Exception:
        instance._alert_old_status = None


def on_shipment_saved(sender, instance, created: bool, update_fields=None, **kwargs) -> None:
    old = getattr(instance, "_alert_old_status", None)
    new = instance.status
    if created or old is None or old == new or new not in STATUS_EVENTS:
        return
    event, pk, org_id = STATUS_EVENTS[new], instance.pk, instance.organization_id

    def fire():
        shipment = sender.objects.select_related("organization").filter(pk=pk, organization_id=org_id).first()
        if shipment is None or STATUS_EVENTS.get(shipment.status) != event:
            return  # changed again before commit: the later save sends its own alert
        notify(shipment.organization, event, lambda: events.shipment_message(shipment, event), "Shipment", pk)

    try:
        transaction.on_commit(_safely(fire))
    except Exception:
        log.exception("Could not schedule alert for shipment %s", pk)
