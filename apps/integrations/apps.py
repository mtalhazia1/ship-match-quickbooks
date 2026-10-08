from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    name = "apps.integrations"
    label = "integrations"
    verbose_name = "Exports, webhooks and API"

    def ready(self):
        from django.apps import apps as django_apps
        from django.db.models.signals import post_save, pre_save

        from apps.core import context_processors, views
        from apps.core.models import AuditEvent
        from apps.shipments import labels
        from apps.shipments.models import Shipment

        from . import dispatch, events
        from . import labels as integration_labels

        # Registered here instead of edited into shared files, to keep feature branches merge-friendly.
        for action, text in integration_labels.ACTIONS.items():
            labels.ACTIONS.setdefault(action, text)
        for group in integration_labels.AUDIT_GROUPS:
            if group not in views.AUDIT_ACTION_GROUPS:
                views.AUDIT_ACTION_GROUPS.append(group)
        for name in ("webhooks", "webhook_edit"):
            context_processors.SECTIONS.setdefault(f"integrations:{name}", "settings")
        if django_apps.is_installed("apps.disputes"):
            events.register_dispute_events()

        post_save.connect(dispatch.on_audit_event, sender=AuditEvent, dispatch_uid="integrations.audit_event")
        pre_save.connect(dispatch.before_shipment_save, sender=Shipment, dispatch_uid="integrations.shipment_pre")
        post_save.connect(dispatch.on_shipment_saved, sender=Shipment, dispatch_uid="integrations.shipment_post")
