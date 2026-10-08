"""Import a folder of documents (PDFs, photos, spreadsheets, ZIP archives) as if they had arrived by email.

If the folder (or its parent) has an emails.json manifest from the synthetic generator,
files are imported in that arrival order with sender, subject and message ID.
"""
import json
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Organization
from apps.documents.models import Document, IngestedEmail
from apps.documents.services.ingest import RejectedFile, ingest_bytes
from apps.intake.services.formats import SUPPORTED_TEXT, is_supported_name


class Command(BaseCommand):
    help = ("Ingest every supported file in a folder: " + SUPPORTED_TEXT + " (uses emails.json for order and "
            "email metadata when present).")

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--org", default="demo")
        parser.add_argument("--async", dest="use_async", action="store_true", help="Queue to Celery instead of processing inline")

    def handle(self, *args, **opts):
        try:
            org = Organization.objects.get(slug=opts["org"])
        except Organization.DoesNotExist:
            raise CommandError(f"Organization '{opts['org']}' not found. Run seed_demo first.")
        folder = Path(opts["path"])
        pdf_dir = folder / "pdf" if (folder / "pdf").is_dir() else folder
        manifest = next((p for p in [folder / "emails.json", pdf_dir.parent / "emails.json"] if p.exists()), None)
        if manifest:
            entries = json.loads(manifest.read_text())
        else:
            # Any supported file (.pdf, .jpg, .xlsx, .zip, ...), including subfolders; hidden files are skipped.
            entries = [{"file": str(p.relative_to(pdf_dir))} for p in sorted(pdf_dir.rglob("*"))
                       if p.is_file() and is_supported_name(p.name)
                       and not any(part.startswith(".") for part in p.relative_to(pdf_dir).parts)]

        process = "async" if opts["use_async"] else "sync"
        created = skipped = rejected = 0
        for e in entries:
            path = pdf_dir / e["file"]
            email = None
            if e.get("message_id"):
                email, _ = IngestedEmail.objects.get_or_create(
                    organization=org, message_id=e["message_id"],
                    defaults={"subject": e.get("subject", ""), "sender": e.get("from", ""),
                              "received_at": datetime.fromisoformat(e["received_at"]) if e.get("received_at") else None,
                              "attachment_count": 1},
                )
            try:
                doc, new = ingest_bytes(org, path.name, path.read_bytes(),
                                        source=Document.Source.EMAIL if email else Document.Source.FOLDER,
                                        email=email, process=process)
            except RejectedFile as err:
                rejected += 1
                self.stderr.write(str(err))
                continue
            created += int(new)
            skipped += int(not new)
            if new and process == "sync":
                self.stdout.write(f"{path.name:45s} {doc.doc_type:20s} {doc.status}")
        self.stdout.write(self.style.SUCCESS(f"Imported {created}, already present {skipped}, rejected {rejected}."))
