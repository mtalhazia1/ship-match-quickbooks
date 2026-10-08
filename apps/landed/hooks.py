"""Keep shared-invoice splits and frozen landed costs in step with what happens to shipments.

Connected to AuditEvent post_save in LandedConfig.ready(), so no shared code has to call this app:
  * document.matched / document.moved / field.corrected: refresh the split of that invoice, and of
    invoices elsewhere that name the shipment the document is now in;
  * shipment.merged: re-check splits that pointed at the merged shipment;
  * shipment.approved: freeze the shipment's landed cost; shipment.reopened: unfreeze it, and give it
    a share of invoices that named it while it was approved.
The work runs in a savepoint and never raises: a problem here must not stop matching or approval.
"""
from __future__ import annotations

import logging

from django.db import transaction

log = logging.getLogger(__name__)
DOCUMENT_ACTIONS = {"document.matched", "document.moved", "field.corrected"}
SHIPMENT_ACTIONS = {"shipment.merged", "shipment.approved", "shipment.reopened"}


def on_audit_event(sender, instance, created, **kwargs):
    if not created or instance.action not in DOCUMENT_ACTIONS | SHIPMENT_ACTIONS or instance.organization_id is None:
        return
    try:
        with transaction.atomic():
            _handle(instance)
    except Exception:
        log.exception("Landed cost / shared invoice update failed after %s on %s %s", instance.action,
                      instance.object_type, instance.object_id)


def _handle(event) -> None:
    from apps.documents.models import Document
    from apps.shipments.models import Shipment

    from .services import allocation, landed

    if event.action in DOCUMENT_ACTIONS and event.object_type == "Document":
        doc = Document.objects.filter(pk=event.object_id, organization_id=event.organization_id).select_related(
            "match").first()
        if doc is None:
            return
        # The code that wrote this event checks the document's own shipment right after; skip it here.
        skip = {doc.match.shipment_id} if hasattr(doc, "match") else set()
        allocation.sync_related(doc, skip=skip, repair_splits=event.action == "document.moved")
        return
    if event.object_type != "Shipment":
        return
    shipment = Shipment.objects.filter(pk=event.object_id, organization_id=event.organization_id).select_related(
        "organization").first()
    if shipment is None:
        return
    if event.action == "shipment.approved":
        landed.freeze(shipment)
    elif event.action == "shipment.reopened":
        landed.unfreeze(shipment)
        allocation.reopened(shipment)
    elif event.action == "shipment.merged":
        changed = set()
        for doc in shipment.documents.filter(doc_type=Document.DocType.FREIGHT_INVOICE):
            changed |= allocation.sync(doc)
        changed |= allocation.repair(shipment.organization)
        allocation.revalidate(changed, skip={shipment.pk})
