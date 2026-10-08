"""Usage notices after a document arrives. Best effort: never breaks intake."""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction

log = logging.getLogger(__name__)


def on_document_saved(sender, instance, created: bool, **kwargs) -> None:
    if not created or not settings.BILLING_ENABLED:
        return
    org_id = instance.organization_id

    def check():
        from apps.core.models import Organization

        from .usage import after_document_received

        try:
            org = Organization.objects.filter(pk=org_id).first()
            if org is not None:
                after_document_received(org)
        except Exception:
            log.exception("Usage check after a new document failed (organization %s)", org_id)

    try:
        transaction.on_commit(check)
    except Exception:
        log.exception("Could not schedule the usage check for organization %s", org_id)
