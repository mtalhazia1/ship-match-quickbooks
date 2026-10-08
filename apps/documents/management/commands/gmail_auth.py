from django.core.management.base import BaseCommand

from apps.core.models import Organization
from apps.documents.services import gmail


class Command(BaseCommand):
    help = "Authorize Gmail read-only access for an organization (opens a browser once)."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="demo")

    def handle(self, *args, **opts):
        org = Organization.objects.get(slug=opts["org"])
        path = gmail.authorize_interactive(org)
        self.stdout.write(self.style.SUCCESS(f"Token saved to {path}"))
