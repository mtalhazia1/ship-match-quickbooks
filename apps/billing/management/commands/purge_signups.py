from django.core.management.base import BaseCommand

from apps.billing.signup import purge


class Command(BaseCommand):
    help = "Delete unverified sign-ups whose verification link has expired (hourly in Celery beat)."

    def handle(self, *args, **opts):
        n = purge()
        self.stdout.write(self.style.SUCCESS(f"Deleted {n} unverified sign-up{'s' if n != 1 else ''}."))
