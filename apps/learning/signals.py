"""Learn from reviewer corrections by listening to the audit trail: every correction writes a
`field.corrected` audit event (apps.documents.services.corrections.correct_field)."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def on_audit_event(sender, instance, created, **kwargs):
    if not created or instance.action != "field.corrected" or instance.object_type != "Document":
        return
    from apps.documents.models import Document

    from .services.learn import learn_from_correction

    data = instance.data or {}
    doc = Document.objects.filter(pk=instance.object_id).select_related("organization").first()
    if doc is None or not data.get("field"):
        return
    learn_from_correction(doc, data["field"], data.get("old"), data.get("new"), instance.actor)
