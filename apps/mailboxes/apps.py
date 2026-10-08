from django.apps import AppConfig


class MailboxesConfig(AppConfig):
    name = "apps.mailboxes"
    label = "mailboxes"
    verbose_name = "Email intake"

    def ready(self):
        # Readable audit log lines for this app's events, without editing the shared label table.
        from apps.shipments import labels

        from .labels import AUDIT_ACTIONS

        for action, text in AUDIT_ACTIONS.items():
            labels.ACTIONS.setdefault(action, text)
