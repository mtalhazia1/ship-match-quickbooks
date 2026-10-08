from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Organization
from apps.documents.models import Document
from apps.documents.services.locate import FOUND, NOT_FOUND, PARTIAL, SCANNED, safe_locate


class Command(BaseCommand):
    help = ("Find where each extracted value is printed on its PDF, for documents read before evidence "
            "highlighting existed. Safe to repeat; human corrections are located too.")

    def add_arguments(self, parser):
        parser.add_argument("ids", nargs="*", type=int, help="Only these document ids")
        parser.add_argument("--org", help="Only this organization (slug)")
        parser.add_argument("--all", action="store_true",
                            help="Locate every document again, not only those with values that have no location yet")

    def handle(self, *args, **opts):
        qs = Document.objects.exclude(fields=None).select_related("organization").order_by("id").distinct()
        if opts["org"]:
            org = Organization.objects.filter(slug=opts["org"]).first()
            if org is None:
                raise CommandError(f"No organization with slug '{opts['org']}'.")
            qs = qs.filter(organization=org)
        if opts["ids"]:
            qs = qs.filter(pk__in=opts["ids"])
        elif not opts["all"]:
            qs = qs.filter(fields__location__isnull=True, fields__value__isnull=False).distinct()

        totals = {FOUND: 0, PARTIAL: 0, NOT_FOUND: 0, SCANNED: 0}
        docs = failed = 0
        for doc in qs.iterator():
            counts = safe_locate(doc)
            docs += 1
            if counts is None:
                failed += 1
                self.stderr.write(f"{doc.pk} {doc.original_filename}: could not be read (see the log)")
                continue
            for k, v in counts.items():
                totals[k] += v
            self.stdout.write(f"{doc.pk} {doc.original_filename}: {counts[FOUND] + counts[PARTIAL]} located, "
                              f"{counts[NOT_FOUND]} not on the page, {counts[SCANNED]} on scanned pages")
        self.stdout.write(self.style.SUCCESS(
            f"{docs} documents: {totals[FOUND] + totals[PARTIAL]} values located, {totals[NOT_FOUND]} not on the page, "
            f"{totals[SCANNED]} on scanned pages without OCR positions, {failed} failed."))
