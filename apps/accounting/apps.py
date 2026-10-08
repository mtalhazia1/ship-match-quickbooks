from django.apps import AppConfig


class AccountingConfig(AppConfig):
    name = "apps.accounting"
    label = "accounting"

    def ready(self):
        from django.apps import apps as django_apps

        from apps.core import context_processors, views
        from apps.shipments import labels

        from . import labels as accounting_labels

        # Registered here instead of edited into shared files, to keep feature branches merge-friendly.
        for action, text in accounting_labels.ACTIONS.items():
            labels.ACTIONS.setdefault(action, text)
        for group in accounting_labels.AUDIT_GROUPS:
            if group not in views.AUDIT_ACTION_GROUPS:
                views.AUDIT_ACTION_GROUPS.append(group)
        for name in ("settings", "xero_tenant"):
            context_processors.SECTIONS.setdefault(f"accounting:{name}", "settings")

        if django_apps.is_installed("apps.notifications"):
            from . import alerts

            alerts.register()
        if django_apps.is_installed("apps.demo"):
            from apps.demo import middleware

            # The public demo keeps its accounting connection, like QuickBooks.
            middleware.RULES.setdefault("accounting:xero_disconnect",
                                        ("demo", None, "Xero can't be disconnected.", "core:settings"))
