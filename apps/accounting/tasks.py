import logging

from celery import shared_task

from apps.shipments.models import Shipment

log = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(ConnectionError, TimeoutError), retry_backoff=True, max_retries=5)
def post_shipment_task(self, shipment_id: int, user_id: int | None = None):
    from django.contrib.auth import get_user_model

    from apps.accounting.services.posting import post_shipment

    actor = get_user_model().objects.filter(pk=user_id).first() if user_id else None
    return post_shipment(Shipment.objects.get(pk=shipment_id), actor=actor)


@shared_task
def sync_all_payments():
    """Celery beat (hourly): read payment status for every organization with an accounting connection.
    Each organization is read at most once per PAYMENT_SYNC_HOURS, whatever the beat schedule says."""
    from django.conf import settings

    from apps.accounting.models import QBOConnection, XeroConnection

    if not getattr(settings, "PAYMENT_SYNC_ENABLED", True):
        return 0
    org_ids = set(QBOConnection.objects.filter(needs_reconnect=False).values_list("organization_id", flat=True))
    org_ids |= set(XeroConnection.objects.filter(needs_reconnect=False).exclude(tenant_id="")
                   .values_list("organization_id", flat=True))
    for org_id in sorted(org_ids):
        try:
            sync_org_payments.delay(org_id, automatic=True)
        except Exception:  # broker down: the next hourly run tries again
            log.exception("Could not queue the payment check for organization %s", org_id)
    return len(org_ids)


@shared_task
def sync_org_payments(org_id: int, automatic: bool = True, user_id: int | None = None, shipment_id: int | None = None):
    from django.contrib.auth import get_user_model

    from apps.accounting.services import payments
    from apps.core.models import Organization

    org = Organization.objects.filter(pk=org_id).first()
    if org is None:
        return {"skipped": "no_organization"}
    actor = get_user_model().objects.filter(pk=user_id).first() if user_id else None
    shipment = Shipment.objects.filter(pk=shipment_id, organization=org).first() if shipment_id else None
    return payments.sync(org, automatic=automatic, actor=actor, shipment=shipment)
