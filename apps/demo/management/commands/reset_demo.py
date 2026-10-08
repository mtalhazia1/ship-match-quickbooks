"""Wipe and rebuild the demo organizations: python manage.py reset_demo

Deletes every organization listed in DEMO_ORGS (default "demo") with all its documents, shipments,
files and settings, then runs seed_demo, imports a fresh synthetic document set, adds the vendor
learning example and, when the command exists, seed_rates. Runs nightly from Celery beat when
DEMO_MODE=1. Refuses to run otherwise unless --force is given (for local testing).
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.demo.services.reset import reset_demo


class Command(BaseCommand):
    help = "Delete and rebuild the demo organizations (DEMO_ORGS) with fresh sample documents."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Run even though DEMO_MODE is off (local testing)")
        parser.add_argument("--shipments", type=int, default=12, help="Synthetic shipments to import (default 12)")
        parser.add_argument("--seed", type=int, default=42, help="Random seed for the synthetic documents")
        parser.add_argument("--no-documents", action="store_true", help="Only recreate organizations and users")
        parser.add_argument("--use-ai", action="store_true",
                            help="Read the sample documents with the configured AI provider (costs credits)")

    def handle(self, *args, **opts):
        if not settings.DEMO_MODE and not opts["force"]:
            raise CommandError(
                "reset_demo deletes the organizations in DEMO_ORGS "
                f"({', '.join(settings.DEMO_ORGS) or 'demo'}) and everything in them. It only runs with "
                "DEMO_MODE=1, or with --force on a machine where that is what you want.")
        if opts["shipments"] < 2 or opts["shipments"] > 60:
            raise CommandError("--shipments must be between 2 and 60.")
        s = reset_demo(shipments=opts["shipments"], seed=opts["seed"], documents=not opts["no_documents"],
                       use_ai=opts["use_ai"], stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS(
            f"Demo reset: {', '.join(s['orgs'])} rebuilt with {s['documents']} documents"
            f"{' and exchange rates' if s['rates'] else ''}; removed {s['users_removed']} visitor accounts."))
