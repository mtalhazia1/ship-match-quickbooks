from django.apps import AppConfig


class CustomsConfig(AppConfig):
    """Customs entries (CBP 7501 and other declarations) and arrival notices: duty checks, landed cost charges,
    free time and last free day alerts. Everything is registered here instead of edited into shared files."""

    name = "apps.customs"
    label = "customs"
    verbose_name = "Customs and free time"

    def ready(self):
        from django.apps import apps as django_apps
        from django.db.models.signals import post_save

        from apps.core import context_processors, views
        from apps.core.models import AuditEvent
        from apps.documents.services.extract_rules import register_rules_reader
        from apps.shipments import labels
        from apps.shipments.services.matching import register_match_fallback
        from apps.shipments.services.validation import register_document_rule, register_shipment_rule

        from . import checks as _system_checks  # noqa: F401  (registers the fee settings check)
        from . import labels as customs_labels
        from .services import checks, matching, readers

        register_rules_reader("customs_entry", readers.customs_entry_rules)
        register_rules_reader("arrival_notice", readers.arrival_notice_rules)
        for rule in (checks.check_duty_math, checks.check_user_fees, checks.check_hts_codes, checks.check_hts_doubts,
                     checks.check_duplicate_entry, checks.check_arrival_notice):
            register_document_rule(rule)
        register_shipment_rule(checks.check_entry_against_invoices)
        register_match_fallback(matching.entry_number_candidate)
        _register_labels()

        for action, text in customs_labels.ACTIONS.items():
            labels.ACTIONS.setdefault(action, text)
        for group in customs_labels.AUDIT_GROUPS:
            if group not in views.AUDIT_ACTION_GROUPS:
                views.AUDIT_ACTION_GROUPS.append(group)
        context_processors.SECTIONS.setdefault("customs:settings", "settings")
        context_processors.SECTIONS.setdefault("customs:free_time", "dashboard")

        if django_apps.is_installed("apps.notifications"):
            from apps.notifications import events

            from .services import alerts

            events.register_event(
                alerts.EVENT_SOON, "Last free day coming up",
                "A container must be picked up (or returned empty) within the days set in Settings, Customs.",
                audit_actions=[alerts.SOON_ACTION], builder=alerts.soon_message, default_for=("slack", "teams", "email"))
            events.register_event(
                alerts.EVENT_LATE, "Free time passed",
                "A container wasn't picked up or returned by its last free day: demurrage or detention is accruing.",
                audit_actions=[alerts.LATE_ACTION], builder=alerts.late_message, default_for=("slack", "teams", "email"))
        if django_apps.is_installed("apps.rates"):
            from apps.rates import savings

            savings.WHOLE_INVOICE.add("duplicate_customs_entry")  # the whole entry counts once on Savings

        if django_apps.is_installed("apps.landed"):
            from apps.landed.services.charges import register_charge_source

            register_charge_source(_landed_charges)

        post_save.connect(_on_audit_event, sender=AuditEvent, dispatch_uid="customs.audit_event", weak=False)


def _landed_charges(shipment):
    """Duty and customs fees as landed cost charges (spread by value unless the organization chose otherwise)."""
    from .services.charges import duty_charges

    out = []
    for c in duty_charges(shipment):
        entry = f" (entry {c['entry_number']})" if c.get("entry_number") else ""
        out.append({"code": c["code"], "amount": c["amount"], "currency": c["currency"],
                    "basis": c.get("basis_hint") if c.get("basis_hint") != "shipment" else None,
                    "description": f"{c['label']}{entry}", "source": "Customs entry", "category": "duty",
                    "document_id": c.get("document_id")})
    return out


def _on_audit_event(sender, instance, created, **kwargs):
    """Free time follows every check of a shipment; a newly read customs entry gets its optional AI tariff check."""
    import logging

    from django.db import transaction

    if not created:
        return
    log = logging.getLogger(__name__)
    if instance.action == "shipment.validated" and instance.object_type == "Shipment":
        from apps.shipments.models import Shipment

        from .services.freetime import sync_shipment

        try:
            with transaction.atomic():
                shipment = Shipment.objects.filter(pk=instance.object_id).select_related("organization").first()
                if shipment is not None:
                    sync_shipment(shipment)
        except Exception:  # free time must never stop a shipment from being checked
            log.exception("Could not update free time for shipment %s", instance.object_id)
    elif instance.action == "document.extracted" and instance.object_type == "Document":
        from .services import hts_ai

        if hts_ai.enabled():
            doc_id = int(instance.object_id)
            transaction.on_commit(lambda: hts_ai.on_extracted(doc_id))


def _register_labels():
    """Labels printed next to customs values, so evidence highlighting picks the right copy of a number or date."""
    from apps.documents.services.locate import register_labels

    for name, labels, negative, header in [
        ("entry_number", [r"entry\s*(?:no|number|#)", r"declaration\s*(?:no|number)", r"\bmrn\b"], [], True),
        ("entry_date", [r"entry\s+date", r"declaration\s+date", r"date\s+of\s+acceptance"],
         [r"summary", r"import", r"export"], True),
        ("import_date", [r"import\s+date", r"arrival\s+date"], [r"export", r"entry"], True),
        ("port_of_entry", [r"port\s+code", r"port\s+of\s+entry", r"customs\s+office"], [], True),
        ("importer_name", [r"importer"], [], True),
        ("broker_name", [r"broker", r"declarant", r"filer"], [], False),
        ("country_of_origin", [r"country\s+of\s+origin", r"origin"], [r"export"], True),
        ("exchange_rate", [r"exchange\s+rate", r"rate\s+of\s+exchange"], [], True),
        ("invoice_currency", [r"invoice\s+currency"], [], True),
        ("total_entered_value", [r"total\s+entered\s+value", r"customs\s+value"], [], False),
        ("total_duty", [r"\bduty\b"], [r"total\s+duty\s+and", r"rate"], False),
        ("merchandise_processing_fee", [r"merchandise\s+processing", r"\bmpf\b", r"\b499\b"], [], False),
        ("harbor_maintenance_fee", [r"harbou?r\s+maintenance", r"\bhmf\b", r"\b501\b"], [], False),
        ("other_fees", [r"\btax\b", r"other\s+fees?"], [], False),
        ("total_duty_and_fees", [r"\btotal\b"], [r"entered", r"value", r"\bduty\s*:"], False),
        ("terminal", [r"terminal", r"\bpier\b"], [], True),
        ("estimated_arrival_date", [r"\beta\b", r"estimated"], [], True),
        ("actual_arrival_date", [r"\bata\b", r"actual\s+arrival"], [], True),
        ("discharge_date", [r"discharg"], [], True),
        ("notice_date", [r"notice\s+date", r"\bdate\b"], [r"discharg", r"\beta\b", r"free", r"return"], True),
        ("demurrage_free_days", [r"demurrage\s+free", r"terminal\s+free", r"storage\s+free"], [], False),
        ("detention_free_days", [r"detention\s+free", r"per\s+diem", r"equipment\s+free"], [], False),
        ("demurrage_last_free_day", [r"last\s+free\s+day", r"\blfd\b", r"pick\s*up\s+by"], [r"detention", r"return"],
         True),
        ("detention_last_free_day", [r"empty\s+return", r"return\s+(?:by|empty)", r"detention"], [], True),
    ]:
        register_labels(name, labels, negative, header=header)
