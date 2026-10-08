"""Run a pilot on your own documents in a separate organization, and print what happened.

    python manage.py pilot                      # reads PDFs from the real_docs folder
    python manage.py pilot --folder D:\\scans --org pilot
"""
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Membership, Organization
from apps.documents.models import Document
from apps.documents.services import llm
from apps.documents.services.ingest import RejectedFile, ingest_bytes
from apps.documents.services.ocr import ocr_provider
from apps.shipments.models import Shipment


class Command(BaseCommand):
    help = "Import a folder of real PDFs into a pilot organization and summarize the results."

    def add_arguments(self, parser):
        parser.add_argument("--folder", default="real_docs")
        parser.add_argument("--org", default="pilot")
        parser.add_argument("--name", default="Pilot (real documents)")

    def handle(self, *args, **o):
        folder = Path(o["folder"])
        from apps.intake.services.formats import SUPPORTED_TEXT, is_supported_name

        files = sorted(p for p in folder.rglob("*") if p.is_file() and is_supported_name(p.name)) if folder.is_dir() else []
        if not files:
            raise CommandError(f"No {SUPPORTED_TEXT} files found in {folder.resolve()}")
        w = self.stdout.write
        reader = f"Claude ({llm.model_name()})" if settings.EXTRACTION_PROVIDER == "anthropic" else settings.EXTRACTION_PROVIDER
        if settings.EXTRACTION_PROVIDER == "anthropic" and not settings.ANTHROPIC_API_KEY:
            raise CommandError("EXTRACTION_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty in .env")
        w(f"Reader: {reader}. Scanned pages: {ocr_provider()}. PDF sent to the AI: {settings.LLM_INPUT}.")

        org, created = Organization.objects.get_or_create(slug=o["org"], defaults={"name": o["name"]})
        for admin in get_user_model().objects.filter(is_superuser=True, is_active=True):
            Membership.objects.get_or_create(user=admin, organization=org, defaults={"role": Membership.Role.ADMIN})
        w(f"Organization: {org.name} ({'new' if created else 'existing'}). {len(files)} files.\n")

        new = dupes = rejected = 0
        for i, path in enumerate(files, 1):
            try:
                doc, is_new = ingest_bytes(org, path.name, path.read_bytes(), source=Document.Source.FOLDER, process="sync")
            except RejectedFile as e:
                rejected += 1
                w(self.style.ERROR(f"[{i}/{len(files)}] {path.name}: {e}"))
                continue
            if not is_new:
                dupes += 1
                w(f"[{i}/{len(files)}] {path.name}: already imported")
                continue
            new += 1
            doc.refresh_from_db()
            cost = (doc.llm_usage or {}).get("cost_usd", 0)
            where = doc.match.shipment.reference if hasattr(doc, "match") else doc.get_status_display()
            w(f"[{i}/{len(files)}] {path.name}: {doc.get_doc_type_display()}, {doc.fields.count()} fields, "
              f"{where}, text from {doc.text_source}, ${cost:.4f}")

        docs = Document.objects.filter(organization=org)
        ships = Shipment.objects.filter(organization=org)
        usage = [d.llm_usage or {} for d in docs]
        cost = sum(u.get("cost_usd", 0) for u in usage)
        w("")
        w(f"Imported {new}, already there {dupes}, rejected {rejected}.")
        w(f"Document types: {dict(Counter(d.get_doc_type_display() for d in docs))}")
        w(f"Read by: {dict(Counter(d.extraction_provider or 'not read' for d in docs))}")
        w(f"Not in a shipment: {docs.exclude(status=Document.Status.MATCHED).count()}")
        w(f"Shipments: {ships.count()} {dict(Counter(ships.values_list('status', flat=True)))}")
        w(f"AI cost: ${cost:.4f} total, ${cost / max(1, len(usage)):.4f} per document (estimate)")
        w(self.style.SUCCESS(f"\nReview them at http://localhost:8000/review/?org={org.slug}  "
                             "Then approve shipments; Reports > Reading accuracy shows how often the reading was right."))
