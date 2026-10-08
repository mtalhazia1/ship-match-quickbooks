"""Print reading accuracy measured from reviewer corrections (same numbers as Reports > Reading accuracy)."""
from django.core.management.base import BaseCommand, CommandError

from apps.core import accuracy
from apps.core.models import Organization


class Command(BaseCommand):
    help = "Field-level reading accuracy from reviewer corrections on approved/posted shipments."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="demo")
        parser.add_argument("--days", type=int, default=365)
        parser.add_argument("--all", action="store_true", help="Include documents in shipments not yet approved")

    def handle(self, *args, **o):
        org = Organization.objects.filter(slug=o["org"]).first()
        if not org:
            raise CommandError(f"No organization '{o['org']}'")
        r = accuracy.build(org, o["days"], reviewed_only=not o["all"])
        w = self.stdout.write
        if not r.documents:
            w("No reviewed documents yet. Approve some shipments, then run this again.")
            return
        w(f"{org.name}: {r.documents} documents, last {r.days} days")
        w(f"Fields read correctly: {r.field_rate}% ({r.fields_correct}/{r.fields_total})")
        w(f"Documents with no corrections: {r.doc_rate}% ({r.documents_all_correct}/{r.documents})")
        w(f"Type changes: {r.type_fixes}, documents moved: {r.moves}")
        if r.ai_documents:
            w(f"AI cost: ${r.cost_usd} total, ${r.cost_per_document} per document (estimate)")
        for name, v in r.by_provider.items():
            w(f"  reader {name}: {v['rate']}% of {v['total']} values")
        w("By field (weakest first):")
        for s in r.by_field:
            w(f"  {s.doc_type:20s} {s.name:20s} {s.rate:5}%  ({s.total} values, {s.wrong} wrong, {s.missed} missed)")
