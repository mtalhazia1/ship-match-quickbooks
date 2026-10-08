from django.apps import AppConfig


class IntakeConfig(AppConfig):
    """Messy inputs: photos, spreadsheets, ZIP archives, multi-invoice PDFs and credit notes."""

    name = "apps.intake"
    label = "intake"
    verbose_name = "Document intake"

    def ready(self):
        from apps.shipments.services.validation import register_document_rule

        from .rules import check_credit_note, check_duplicate_credit_note

        register_document_rule(check_credit_note)
        register_document_rule(check_duplicate_credit_note)
