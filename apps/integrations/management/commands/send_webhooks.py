from django.core.management.base import BaseCommand

from apps.integrations.tasks import retry_due_deliveries


class Command(BaseCommand):
    help = "Send webhook deliveries whose retry is due (Celery beat runs this every minute)."

    def handle(self, *args, **opts):
        n = retry_due_deliveries()
        self.stdout.write(self.style.SUCCESS(f"Queued {n} webhook deliver{'ies' if n != 1 else 'y'}."))
