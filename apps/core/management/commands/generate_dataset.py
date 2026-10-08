from django.core.management.base import BaseCommand

from synthetic.generator import generate


class Command(BaseCommand):
    help = "Generate fictional shipment PDFs with ground truth and planted errors."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="datasets/synthetic")
        parser.add_argument("--shipments", type=int, default=20)
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--scanned", type=int, default=0, help="How many clean documents to turn into image-only scans")
        parser.add_argument("--accessorials", action="store_true",
                            help="Add extra charges (demurrage, detention, exam fees ...) to some freight invoices "
                                 "for rate-check demos (see seed_rates). Off by default.")
        parser.add_argument("--customs", action="store_true",
                            help="Also write CBP 7501-style customs entries (some with duty errors) and arrival notices "
                                 "dated around today (synthetic/customs.py). Off by default.")

    def handle(self, *args, **opts):
        if opts["shipments"] < 8:
            self.stderr.write("Use at least 8 shipments so every planted error gets its own shipment.")
            return
        result = generate(opts["out"], opts["shipments"], opts["seed"], opts["scanned"],
                          accessorials=opts["accessorials"])
        if opts["customs"]:
            from synthetic.customs import add_to_dataset

            added = add_to_dataset(opts["out"], opts["seed"])
            result["documents"] += added["customs_entries"] + added["arrival_notices"]
        self.stdout.write(self.style.SUCCESS(
            f"{result['documents']} documents for {result['shipments']} shipments written to {result['out']} "
            f"({result['scanned']} scanned)."
        ))
