from django.core.management.base import BaseCommand

from apps.demo.services.tryit import purge


class Command(BaseCommand):
    help = "Delete try page uploads (files, results and sandbox organizations) older than TRY_RETENTION_HOURS."

    def handle(self, *args, **opts):
        n = purge()
        self.stdout.write(self.style.SUCCESS(f"Deleted {n} expired try page upload{'s' if n != 1 else ''}."))
