from django.apps import AppConfig


class LandedConfig(AppConfig):
    name = "apps.landed"
    label = "landed"
    verbose_name = "Landed cost and shared invoices"

    def ready(self):
        from django.db.models.signals import post_save

        from apps.core import context_processors, views
        from apps.core.models import AuditEvent
        from apps.shipments import labels
        from apps.shipments.services.approval import register_approval_blocker, register_posting_blocker
        from apps.shipments.services.validation import register_shipment_rule

        from . import hooks
        from . import labels as landed_labels
        from .services import allocation, rules

        # Registered here instead of edited into shared files, to keep feature branches merge-friendly.
        labels.ACTIONS.update(landed_labels.ACTIONS)
        for group in landed_labels.AUDIT_GROUPS:
            if group not in views.AUDIT_ACTION_GROUPS:
                views.AUDIT_ACTION_GROUPS.append(group)
        context_processors.SECTIONS.setdefault("landed:report", "landed")
        context_processors.SECTIONS.setdefault("landed:settings", "settings")
        register_shipment_rule(rules.check_shared)
        register_approval_blocker(rules.approval_blockers)
        register_posting_blocker(allocation.posting_blockers)

        # A shared invoice posts as one bill with each shipment's share on its own lines.
        from apps.accounting.services.posting import register_line_builder

        def shared_invoice_lines(doc):
            lines = allocation.bill_lines(doc)
            if lines is None:
                return None
            return [{"description": line["description"], "amount": line["amount"],
                     "memo": f"Shipment {line['shipment_reference']}"} for line in lines]

        register_line_builder(shared_invoice_lines)
        post_save.connect(hooks.on_audit_event, sender=AuditEvent, dispatch_uid="landed.on_audit_event", weak=False)

        # Split checks are about our own paperwork, not what the vendor billed: never offered as a dispute.
        from django.apps import apps as django_apps

        if django_apps.is_installed("apps.disputes"):
            from apps.disputes import evidence

            evidence.NOT_DISPUTABLE.update({"shared_invoice_split", "shared_split_mismatch",
                                            "shared_invoice_locked_ref"})
