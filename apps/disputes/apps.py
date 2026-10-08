from django.apps import AppConfig


class DisputesConfig(AppConfig):
    name = "apps.disputes"
    label = "disputes"
    verbose_name = "Vendor disputes"

    def ready(self):
        from django.db.models.signals import post_save

        from apps.core import context_processors, views
        from apps.shipments import labels
        from apps.shipments.models import ValidationIssue
        from apps.shipments.services.approval import register_approval_blocker

        from . import labels as dispute_labels
        from .services import workflow

        # Registered here instead of edited into shared files, to keep feature branches merge-friendly.
        labels.ACTIONS.update(dispute_labels.ACTIONS)
        for group in dispute_labels.AUDIT_GROUPS:
            if group not in views.AUDIT_ACTION_GROUPS:
                views.AUDIT_ACTION_GROUPS.append(group)
        context_processors.SECTIONS.setdefault("disputes:settings", "settings")
        register_approval_blocker(workflow.approval_blockers)

        def _relink(sender, instance, created, **kwargs):
            if created:
                workflow.relink_issue(instance)

        post_save.connect(_relink, sender=ValidationIssue, dispatch_uid="disputes.relink_issue", weak=False)

        # A credit note naming a disputed invoice records the credit on that dispute.
        from apps.core.models import AuditEvent

        from .services import auto_credit

        post_save.connect(auto_credit.on_audit_event, sender=AuditEvent, dispatch_uid="disputes.auto_credit",
                          weak=False)

        # Money recovered through disputes shows on the Savings page when the rates app is installed.
        from django.apps import apps as django_apps

        if django_apps.is_installed("apps.rates"):
            from apps.rates.savings import register_recovery_source

            from .savings import recovery_items

            register_recovery_source(recovery_items)
