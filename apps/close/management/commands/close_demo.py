"""Month-end demo data for an organization that already has the synthetic dataset loaded.

    python manage.py close_demo --org demo

* Two shipments that shipped near the end of last month and aren't fully billed yet (one with only its
  bill of lading and commercial invoice, one whose ocean freight is billed but not its destination charges),
  so Month-end > Accruals shows estimates next to received invoices.
* A Harborlink statement of account built from the Harborlink invoices in the organization, with one invoice
  ShipMatch never received, one amount difference and one credit note ShipMatch has but the vendor hasn't
  applied. The statement (PDF, XLSX, CSV) and the credit note are written to --out; the credit note is
  imported and the PDF statement uploaded, unless --files-only.

Safe to run again: identical files are recognized and not imported twice.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.accounting.models import vendor_key
from apps.core.models import Organization
from apps.documents.models import Document
from apps.documents.services.ingest import ingest_bytes
from apps.shipments.models import ValidationIssue

from ...services import statements as statement_service
from ...services.accruals import previous_month_end
from ...services.history import DUPLICATE_CODES


class Command(BaseCommand):
    help = "Add month-end demo data: shipments not yet invoiced and a vendor statement with planted differences."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="demo")
        parser.add_argument("--vendor", default="Harborlink Logistics LLC")
        parser.add_argument("--out", default="datasets/synthetic-month-end")
        parser.add_argument("--files-only", action="store_true", help="Write the files without importing them")
        parser.add_argument("--no-shipments", action="store_true", help="Only the statement")

    def handle(self, *args, **opts):
        from synthetic import month_end

        org = Organization.objects.filter(slug=opts["org"]).first()
        if org is None:
            raise CommandError(f"No organization '{opts['org']}'. Run seed_demo first or pass --org.")
        out = Path(opts["out"])
        out.mkdir(parents=True, exist_ok=True)
        load = not opts["files_only"]

        if not opts["no_shipments"]:
            ship_day = previous_month_end().replace(day=24)
            for name, pdf, _ in month_end.arrived_not_invoiced(ship_day):
                (out / name).write_bytes(pdf)
                if load:
                    ingest_bytes(org, name, pdf, process="sync")
            self.stdout.write(f"Shipments not yet invoiced: written to {out}" + (" and imported." if load else "."))

        invoices = self._invoices(org, opts["vendor"])
        if len(invoices) < 2:
            raise CommandError(f"{org.slug} has fewer than two invoices from {opts['vendor']}. Import the synthetic "
                               "dataset first (generate_dataset, then ingest_folder).")
        vendor = next((p for p in month_end.FORWARDERS if p.name == opts["vendor"]), None) or month_end.Party(
            opts["vendor"], "", "example.com")
        s = month_end.scenario(invoices, vendor=vendor)
        stem = vendor_key(vendor.name).replace(" ", "-")
        files = {f"{stem}-statement.pdf": month_end.statement_pdf(s), f"{stem}-statement.xlsx": month_end.statement_xlsx(s),
                 f"{stem}-statement.csv": month_end.statement_csv(s), f"{stem}-credit-note.pdf": s["credit_note"][0]}
        for name, data in files.items():
            (out / name).write_bytes(data)
        self.stdout.write(f"Statement and credit note written to {out}.")
        if load:
            ingest_bytes(org, f"{stem}-credit-note.pdf", s["credit_note"][0], process="sync")
            st, created = statement_service.upload(org, f"{stem}-statement.pdf", files[f"{stem}-statement.pdf"], None)
            summary = st.summary or {}
            self.stdout.write(self.style.SUCCESS(
                f"Statement {'uploaded' if created else 'already uploaded'}: {st.vendor_name}, difference "
                f"{summary.get('difference', '?')} {summary.get('currency', '')} (planted: {s['expected']['difference']})."))

    @staticmethod
    def _invoices(org, vendor: str) -> list[dict]:
        vk = vendor_key(vendor)
        duplicates = set(ValidationIssue.objects.filter(organization=org, resolved=False, code__in=DUPLICATE_CODES)
                         .values_list("document_id", flat=True))
        out = []
        for d in Document.objects.filter(organization=org, doc_type=Document.DocType.FREIGHT_INVOICE).prefetch_related(
                "fields"):
            data = d.data()
            if d.pk in duplicates or vendor_key(data.get("vendor_name")) != vk:
                continue
            if not (data.get("invoice_number") and data.get("invoice_date") and data.get("total_amount")):
                continue
            out.append({"invoice_number": data["invoice_number"], "invoice_date": data["invoice_date"],
                        "bl_number": data.get("bl_number") or "", "total_amount": str(data["total_amount"]),
                        "container_numbers": data.get("container_numbers") or []})
        return sorted(out, key=lambda i: (i["invoice_date"], i["invoice_number"]))
