from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "apps.billing"
    label = "billing"
    verbose_name = "Sign-up and billing"

    def ready(self):
        from django.db.models.signals import post_save

        from apps.core import context_processors, views
        from apps.documents.models import Document
        from apps.shipments import labels

        from . import labels as billing_labels
        from .signals import on_document_saved

        # Registered here instead of edited into shared files, to keep feature branches merge-friendly.
        for action, text in billing_labels.ACTIONS.items():
            labels.ACTIONS.setdefault(action, text)
        for group in billing_labels.AUDIT_GROUPS:
            if group not in views.AUDIT_ACTION_GROUPS:
                views.AUDIT_ACTION_GROUPS.append(group)
        context_processors.SECTIONS.setdefault("billing:settings", "settings")

        post_save.connect(on_document_saved, sender=Document, dispatch_uid="billing.document_saved")
