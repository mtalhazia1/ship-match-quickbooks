from django.apps import AppConfig


class LearningConfig(AppConfig):
    name = "apps.learning"
    label = "learning"
    verbose_name = "Vendor learning"

    def ready(self):
        from django.db.models.signals import post_save

        from apps.core import context_processors, views
        from apps.core.models import AuditEvent
        from apps.shipments import labels

        from .signals import on_audit_event

        # Learn from every reviewer correction, wherever it is made (review screen, API, ...).
        post_save.connect(on_audit_event, sender=AuditEvent, dispatch_uid="learning.on_audit_event")
        # Audit log wording, filter group and sidebar highlight, registered here so shared files stay untouched.
        labels.ACTIONS.setdefault("learning.updated", "learned how {vendor} prints {field}")
        labels.ACTIONS.setdefault("learning.forgotten", "made ShipMatch forget what it learned about {vendor}")
        if not any(prefix == "learning." for prefix, _ in views.AUDIT_ACTION_GROUPS):
            views.AUDIT_ACTION_GROUPS.append(("learning.", "Vendor learning"))
        context_processors.SECTIONS.setdefault("learning:settings", "settings")
