"""Demo quotes and approved extra charges matching the synthetic dataset (synthetic/generator.py).

    python manage.py seed_rates --org demo

Idempotent: quotes are keyed by their reference and replaced on a second run.

What it creates, all for the fictional vendors in the generator:
  * 40HC quotes for 2026 from each forwarder (Harborlink, Swift Cargo, Atlas Freight) for every
    origin and destination port in the dataset, except Atlas Freight from Ho Chi Minh City, so the
    demo shows a "no matching quote" warning.
  * Harborlink's expired 2025 quotes (lower rates), to show history and date matching.
  * Metro Drayage quotes per port of discharge (origin left empty = any).
  * Approved extra charges: demurrage and storage after free days for the forwarders; detention,
    waiting time, pre-pull and chassis split for Metro Drayage. Exam fees, congestion surcharges,
    admin fees and re-deliveries are deliberately not approved.

The rates sit near the top of the generator's price ranges, so most invoices pass and a few go
over the quote (plus the planted 5x ocean freight outlier). Generate the dataset with
`generate_dataset --accessorials` to also get extra charges on some invoices.
"""
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.models import Organization
from apps.core.utils import audit
from apps.rates import lanes
from apps.rates.models import ApprovedAccessorial, Quote, QuoteCharge
from apps.rates.services import recheck, snapshot
from synthetic.generator import FORWARDERS, POD, POL, TRUCKER

D = Decimal
FORWARDER_RATES = {  # vendor -> (prefix, ocean freight per 40HC)
    FORWARDERS[0].name: ("HL", D("2350.00")),
    FORWARDERS[1].name: ("SC", D("2400.00")),
    FORWARDERS[2].name: ("AF", D("2450.00")),
}
FORWARDER_CHARGES = [  # code, description, amount, basis (ocean freight is per vendor)
    ("thc_destination", "Terminal handling at destination", D("350.00"), QuoteCharge.Basis.CONTAINER),
    ("documentation", "Documentation fee", D("75.00"), QuoteCharge.Basis.BL),
    ("customs_clearance", "Customs clearance", D("195.00"), QuoteCharge.Basis.SHIPMENT),
    ("security_filing", "ISF filing", D("45.00"), QuoteCharge.Basis.SHIPMENT),
]
DRAYAGE_CHARGES = [
    ("trucking", "Drayage port to warehouse", D("630.00"), QuoteCharge.Basis.CONTAINER),
    ("chassis", "Chassis rental", D("55.00"), QuoteCharge.Basis.CONTAINER),
    ("fuel_surcharge", "Fuel surcharge", D("90.00"), QuoteCharge.Basis.CONTAINER),
]
NO_QUOTE = {(FORWARDERS[2].name, "Ho Chi Minh City (Cat Lai)")}
APPROVED = [  # vendor, code, unit, free units, max per unit, max per invoice, note
    (FORWARDERS[0].name, "demurrage", "day", 4, D("150.00"), None, "Contract clause 7.1"),
    (FORWARDERS[0].name, "storage", "day", 5, D("60.00"), None, "Contract clause 7.2"),
    (FORWARDERS[1].name, "demurrage", "day", 4, D("150.00"), None, "Rate agreement 2026"),
    (FORWARDERS[1].name, "storage", "day", 5, D("60.00"), D("600.00"), "Rate agreement 2026"),
    (FORWARDERS[2].name, "demurrage", "day", 4, D("150.00"), None, ""),
    (TRUCKER.name, "detention", "day", 4, D("100.00"), None, "Drayage terms, section 3"),
    (TRUCKER.name, "waiting_time", "hour", 2, D("85.00"), None, "Two free hours at pickup and delivery"),
    (TRUCKER.name, "pre_pull", "each", 0, D("150.00"), None, ""),
    (TRUCKER.name, "chassis_split", "each", 0, D("75.00"), None, ""),
]


def _code(place: str) -> str:
    p = lanes.resolve(place)
    return p.code[2:] if p else place[:3].upper()


class Command(BaseCommand):
    help = "Create demo quotes and approved extra charges that match the synthetic dataset (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="demo", help="Organization slug")
        parser.add_argument("--no-recheck", action="store_true", help="Don't re-check open shipments afterwards")

    def handle(self, *args, **opts):
        org = Organization.objects.filter(slug=opts["org"]).first()
        if org is None:
            raise CommandError(f"No organization '{opts['org']}'. Run seed_demo first or pass --org.")
        made = 0
        with transaction.atomic():
            for vendor, (prefix, ocean) in FORWARDER_RATES.items():
                for origin in POL:
                    for dest in POD:
                        if (vendor, origin) in NO_QUOTE:
                            continue
                        lines = [("ocean_freight", "Ocean freight", ocean, QuoteCharge.Basis.CONTAINER),
                                 *FORWARDER_CHARGES]
                        ref = f"{prefix}-26-{_code(origin)}-{_code(dest)}"
                        made += self._quote(org, vendor, ref, origin, dest, "40HC", date(2026, 1, 1),
                                            date(2026, 12, 31), lines, "Annual rate agreement 2026")
                        if prefix == "HL":  # last year's rates, now expired
                            old = [(c, d, (a * D("0.95")).quantize(D("1.00")), b) for c, d, a, b in lines]
                            made += self._quote(org, vendor, f"HL-25-{_code(origin)}-{_code(dest)}", origin, dest,
                                                "40HC", date(2025, 1, 1), date(2025, 12, 31), old,
                                                "Annual rate agreement 2025")
            for dest in POD:
                made += self._quote(org, TRUCKER.name, f"MD-26-{_code(dest)}", "", dest, "40HC", date(2026, 1, 1),
                                    None, DRAYAGE_CHARGES, "Drayage tariff, port to warehouse within 50 miles")
            for vendor, code, unit, free, per_unit, cap, note in APPROVED:
                ApprovedAccessorial.objects.update_or_create(
                    organization=org, vendor_name=vendor, code=code,
                    defaults={"unit": unit, "free_units": free, "max_per_unit": per_unit, "max_amount": cap,
                              "currency": "USD", "valid_from": date(2026, 1, 1), "valid_to": None, "notes": note})
        self.stdout.write(self.style.SUCCESS(
            f"{made} quotes and {len(APPROVED)} approved extra charges ready for '{org.slug}'."))
        if not opts["no_recheck"]:
            n = recheck(org)
            self.stdout.write(f"Checked {n} open shipment{'s' if n != 1 else ''} against the rates.")

    def _quote(self, org, vendor, ref, origin, dest, equipment, start, end, lines, notes) -> int:
        q, created = Quote.objects.update_or_create(
            organization=org, reference=ref,
            defaults={"vendor_name": vendor, "origin": origin, "destination": dest, "equipment": equipment,
                      "valid_from": start, "valid_to": end, "currency": "USD", "all_in": False, "notes": notes,
                      "archived": False})
        q.charges.all().delete()
        QuoteCharge.objects.bulk_create([QuoteCharge(quote=q, code=c, description=d, amount=a, basis=b)
                                         for c, d, a, b in lines])
        audit(org, "quote.created" if created else "quote.updated", q, vendor=q.vendor_name, name=q.audit_name,
              reference=q.reference, lane=q.lane, source="seed_rates demo data", quote=snapshot(q))
        return 1
