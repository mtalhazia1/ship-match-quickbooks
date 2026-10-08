"""Opt-in customs paperwork for demos and tests: a CBP Form 7501-style entry summary (with deliberate duty math
errors on request), a generic customs import declaration, a carrier arrival notice, and a commercial invoice
that prints its country of origin.

The default dataset written by `generator.generate` never includes them. `generate_dataset --customs` adds them
to a dataset afterwards with `add_to_dataset()`, from its own random stream, so the rest stays identical.
Every helper returns the PDF bytes and the values printed on it (the ground truth). All parties are fictional and
every page says it is a synthetic sample.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from apps.customs.fees import expected_hmf, expected_mpf
from synthetic.generator import CARRIERS, CONSIGNEE, Page, Party

CENT = Decimal("0.01")
BROKER = Party("Harborlink Customs Brokerage LLC", "500 Ocean Gate, Long Beach, CA 90802, USA", "harborlink.example")

# Tariff numbers for the generator's products (plausible HTSUS lines; rates as printed on the sample entries).
HTS = {
    "Bluetooth speaker BX-20": ("8518.22.0000", "4.9%"),
    "USB-C charger 65W": ("8504.40.9550", "Free"),
    "Wireless earbuds E7": ("8518.30.2000", "4.9%"),
    "Power bank 20000mAh": ("8507.60.0020", "3.4%"),
    "Stainless cookware set 10pc": ("7323.93.0080", "2%"),
    "Glass food container 3pc": ("7013.49.9000", "12.5%"),
    "Silicone utensil set": ("3924.10.4000", "3.4%"),
    "Ceramic mug 350ml": ("6912.00.4810", "9.8%"),
    "Cotton bath towel 70x140": ("6302.60.0010", "9.1%"),
    "Bed sheet set queen": ("6302.31.9020", "6.7%"),
    "Kitchen towel pack of 4": ("6302.60.0030", "9.1%"),
    "Bathrobe unisex L": ("6207.91.1000", "7.4%"),
    "Oak dining chair": ("9401.69.8031", "Free"),
    "Coffee table walnut": ("9403.60.8081", "Free"),
    "Bookshelf 5-tier": ("9403.60.8081", "Free"),
    "Bar stool metal": ("9401.79.0050", "Free"),
}
SECTION_301 = ("9903.88.15", "Section 301 additional duty, List 4A", "7.5%")
ENTRY_ERRORS = ["line_duty", "total_duty", "grand_total", "mpf_over_max", "mpf", "hmf", "hts_format"]


@dataclass
class Line:
    hts: str
    description: str
    value: Decimal | None   # None: a Chapter 99 line using the value of the line above
    rate: str               # as printed: 4.9%, Free, 2.4¢/kg
    duty: Decimal | None = None


def _rate_pct(rate: str) -> Decimal | None:
    r = rate.strip().lower()
    if r == "free":
        return Decimal("0")
    if r.endswith("%"):
        return Decimal(r[:-1])
    return None


def _duty(value: Decimal, rate: str) -> Decimal:
    pct = _rate_pct(rate)
    return (value * pct / 100).quantize(CENT, rounding=ROUND_HALF_UP) if pct is not None else Decimal("0.00")


def _fmt(x: Decimal | None) -> str:
    return "" if x is None else f"{x:,.2f}"


def _us(d: date) -> str:
    return d.strftime("%m/%d/%Y")


# --------------------------------------------------------------------------- CBP Form 7501


def cbp7501(entry_number: str, *, entry_date: date = date(2026, 4, 2), bl: str = "OSLN7712345678",
            containers: list[str] | None = None, lines: list[Line] | None = None, origin: str = "CN",
            port: str = "2704", importer: Party = CONSIGNEE, broker: Party = BROKER,
            invoice_currency: str | None = None, exchange_rate: Decimal | None = None,
            tax: Decimal = Decimal("0.00"), hmf: bool = True, errors: tuple[str, ...] = (),
            import_date: date | None = None, vessel: str = "MV Coral Dawn") -> tuple[bytes, dict]:
    """An entry summary laid out like CBP Form 7501. errors: any of ENTRY_ERRORS, printed on purpose:
    line_duty (line 1 overstated by 100.00), total_duty (box 37 above the lines by 250.00), grand_total
    (box 40 above duty plus fees by 75.00), mpf_over_max (MPF at 0.3464% without the maximum), mpf (MPF
    overstated by 40.00), hmf (HMF at 0.15%), hts_format (line 1 tariff number missing a digit)."""
    containers = containers if containers is not None else ["OSLU1234564"]
    lines = [Line(x.hts, x.description, x.value, x.rate, x.duty) for x in (lines or [
        Line("8518.22.0000", "Bluetooth speaker BX-20", Decimal("18450.00"), "4.9%"),
        Line(*SECTION_301[:2], None, SECTION_301[2]),
        Line("8504.40.9550", "USB-C charger 65W", Decimal("4875.00"), "Free"),
    ])]
    last = None
    for ln in lines:  # the duty each line should carry (a Chapter 99 line uses the value above it)
        base = ln.value if ln.value is not None else last
        if ln.value is not None and not ln.hts.startswith("99"):
            last = ln.value
        if ln.duty is None:
            ln.duty = _duty(base or Decimal("0"), ln.rate)
    if "line_duty" in errors:
        lines[0].duty += Decimal("100.00")
    if "hts_format" in errors:
        lines[0].hts = lines[0].hts[:-1]
    entered = sum((ln.value for ln in lines if ln.value is not None), Decimal("0.00"))
    duty = sum((ln.duty for ln in lines), Decimal("0.00"))
    printed_duty = duty + (Decimal("250.00") if "total_duty" in errors else 0)
    mpf, mpf_rate = expected_mpf(entered, entry_date)
    if "mpf_over_max" in errors:
        mpf = (entered * mpf_rate.percent / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    if "mpf" in errors:
        mpf += Decimal("40.00")
    hmf_amount = expected_hmf(entered) if hmf else None
    if hmf and "hmf" in errors:
        hmf_amount = (entered * Decimal("0.15") / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    other = mpf + (hmf_amount or Decimal("0.00"))
    total = printed_duty + tax + other + (Decimal("75.00") if "grand_total" in errors else 0)
    import_date = import_date or entry_date - timedelta(days=1)

    p = Page(0)
    p.line((50, "DEPARTMENT OF HOMELAND SECURITY"), (545, "ENTRY SUMMARY", "r"), size=12, bold=True, step=15)
    p.line((50, "U.S. Customs and Border Protection"), (545, "CBP Form 7501 (synthetic sample)", "r"), size=9)
    p.gap(8)
    p.rule()
    p.line((50, f"1. Filer Code/Entry No.: {entry_number}"), (290, "2. Entry Type: 01 ABI/A"),
           (420, f"3. Summary Date: {_us(entry_date + timedelta(days=9))}"), size=9)
    p.line((50, "4. Surety No.: 457"), (290, "5. Bond Type: 8"), (420, f"6. Port Code: {port}"), size=9)
    p.line((50, f"7. Entry Date: {_us(entry_date)}"), (290, f"8. Importing Carrier: {vessel}"), size=9)
    p.line((50, f"10. Country of Origin: {origin}"), (290, f"11. Import Date: {_us(import_date)}"), size=9)
    p.line((50, f"12. B/L or AWB No.: {bl}"), (290, f"14. Exporting Country: {origin}"), size=9)
    if containers:
        p.line((50, f"Container(s): {', '.join(containers)}"), size=9)
    p.line((50, f"26. Importer of Record: {importer.name}"), size=9)
    if invoice_currency and exchange_rate:
        p.line((50, f"Invoice Currency: {invoice_currency}"), (290, f"Exchange Rate: {exchange_rate}"), size=9)
    p.gap(6)
    p.rule()
    p.line((50, "Line"), (80, "HTSUS No."), (165, "Description"), (395, "Entered Value", "r"),
           (470, "HTSUS Rate", "r"), (545, "Duty", "r"), size=9, bold=True)
    for n, ln in enumerate(lines, start=1):
        p.line((50, f"{n:03d}"), (80, ln.hts), (165, ln.description[:40]), (395, _fmt(ln.value), "r"),
               (470, ln.rate, "r"), (545, _fmt(ln.duty), "r"), size=9)
    p.rule()
    p.line((50, f"35. Total Entered Value: {_fmt(entered)}"), size=9)
    p.line((50, f"37. Duty: {_fmt(printed_duty)}"), (290, f"38. Tax: {_fmt(tax)}"), size=9)
    p.line((50, f"39. Other: {_fmt(other)}"), (290, f"40. Total: {_fmt(total)}"), size=9, bold=True)
    p.gap(6)
    p.line((50, "Other Fee Summary for Block 39"), size=9, bold=True)
    p.line((50, f"499 Merchandise Processing Fee: {_fmt(mpf)}"), size=9)
    if hmf_amount is not None:
        p.line((50, f"501 Harbor Maintenance Fee: {_fmt(hmf_amount)}"), size=9)
    p.gap(10)
    p.line((50, f"Broker/Filer: {broker.name}, {broker.address}"), size=8)
    p.line((50, "Synthetic sample for software testing. Not issued by U.S. Customs and Border Protection."), size=7)
    truth = {
        "entry_number": entry_number, "entry_type": "01 ABI/A", "entry_date": entry_date.isoformat(),
        "import_date": import_date.isoformat(), "port_of_entry": port, "importer_name": importer.name,
        "bl_number": bl, "container_numbers": containers, "country_of_origin": origin,
        "total_entered_value": f"{entered:.2f}", "total_duty": f"{printed_duty:.2f}",
        "merchandise_processing_fee": f"{mpf:.2f}", "total_duty_and_fees": f"{total:.2f}",
        "other_fees": f"{tax:.2f}",
        "entry_lines": [{"hts_code": ln.hts, "description": ln.description, "duty_rate": ln.rate,
                         "entered_value": None if ln.value is None else f"{ln.value:.2f}", "duty_amount": f"{ln.duty:.2f}"}
                        for ln in lines],
        "planted_errors": list(errors), "expected_mpf": f"{expected_mpf(entered, entry_date)[0]:.2f}",
    }
    if hmf_amount is not None:
        truth["harbor_maintenance_fee"] = f"{hmf_amount:.2f}"
    if invoice_currency and exchange_rate:
        truth.update({"invoice_currency": invoice_currency, "exchange_rate": str(exchange_rate)})
    return p.pdf(), truth


def lines_for_invoice(items: list[dict], rate: Decimal = Decimal("1"), origin: str = "CN") -> list[Line]:
    """Entry lines for a commercial invoice's line items, converted at `rate`; China-origin electronics get a
    Section 301 line."""
    out = []
    for item in items:
        hts, duty_rate = HTS.get(item["description"], ("9999.99.9999", "Free"))
        value = (Decimal(str(item["amount"])) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP) + Decimal("0.00")
        out.append(Line(hts, item["description"], value, duty_rate))
        if origin == "CN" and hts.startswith("85") and not any(x.hts.startswith("99") for x in out):
            out.append(Line(SECTION_301[0], SECTION_301[1], None, SECTION_301[2]))
    return out


# --------------------------------------------------------------------------- other countries' declarations


def customs_declaration(number: str = "26NL0003961234567A", *, day: date = date(2026, 4, 3), bl: str = "BMSR1234567890",
                        containers: list[str] | None = None, origin: str = "CN",
                        lines: list[tuple[str, str, str, str, str]] | None = None) -> tuple[bytes, dict]:
    """A generic (non-US) import declaration in Label: value form. lines: (code, description, value, rate, duty)."""
    containers = containers if containers is not None else ["BMSU1234566"]
    lines = lines or [("85182200", "Loudspeakers in one enclosure", "12,000.00", "2.0%", "240.00"),
                      ("63026000", "Cotton terry towels", "3,500.00", "12.0%", "420.00")]
    p = Page(0)
    p.line((50, "Rotterdam Customs Services BV"), (545, "CUSTOMS IMPORT DECLARATION", "r"), size=12, bold=True, step=15)
    p.line((50, "Waalhaven 12, Rotterdam, Netherlands"), size=9)
    p.gap(8)
    p.line((50, "Declaration No.:"), (190, number))
    p.line((50, "Declaration Date:"), (190, day.isoformat()))
    p.line((50, "Customs Office:"), (190, "NL000396 Rotterdam Maasvlakte"))
    p.line((50, "Importer:"), (190, "Acme Imports Europe BV"))
    p.line((50, "Declarant:"), (190, "Rotterdam Customs Services BV"))
    p.line((50, "Transport Document (B/L):"), (190, bl))
    p.line((50, "Container(s):"), (190, ", ".join(containers)))
    p.line((50, "Country of Origin:"), (190, origin))
    p.line((50, "Currency:"), (190, "EUR"))
    p.gap(8)
    p.line((50, "Item"), (85, "Commodity Code"), (175, "Description"), (400, "Customs Value", "r"),
           (465, "Duty Rate", "r"), (545, "Duty", "r"), size=9, bold=True)
    for n, (code, desc, value, rate, duty) in enumerate(lines, start=1):
        p.line((50, str(n)), (85, code), (175, desc[:38]), (400, value, "r"), (465, rate, "r"), (545, duty, "r"), size=9)
    total_value = sum((Decimal(v.replace(",", "")) for _, _, v, _, _ in lines), Decimal("0.00"))
    total_duty = sum((Decimal(d.replace(",", "")) for *_, d in lines), Decimal("0.00"))
    p.gap(6)
    p.line((50, f"Total Customs Value: {_fmt(total_value)}"), size=9)
    p.line((50, f"Total Duty: {_fmt(total_duty)}"), size=9)
    p.line((50, f"Total Duty and Fees: {_fmt(total_duty)}"), size=9, bold=True)
    p.line((50, "Synthetic sample for software testing."), size=7)
    truth = {"entry_number": number, "entry_date": day.isoformat(), "bl_number": bl, "container_numbers": containers,
             "country_of_origin": origin, "currency": "EUR", "total_entered_value": f"{total_value:.2f}",
             "total_duty": f"{total_duty:.2f}", "total_duty_and_fees": f"{total_duty:.2f}",
             "hts_codes": [c for c, *_ in lines]}
    return p.pdf(), truth


# --------------------------------------------------------------------------- arrival notice


def arrival_notice(bl: str = "OSLN7712345678", containers: list[str] | None = None, *,
                   carrier: Party = CARRIERS[0][0], eta: date = date(2026, 4, 6), discharge: date | None = None,
                   demurrage_days: int | None = 4, detention_days: int | None = 5, basis: str = "working",
                   printed_lfd: bool = False, printed_return: bool = False, notice_date: date | None = None,
                   terminal: str = "Pier T Container Terminal", port: str = "Long Beach, CA",
                   charges: list[tuple[str, str]] | None = None, vessel: str = "MV Coral Dawn / 512E",
                   lfd: dict | None = None, return_by: dict | None = None) -> tuple[bytes, dict]:
    """A carrier arrival notice. basis: "working" (weekends and holidays not counted), "calendar" or "" (not
    printed). printed_lfd / printed_return: print each container's last free day and empty return date (from
    `lfd` / `return_by`, {container: date}); otherwise only the free days are printed."""
    containers = containers if containers is not None else ["OSLU1234564"]
    charges = charges if charges is not None else [("Destination THC", "450.00"), ("Documentation Fee", "75.00")]
    notice_date = notice_date or eta - timedelta(days=4)
    lfd, return_by = lfd or {}, return_by or {}
    p = Page(0)
    p.line((50, carrier.name), (545, "ARRIVAL NOTICE", "r"), size=13, bold=True, step=16)
    p.line((50, carrier.address), size=9)
    p.gap(8)
    p.line((50, f"Notice Date: {notice_date.isoformat()}"), (300, f"B/L No.: {bl}"), size=10)
    p.line((50, f"Vessel / Voyage: {vessel}"), (300, f"Port of Discharge: {port}"), size=10)
    p.line((50, f"Terminal: {terminal}"), (300, f"ETA: {eta.isoformat()}"), size=10)
    if discharge:
        p.line((50, f"Discharge Date: {discharge.isoformat()}"), size=10)
    p.line((50, f"Consignee: {CONSIGNEE.name}"), size=10)
    if demurrage_days is not None or detention_days is not None:
        parts = []
        if demurrage_days is not None:
            parts.append((50, f"Demurrage Free Days: {demurrage_days}"))
        if detention_days is not None:
            parts.append((300, f"Detention Free Days: {detention_days}"))
        p.line(*parts, size=10)
    if basis == "working":
        p.line((50, "Free time is counted in working days (excluding Saturdays, Sundays and public holidays)."), size=9)
    elif basis == "calendar":
        p.line((50, "Free time is counted in calendar days, including weekends and holidays."), size=9)
    p.gap(8)
    p.line((50, "Container No."), (160, "Size/Type"), (240, "Discharged"), (330, "Last Free Day"),
           (440, "Empty Return By"), size=9, bold=True)
    p.rule()
    for c in containers:
        p.line((50, c), (160, "40HC"), (240, discharge.isoformat() if discharge else ""),
               (330, lfd[c].isoformat() if printed_lfd and c in lfd else ""),
               (440, return_by[c].isoformat() if printed_return and c in return_by else ""), size=9)
    p.gap(10)
    total = sum((Decimal(a) for _, a in charges), Decimal("0.00"))
    if charges:
        p.line((50, "Charges due before release"), size=10, bold=True)
        p.line((50, "Description"), (545, "Amount (USD)", "r"), size=9, bold=True)
        for desc, amount in charges:
            p.line((50, desc), (545, f"{Decimal(amount):,.2f}", "r"), size=9)
        p.line((330, "Total due before release:"), (545, f"{total:,.2f}", "r"), size=9, bold=True)
    p.gap(14)
    p.line((50, "Release requires the original B/L or telex release, customs release and payment of the charges above."),
           size=8)
    p.line((50, "Synthetic sample for software testing."), size=7)
    truth = {"carrier_name": carrier.name, "bl_number": bl, "container_numbers": containers,
             "estimated_arrival_date": eta.isoformat(), "terminal": terminal, "port_of_discharge": port,
             "vessel_voyage": vessel, "notice_date": notice_date.isoformat()}
    if discharge:
        truth["discharge_date"] = discharge.isoformat()
    if demurrage_days is not None:
        truth["demurrage_free_days"] = demurrage_days
    if detention_days is not None:
        truth["detention_free_days"] = detention_days
    if charges:
        truth["total_amount"] = f"{total:.2f}"
    return p.pdf(), truth


# --------------------------------------------------------------------------- commercial invoice with origin


def commercial_invoice(number: str, *, bl: str, containers: list[str], origin_country: str = "China",
                       items: list[tuple[str, int, str]] | None = None, currency: str = "USD",
                       vendor: Party | None = None, po: str = "PO-2026-7001", day: date = date(2026, 3, 28),
                       ) -> tuple[bytes, dict]:
    """A supplier invoice that prints the country of origin (the default dataset's invoices don't).
    items: (description, quantity, unit price)."""
    from synthetic.generator import SUPPLIERS

    vendor = vendor or SUPPLIERS[0]
    items = items or [("Bluetooth speaker BX-20", 1000, "18.45"), ("USB-C charger 65W", 500, "9.75")]
    rows = [(d, q, Decimal(u), (Decimal(u) * q).quantize(CENT)) for d, q, u in items]
    total = sum((a for *_, a in rows), Decimal("0.00"))
    p = Page(0)
    p.line((50, vendor.name), (545, "COMMERCIAL INVOICE", "r"), size=13, bold=True, step=16)
    p.line((50, vendor.address), size=9)
    p.gap()
    p.line((50, "Invoice No.:"), (160, number))
    p.line((50, "Date:"), (160, day.isoformat()))
    p.line((50, "PO No.:"), (160, po))
    p.line((50, "Currency:"), (160, currency))
    p.line((50, "B/L No.:"), (160, bl))
    p.line((50, "Container(s):"), (160, ", ".join(containers)))
    p.line((50, "Country of Origin:"), (160, origin_country))
    p.gap()
    p.line((50, "Buyer:"), (160, CONSIGNEE.name))
    p.gap(14)
    p.line((50, "Description"), (360, "Qty", "r"), (450, "Unit Price", "r"), (545, "Amount", "r"), bold=True)
    p.rule()
    for d, q, u, a in rows:
        p.line((50, d), (360, str(q), "r"), (450, f"{u:,.2f}", "r"), (545, f"{a:,.2f}", "r"))
    p.rule()
    p.line((330, f"TOTAL ({currency}):"), (545, f"{total:,.2f}", "r"), bold=True)
    p.gap(30)
    p.line((50, "Terms: FOB origin. Payment 60 days from B/L date."), size=8)
    truth = {"vendor_name": vendor.name, "invoice_number": number, "total_amount": f"{total:.2f}", "currency": currency,
             "country_of_origin": origin_country,
             "line_items": [{"description": d, "amount": f"{a:.2f}"} for d, _, _, a in rows]}
    return p.pdf(), truth


# --------------------------------------------------------------------------- dataset add-on


def add_to_dataset(out_dir: str | Path, seed: int = 42, today: date | None = None) -> dict:
    """Add customs entries and arrival notices to a dataset written by generator.generate (opt-in).

    Every third shipment with a commercial invoice gets a CBP 7501-style entry built from its invoice (some with
    a planted duty error), every other shipment with a B/L gets an arrival notice dated around `today` so free
    time countdowns are live. Documents are added to ground_truth.json (doc types customs_entry and
    arrival_notice) and emails.json; nothing else in the dataset changes."""
    out = Path(out_dir)
    truth = json.loads((out / "ground_truth.json").read_text())
    emails = json.loads((out / "emails.json").read_text())
    rng = random.Random(seed * 104729 + 7)
    today = today or date.today()
    by_shipment: dict[str, dict] = {}
    for d in truth["documents"]:
        by_shipment.setdefault(d["shipment_id"], {}).setdefault(d["doc_type"], d)
    error_cycle = ["line_duty", None, "mpf", None, "total_duty", None, "hmf", "hts_format"]
    added, last_time = [], max((e["received_at"] for e in emails), default="2026-09-01T08:00:00+00:00")
    start = date.fromisoformat(last_time[:10])
    n_entries = 0
    for i, meta in enumerate(truth.get("shipments", [])):
        sid, docs = meta["shipment_id"], by_shipment.get(meta["shipment_id"], {})
        ci, bl_doc = docs.get("commercial_invoice"), docs.get("bill_of_lading")
        if ci and i % 3 == 0:
            cur = ci["fields"].get("currency") or "USD"
            rate = Decimal("1.0850") if cur == "EUR" else Decimal("1")
            origin = {"Anatolia": "TR", "Mekong": "VN"}.get(ci["fields"]["vendor_name"].split()[0], "CN")
            error = error_cycle[n_entries % len(error_cycle)]
            n_entries += 1
            pdf, t = cbp7501(f"HLB-{2604000 + i * 37:07d}-{(i * 7) % 10}", bl=meta["bl_number"],
                             containers=meta["container_numbers"], origin=origin,
                             entry_date=date.fromisoformat(ci["fields"]["invoice_date"]) + timedelta(days=24),
                             lines=lines_for_invoice(ci["fields"]["line_items"], rate, origin),
                             invoice_currency=cur if cur != "USD" else None,
                             exchange_rate=rate if cur != "USD" else None, errors=(error,) if error else ())
            name = f"{sid}_customs_entry.pdf"
            (out / "pdf" / name).write_bytes(pdf)
            planted, expected_mpf, lines = t.pop("planted_errors"), t.pop("expected_mpf"), t.pop("entry_lines")
            added.append({"file": name, "doc_type": "customs_entry", "shipment_id": sid, "scanned": False,
                          "planted_errors": [], "planted_customs_errors": planted, "expected_mpf": expected_mpf,
                          "entry_lines": lines, "fields": t,
                          "sender": BROKER, "subject": f"Entry summary {t['entry_number']} - B/L {meta['bl_number']}"})
        if bl_doc and i % 2 == 0:
            carrier = next((c for c, _, prefix in CARRIERS if meta["bl_number"].startswith(prefix)), CARRIERS[0][0])
            discharge = today - timedelta(days=rng.choice([1, 2, 3, 5, 8]))
            pdf, t = arrival_notice(meta["bl_number"], meta["container_numbers"], carrier=carrier,
                                    eta=discharge - timedelta(days=1), discharge=discharge,
                                    demurrage_days=rng.choice([4, 5]), detention_days=rng.choice([4, 7]),
                                    basis=rng.choice(["working", "calendar"]),
                                    port=bl_doc["fields"].get("port_of_discharge") or "Long Beach, CA",
                                    vessel=bl_doc["fields"].get("vessel_voyage") or "MV Coral Dawn / 512E")
            name = f"{sid}_arrival_notice.pdf"
            (out / "pdf" / name).write_bytes(pdf)
            added.append({"file": name, "doc_type": "arrival_notice", "shipment_id": sid, "scanned": False,
                          "planted_errors": [], "fields": t, "sender": carrier,
                          "subject": f"Arrival notice B/L {meta['bl_number']}"})
    for n, d in enumerate(added):
        sender, subject = d.pop("sender"), d.pop("subject")
        emails.append({"file": d["file"], "from": f"notices@{sender.domain}", "subject": subject,
                       "message_id": f"<synthetic-customs-{seed}-{n}@{sender.domain}>",
                       "received_at": f"{start.isoformat()}T{8 + n % 10:02d}:{(n * 7) % 60:02d}:00+00:00"})
    truth["documents"] = sorted(truth["documents"] + added, key=lambda x: x["file"])
    (out / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    (out / "emails.json").write_text(json.dumps(emails, indent=2))
    return {"customs_entries": sum(d["doc_type"] == "customs_entry" for d in added),
            "arrival_notices": sum(d["doc_type"] == "arrival_notice" for d in added)}


__all__ = ["BROKER", "ENTRY_ERRORS", "HTS", "Line", "add_to_dataset", "arrival_notice", "cbp7501",
           "commercial_invoice", "customs_declaration", "lines_for_invoice"]
