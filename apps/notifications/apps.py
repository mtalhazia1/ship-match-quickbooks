from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "apps.notifications"
    label = "notifications"
    verbose_name = "Alerts"

    def ready(self):
        from django.db.models.signals import post_save, pre_save

        from apps.core import context_processors, views
        from apps.core.models import AuditEvent
        from apps.shipments import labels
        from apps.shipments.models import Shipment

        from . import dispatch
        from . import labels as alert_labels

        # Registered here instead of edited into shared files, to keep feature branches merge-friendly.
        for action, text in alert_labels.ACTIONS.items():
            labels.ACTIONS.setdefault(action, text)
        for group in alert_labels.AUDIT_GROUPS:
            if group not in views.AUDIT_ACTION_GROUPS:
                views.AUDIT_ACTION_GROUPS.append(group)
        for name in ("settings", "channel_new", "channel_edit"):
            context_processors.SECTIONS.setdefault(f"notifications:{name}", "settings")

        post_save.connect(dispatch.on_audit_event, sender=AuditEvent, dispatch_uid="notifications.audit_event")
        pre_save.connect(dispatch.before_shipment_save, sender=Shipment, dispatch_uid="notifications.shipment_pre")
        post_save.connect(dispatch.on_shipment_saved, sender=Shipment, dispatch_uid="notifications.shipment_post")
