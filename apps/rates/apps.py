from django.apps import AppConfig


class RatesConfig(AppConfig):
    name = "apps.rates"
    label = "rates"
    verbose_name = "Rates and savings"

    def ready(self):
        from apps.shipments.services.validation import register_shipment_rule

        from . import ledger
        from .checks import check_rates

        register_shipment_rule(check_rates)
        ledger.connect()
