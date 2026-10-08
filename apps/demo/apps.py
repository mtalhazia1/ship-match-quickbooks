from django.apps import AppConfig


class DemoConfig(AppConfig):
    name = "apps.demo"
    label = "demo"
    verbose_name = "Public demo and try page"

    def ready(self):
        from apps.shipments import labels

        labels.ACTIONS.setdefault("demo.reset", "reset the demo organizations")
        labels.ACTIONS.setdefault("try.purged", "deleted {count} expired try page uploads")
