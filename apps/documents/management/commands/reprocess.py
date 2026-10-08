from django.core.management.base import BaseCommand

from apps.documents.models import Document
from apps.documents.services.pipeline import process_document


class Command(BaseCommand):
    help = "Re-run OCR, extraction, matching and validation (human corrections are kept)."

    def add_arguments(self, parser):
        parser.add_argument("ids", nargs="*", type=int)
        parser.add_argument("--status", help="Reprocess every document with this status, e.g. error or needs_ocr")

    def handle(self, *args, **opts):
        qs = Document.objects.all()
        if opts["ids"]:
            qs = qs.filter(pk__in=opts["ids"])
        elif opts["status"]:
            qs = qs.filter(status=opts["status"])
        else:
            self.stderr.write("Give document ids or --status")
            return
        for doc in qs:
            d = process_document(doc.pk)
            self.stdout.write(f"{d.pk} {d.original_filename} -> {d.status}")
