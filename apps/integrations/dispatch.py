"""Turn what happens in ShipMatch into webhook events, after the triggering transaction commits.

Sources: audit rows (events.AUDIT_ACTIONS), shipment status changes into needs review / ready, and new
validation issues (issues are re-created on every check, so the ones already announced are remembered).
Best effort: a webhook problem never breaks the action that caused it.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.models import AuditEvent

from . import events
from .models import IssueAnnouncement, WebhookDelivery, WebhookEndpoint, WebhookEvent

log = logging.getLogger(__name__)


def endpoints_for(org, event_type: str) -> list[WebhookEndpoint]:
    return [e for e in WebhookEndpoint.objects.filter(organization=org, enabled=True) if event_type in (e.events or [])]


def webhooks_allowed(org) -> bool:
    from apps.billing.plans import feature_allowed

    return feature_allowed(org, "webhooks")


def enqueue(delivery_id: int) -> None:
    from .tasks import deliver

    try:
        deliver.delay(delivery_id)
    except Exception:  # broker down: the minute sweep picks pending deliveries up
        log.exception("Could not queue webhook delivery %s", delivery_id)


def publish(org, event_type: str, object_type: str, object_id, summary: dict,
            endpoints: list[WebhookEndpoint] | None = None) -> WebhookEvent | None:
    """Store the event and queue one delivery per subscribed endpoint."""
    targets = endpoints if endpoints is not None else endpoints_for(org, event_type)
    if not targets or not webhooks_allowed(org):
        return None
    event = WebhookEvent(organization=org, type=event_type, object_type=object_type, object_id=str(object_id)[:64])
    event.payload = events.build_payload(org, event_type, summary, event.event_id, timezone.now())
    event.save()
    deliveries = [WebhookDelivery.objects.create(endpoint=ep, event=event) for ep in targets]
    for d in deliveries:
        transaction.on_commit(lambda pk=d.pk: enqueue(pk))
    return event


def _safely(fn):
    def run():
        try:
            fn()
        except Exception:
            log.exception("Webhook handler failed")
    return run


def _has_endpoints(org_id) -> bool:
    return WebhookEndpoint.objects.filter(organization_id=org_id, enabled=True).exists()


# --------------------------------------------------------------------------- audit rows


def on_audit_event(sender, instance: AuditEvent, created: bool, **kwargs) -> None:
    if not created or instance.organization_id is None:
        return
    action = instance.action
    if action != "shipment.validated" and action not in events.AUDIT_ACTIONS:
        return
    try:
        if not _has_endpoints(instance.organization_id):
            return  # the usual case: nothing to schedule
    except Exception:
        log.exception("Could not check webhook endpoints")
        return
    pk = instance.pk

    def fire():
        e = AuditEvent.objects.select_related("organization").filter(pk=pk).first()
        if e is None:
            return
        if e.action == "shipment.validated":
            announce_new_issues(e.organization, e.object_id)
            return
        event_type = events.AUDIT_ACTIONS[e.action]
        if not endpoints_for(e.organization, event_type):
            return
        spec = events.EVENT_TYPES.get(event_type)
        built = spec.builder(e) if spec and spec.builder else None
        if built is None:
            return
        object_type, object_id, summary = built
        publish(e.organization, event_type, object_type, object_id, summary)

    try:
        transaction.on_commit(_safely(fire))
    except Exception:
        log.exception("Could not schedule webhook for audit event %s", pk)


def announce_new_issues(org, shipment_id) -> int:
    """issue.created for each open issue on the shipment that wasn't announced before."""
    from apps.shipments.models import ValidationIssue

    targets = endpoints_for(org, "issue.created")
    if not targets:
        return 0
    issues = (ValidationIssue.objects.filter(organization=org, shipment_id=shipment_id, resolved=False)
              .select_related("shipment").order_by("id"))
    sent = 0
    for issue in issues:
        try:
            with transaction.atomic():
                IssueAnnouncement.objects.create(organization=org, shipment_id=issue.shipment_id,
                                                 fingerprint=issue.fingerprint[:200])
        except IntegrityError:
            continue
        publish(org, "issue.created", "ValidationIssue", issue.pk, events.issue_summary(issue), endpoints=targets)
        sent += 1
    return sent


# --------------------------------------------------------------------------- shipment status


STATUS_EVENTS = {"needs_review": "shipment.needs_review", "ready": "shipment.ready"}


def before_shipment_save(sender, instance, update_fields=None, **kwargs) -> None:
    instance._webhook_old_status = None
    if instance.pk is None or (update_fields is not None and "status" not in update_fields):
        return
    try:
        instance._webhook_old_status = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    except Exception:
        instance._webhook_old_status = None


def on_shipment_saved(sender, instance, created: bool, update_fields=None, **kwargs) -> None:
    old = getattr(instance, "_webhook_old_status", None)
    new = instance.status
    if created or old is None or old == new or new not in STATUS_EVENTS:
        return
    event_type, pk, org_id = STATUS_EVENTS[new], instance.pk, instance.organization_id
    try:
        if not _has_endpoints(org_id):
            return
    except Exception:
        log.exception("Could not check webhook endpoints")
        return

    def fire():
        shipment = sender.objects.select_related("organization", "approved_by").filter(pk=pk).first()
        if shipment is None or STATUS_EVENTS.get(shipment.status) != event_type:
            return  # changed again before commit: the later save sends its own event
        if endpoints_for(shipment.organization, event_type):
            publish(shipment.organization, event_type, "Shipment", pk, events.shipment_summary(shipment))

    try:
        transaction.on_commit(_safely(fire))
    except Exception:
        log.exception("Could not schedule webhook for shipment %s", pk)
