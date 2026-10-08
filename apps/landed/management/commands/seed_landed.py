"""Demo data for landed cost and shared invoices (idempotent: identical files are not imported twice).

Imports, into an organization, three shipments from one supplier (commercial invoices with SKUs, HS codes,
weights and volumes, and bills of lading) and one forwarder invoice that covers all three. The invoice is
detected as shared and split by the lines that name each B/L; a reviewer confirms the split. Earlier
rounds (--rounds 3) give the per-product report a history. Nothing is approved: people do that.

    python manage.py seed_landed --org demo [--rounds 3] [--unattributed]
"""
from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Organization
from apps.documents.models import Document
from apps.documents.services.ingest import ingest_bytes
from synthetic.landed import scenario


class Command(BaseCommand):
    help = "Import demo documents for landed cost and an invoice shared by several shipments."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="demo")
        parser.add_argument("--rounds", type=int, default=2, help="Sets of three shipments (1 to 6)")
        parser.add_argument("--unattributed", action="store_true",
                            help="The shared invoice's lines don't name a B/L (split by containers)")

    def handle(self, *args, **opts):
        org = Organization.objects.filter(slug=opts["org"]).first()
        if org is None:
            raise CommandError(f"No organization '{opts['org']}'. Run seed_demo first.")
        rounds = max(1, min(6, opts["rounds"]))
        added = 0
        for n in range(rounds):
            sc = scenario(seed=7 + n, attributed=not opts["unattributed"])
            for name, data in sc["files"]:
                _, created = ingest_bytes(org, name, data, source=Document.Source.UPLOAD, process="sync")
                added += int(created)
        self.stdout.write(self.style.SUCCESS(
            f"Imported {added} new document{'s' if added != 1 else ''} into '{org.slug}'. Open a shipment with "
            "a shared invoice from the review queue, or Landed cost in the sidebar."))
