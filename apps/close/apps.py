from django.apps import AppConfig


class CloseConfig(AppConfig):
    name = "apps.close"
    label = "close"
    verbose_name = "Month-end close"

    def ready(self):
        from apps.shipments import labels
        from apps.shipments.services.validation import register_document_rule

        from . import labels as close_labels
        from .rules import check_statement_sent_as_invoice

        labels.ACTIONS.update(close_labels.ACTIONS)
        register_document_rule(check_statement_sent_as_invoice)
