"""Synthetic shipment documents with ground truth and planted errors.

Generates fictional import shipments (commercial invoice, bill of lading, freight invoices)
as PDFs in several visual layouts, plus:
  * ground_truth.json  - correct values for every document and the errors planted in it
  * emails.json        - simulated email arrival (sender, subject, time), shuffled order

All company names are fictional. Usage:
    python manage.py generate_dataset --out datasets/synthetic --shipments 20 --seed 42
"""
from __future__ import annotations

import io
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from apps.shipments.services.containers import make_container

# ---------------------------------------------------------------------------
# Fictional parties
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Party:
    name: str
    address: str
    domain: str


SUPPLIERS = [
    Party("Brightway Electronics Co., Ltd.", "88 Keyuan Road, Shenzhen, China", "brightway-elec.example"),
    Party("Apex Housewares Manufacturing Ltd.", "12 Jiangbei Industrial Park, Ningbo, China", "apexhousewares.example"),
    Party("Anatolia Textile Export A.S.", "Organize Sanayi Bolgesi 4, Bursa, Turkey", "anatolia-textile.example"),
    Party("Mekong Furniture Joint Stock Co.", "Lot B7 Song Than IZ, Binh Duong, Vietnam", "mekongfurniture.example"),
]
CARRIERS = [  # (party, container owner code, B/L prefix)
    (Party("Oceanic Star Line", "1 Marina Plaza, Singapore", "oceanicstar.example"), "OSL", "OSLN"),
    (Party("Blue Meridian Shipping", "40 Kade Street, Rotterdam, Netherlands", "bluemeridian.example"), "BMS", "BMSR"),
    (Party("Pacific Crest Lines", "200 Harbour Road, Hong Kong", "pacificcrest.example"), "PCX", "PCXL"),
]
FORWARDERS = [
    Party("Harborlink Logistics LLC", "500 Ocean Gate, Long Beach, CA 90802, USA", "harborlink.example"),
    Party("Swift Cargo Forwarding Inc.", "77 Port Ave, Newark, NJ 07114, USA", "swiftcargo.example"),
    Party("Atlas Freight Partners LLC", "1600 Terminal Dr, Houston, TX 77029, USA", "atlasfreight.example"),
]
TRUCKER = Party("Metro Drayage Co.", "9 Container Way, Carson, CA 90745, USA", "metrodrayage.example")
CONSIGNEE = Party("Acme Imports LLC", "1200 Harbor Blvd, Long Beach, CA 90802, USA", "acme-imports.example")

PRODUCTS = {
    "Brightway Electronics Co., Ltd.": [("Bluetooth speaker BX-20", 18.5), ("USB-C charger 65W", 9.75), ("Wireless earbuds E7", 14.2), ("Power bank 20000mAh", 12.4)],
    "Apex Housewares Manufacturing Ltd.": [("Stainless cookware set 10pc", 42.0), ("Glass food container 3pc", 6.8), ("Silicone utensil set", 4.35), ("Ceramic mug 350ml", 1.9)],
    "Anatolia Textile Export A.S.": [("Cotton bath towel 70x140", 5.6), ("Bed sheet set queen", 17.9), ("Kitchen towel pack of 4", 3.25), ("Bathrobe unisex L", 14.8)],
    "Mekong Furniture Joint Stock Co.": [("Oak dining chair", 38.0), ("Coffee table walnut", 96.0), ("Bookshelf 5-tier", 64.5), ("Bar stool metal", 27.0)],
}
POL = ["Shenzhen (Yantian)", "Ningbo", "Istanbul (Ambarli)", "Ho Chi Minh City (Cat Lai)"]
POD = ["Long Beach, CA", "Newark, NJ", "Houston, TX"]

PLANTED_ERRORS = ["total_mismatch", "invalid_container", "container_not_on_bl", "duplicate_invoice", "amount_outlier", "missing_bl"]

# ---------------------------------------------------------------------------
# Formatting helpers (layouts deliberately differ, like real vendors do)
# ---------------------------------------------------------------------------

TWO = Decimal("0.01")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(TWO, rounding=ROUND_HALF_UP)


def fmt_date(d: date, style: int) -> str:
    return [d.isoformat(), d.strftime("%d %b %Y"), d.strftime("%m/%d/%Y")][style]


def fmt_money(x: Decimal, style: int) -> str:
    return [f"{x:,.2f}", f"{x:.2f}", f"{x:,.2f}"][style]


FONTS = [("Helvetica", "Helvetica-Bold"), ("Times-Roman", "Times-Bold"), ("Courier", "Courier-Bold")]


class Page:
    """Thin wrapper around a reportlab canvas with a cursor."""

    def __init__(self, variant: int):
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=A4, invariant=1)
        self.font, self.bold = FONTS[variant]
        self.y = 800

    def text(self, x, s, size=10, bold=False, right=False):
        self.c.setFont(self.bold if bold else self.font, size)
        (self.c.drawRightString if right else self.c.drawString)(x, self.y, s)

    def line(self, *parts, size=10, bold=False, step=15):
        """parts: (x, text) or (x, text, 'r') for right-aligned."""
        for p in parts:
            self.text(p[0], p[1], size=size, bold=bold, right=len(p) > 2)
        self.y -= step

    def gap(self, n=10):
        self.y -= n

    def rule(self):
        self.c.line(50, self.y + 8, 545, self.y + 8)
        self.y -= 6

    def stamp(self, s):
        self.c.saveState()
        self.c.setFont(self.bold, 40)
        self.c.setFillGray(0.75)
        self.c.translate(300, 420)
        self.c.rotate(30)
        self.c.drawCentredString(0, 0, s)
        self.c.restoreState()

    def pdf(self) -> bytes:
        self.c.showPage()
        self.c.save()
        return self.buf.getvalue()


# ---------------------------------------------------------------------------
# Document renderers
# ---------------------------------------------------------------------------


def render_commercial_invoice(d: dict, v: int) -> bytes:
    p = Page(v)
    sup = d["vendor"]
    if v == 0:
        p.line((50, sup.name, ), (545, "COMMERCIAL INVOICE", "r"), size=13, bold=True, step=16)
        p.line((50, sup.address), size=9)
    elif v == 1:
        p.line((300, "Commercial Invoice", "c"), size=15, bold=True)  # 'c' treated as right; fine for layout variety
        p.line((50, sup.name), size=12, bold=True)
        p.line((50, sup.address), size=9)
    else:
        p.line((50, sup.name), size=12, bold=True)
        p.line((50, sup.address), size=9)
        p.line((50, "COMMERCIAL INVOICE"), size=13, bold=True)
    p.gap()
    labels = [("Invoice No.:", "Date:", "PO No.:", "Currency:"),
              ("Invoice Number:", "Invoice Date:", "Purchase Order:", "Currency:"),
              ("Invoice #:", "Dated:", "P.O. Number:", "Currency:")][v]
    p.line((50, labels[0]), (160, d["invoice_number"]))
    p.line((50, labels[1]), (160, fmt_date(d["invoice_date"], v)))
    p.line((50, labels[2]), (160, ", ".join(d["po_numbers"])))
    p.line((50, labels[3]), (160, d["currency"]))
    p.line((50, "Container(s):"), (160, ", ".join(d["container_numbers"])))
    p.gap()
    p.line((50, "Buyer:"), (160, CONSIGNEE.name))
    p.line((160, CONSIGNEE.address), size=9)
    p.gap(14)
    p.line((50, "Description"), (360, "Qty", "r"), (450, "Unit Price", "r"), (545, "Amount", "r"), bold=True)
    p.rule()
    for li in d["line_items"]:
        p.line((50, li["description"]), (360, str(li["quantity"]), "r"),
               (450, fmt_money(li["unit_price"], v), "r"), (545, fmt_money(li["amount"], v), "r"))
    p.rule()
    total_label = ["TOTAL", "Total Amount", "Grand Total"][v]
    p.line((330, f"{total_label} ({d['currency']}):"), (545, fmt_money(d["printed_total"], v), "r"), bold=True)
    p.gap(30)
    p.line((50, "Terms: FOB origin. Payment 60 days from B/L date."), size=8)
    if d.get("copy"):
        p.stamp("COPY")
    return p.pdf()


def render_bill_of_lading(d: dict, v: int) -> bytes:
    p = Page(v)
    car = d["carrier"]
    title = ["BILL OF LADING", "OCEAN BILL OF LADING", "Combined Transport Bill of Lading"][v]
    p.line((50, car.name), (545, title, "r"), size=13, bold=True, step=16)
    p.line((50, car.address), size=9)
    p.gap()
    bl_label = ["B/L No.:", "Bill of Lading No.:", "B/L Number:"][v]
    p.line((50, bl_label), (180, d["bl_number"]))
    p.line((50, "Date of Issue:"), (180, fmt_date(d["issue_date"], v)))
    p.line((50, "Shipper:"), (180, d["shipper"].name))
    p.line((50, "Consignee:"), (180, CONSIGNEE.name))
    p.line((50, "Notify Party:"), (180, "Same as consignee"))
    p.line((50, "Vessel / Voyage:"), (180, d["vessel_voyage"]))
    p.line((50, "Port of Loading:"), (180, d["port_of_loading"]))
    p.line((50, "Port of Discharge:"), (180, d["port_of_discharge"]))
    p.line((50, "Shipper's Ref / PO:"), (180, ", ".join(d["po_numbers"])))
    p.gap(14)
    p.line((50, "Container No."), (200, "Seal No."), (330, "Type"), (545, "Gross Weight (kg)", "r"), bold=True)
    p.rule()
    for c, seal in zip(d["container_numbers"], d["seals"]):
        p.line((50, c), (200, seal), (330, "40HC"), (545, f"{d['rng'].randint(9000, 21000):,}", "r"))
    p.rule()
    p.gap(20)
    p.line((50, "Freight: COLLECT. Shipped on board in apparent good order and condition."), size=8)
    return p.pdf()


def render_freight_invoice(d: dict, v: int) -> bytes:
    p = Page(v)
    ven = d["vendor"]
    if v == 0:
        p.line((50, ven.name), (545, "FREIGHT INVOICE", "r"), size=13, bold=True, step=16)
        p.line((50, ven.address), size=9)
    elif v == 1:
        p.line((50, ven.name), size=13, bold=True)
        p.line((50, ven.address), size=9)
        p.line((50, "INVOICE - Freight & Logistics Charges"), size=12, bold=True)
    else:
        p.line((50, "INVOICE"), size=15, bold=True)
        p.line((50, ven.name), size=11, bold=True)
        p.line((50, ven.address), size=9)
    p.gap()
    labels = [("Invoice No.:", "Invoice Date:", "Due Date:", "B/L No.:"),
              ("Invoice Number:", "Date:", "Payment Due:", "Bill of Lading No.:"),
              ("Invoice #:", "Issued:", "Due:", "MBL:")][v]
    p.line((50, labels[0]), (170, d["invoice_number"]))
    p.line((50, labels[1]), (170, fmt_date(d["invoice_date"], v)))
    p.line((50, labels[2]), (170, fmt_date(d["due_date"], v)))
    p.line((50, labels[3]), (170, d["bl_number"]))
    p.line((50, "Container(s):"), (170, ", ".join(d["container_numbers"])))
    p.line((50, "Customer Ref / PO:"), (170, ", ".join(d["po_numbers"])))
    p.line((50, "Bill To:"), (170, CONSIGNEE.name))
    p.gap(14)
    p.line((50, "Charge Description"), (545, f"Amount ({d['currency']})", "r"), bold=True)
    p.rule()
    for li in d["line_items"]:
        p.line((50, li["description"]), (545, fmt_money(li["amount"], v), "r"))
    p.rule()
    total_label = ["TOTAL DUE", "Total", "Amount Due"][v]
    p.line((330, f"{total_label}:"), (545, fmt_money(d["printed_total"], v), "r"), bold=True)
    p.gap(30)
    p.line((50, f"Please remit to {ven.name}. Reference our invoice number on payment."), size=8)
    if d.get("copy"):
        p.stamp("RE-SENT COPY")
    return p.pdf()


RENDERERS = {
    "commercial_invoice": render_commercial_invoice,
    "bill_of_lading": render_bill_of_lading,
    "freight_invoice": render_freight_invoice,
}

# ---------------------------------------------------------------------------
# Scanned look (image-only PDF; needs OCR to read)
# ---------------------------------------------------------------------------


def make_scanned(pdf_bytes: bytes, rng: random.Random) -> bytes:
    import pdfplumber
    from PIL import Image, ImageFilter

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        img = pdf.pages[0].to_image(resolution=150).original.convert("L")
    img = img.rotate(rng.uniform(-1.2, 1.2), expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(0.6))
    out = io.BytesIO()
    Image.Image.save(img, out, format="PDF", resolution=150)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Shipment generation
# ---------------------------------------------------------------------------


@dataclass
class GenDoc:
    file: str
    doc_type: str
    shipment_id: str
    fields: dict
    planted_errors: list = field(default_factory=list)
    sender: Party | None = None
    subject: str = ""
    scanned: bool = False
    planted_charges: list = field(default_factory=list)  # extra charges added with accessorials=True


def _line_items_goods(rng, supplier: Party) -> list[dict]:
    items = rng.sample(PRODUCTS[supplier.name], rng.randint(2, 4))
    out = []
    for desc, price in items:
        qty = rng.choice([200, 300, 480, 500, 600, 800, 1000, 1200])
        unit = money(price * rng.uniform(0.95, 1.05))
        out.append({"description": desc, "quantity": qty, "unit_price": unit, "amount": money(unit * qty)})
    return out


def _line_items_freight(rng, n_containers: int, outlier: bool) -> list[dict]:
    per = money(rng.uniform(1900, 2500)) * (5 if outlier else 1)
    items = [
        {"description": "Ocean Freight", "amount": money(per * n_containers)},
        {"description": "Terminal Handling Charge (THC)", "amount": money(rng.uniform(280, 360) * n_containers)},
        {"description": "Documentation Fee", "amount": money(rng.choice([65, 75, 85]))},
        {"description": "Customs Clearance", "amount": money(rng.choice([150, 175, 195]))},
    ]
    if rng.random() < 0.5:
        items.append({"description": "ISF Filing Fee", "amount": money(rng.choice([35, 45]))})
    return items


def _line_items_trucking(rng, n_containers: int) -> list[dict]:
    return [
        {"description": "Drayage Port to Warehouse", "amount": money(rng.uniform(480, 650) * n_containers)},
        {"description": "Chassis Rental", "amount": money(rng.choice([45, 55]) * n_containers)},
        {"description": "Fuel Surcharge", "amount": money(rng.uniform(60, 95) * n_containers)},
    ]


# Opt-in extra charges (generate(..., accessorials=True)). They come from their own random stream,
# so turning them on never changes anything else in the dataset, and the default output is unchanged.
FORWARDER_EXTRAS = [
    ("demurrage", lambda r, n: (f"Demurrage {(d := r.randint(5, 8))} days @ 175.00/day", money(175 * d))),
    ("exam", lambda r, n: ("Exam Fee (CET)", money(r.choice([325, 350, 395])))),
    ("congestion", lambda r, n: ("Port Congestion Surcharge", money(150 * n))),
    ("storage", lambda r, n: (f"Storage {(d := r.randint(6, 9))} days @ 85.00/day", money(85 * d))),
    ("admin_fee", lambda r, n: ("Admin Fee", money(45))),
]
TRUCKER_EXTRAS = [
    ("waiting_time", lambda r, n: (f"Waiting Time {(h := r.randint(3, 4))} hrs @ 95.00/hr", money(95 * h))),
    ("detention", lambda r, n: (f"Detention {(d := r.randint(6, 7))} days @ 125.00/day", money(125 * d))),
    ("pre_pull", lambda r, n: ("Pre-pull", money(150))),
    ("redelivery", lambda r, n: ("Re-delivery", money(225))),
    ("chassis_split", lambda r, n: ("Chassis Split", money(75))),
]


def _extras(rng: random.Random, table: list, n_containers: int, how_many: int) -> list[dict]:
    out = []
    for code, make in rng.sample(table, how_many):
        desc, amount = make(rng, n_containers)
        out.append({"description": desc, "amount": amount, "code": code})
    return out


def _total(items: list[dict]) -> Decimal:
    return sum((i["amount"] for i in items), Decimal("0.00"))


def _gt_fields(doc_type: str, d: dict) -> dict:
    """Ground-truth values exactly as printed on the document."""
    if doc_type == "bill_of_lading":
        return {
            "carrier_name": d["carrier"].name, "bl_number": d["bl_number"], "issue_date": d["issue_date"].isoformat(),
            "shipper": d["shipper"].name, "consignee": CONSIGNEE.name, "port_of_loading": d["port_of_loading"],
            "port_of_discharge": d["port_of_discharge"], "vessel_voyage": d["vessel_voyage"],
            "container_numbers": d["container_numbers"], "po_numbers": d["po_numbers"],
        }
    f = {
        "vendor_name": d["vendor"].name, "invoice_number": d["invoice_number"],
        "invoice_date": d["invoice_date"].isoformat(), "currency": d["currency"],
        "container_numbers": d["container_numbers"], "po_numbers": d["po_numbers"],
        "total_amount": f"{d['printed_total']:.2f}",
        "line_items": [{k: (f"{v:.2f}" if isinstance(v, Decimal) else v) for k, v in li.items()} for li in d["line_items"]],
    }
    if doc_type == "freight_invoice":
        f["due_date"] = d["due_date"].isoformat()
        f["bl_number"] = d["bl_number"]
    return f


def generate(out_dir: str | Path, n_shipments: int = 20, seed: int = 42, scanned: int = 0,
             accessorials: bool = False) -> dict:
    """Write the dataset. accessorials=True adds extra charges (demurrage, detention, exam fees ...)
    to some freight and trucking invoices, for demos of rate checks; off by default."""
    rng = random.Random(seed)
    extra_rng = random.Random(seed * 7919 + 17)  # separate stream: the main dataset stays identical
    out = Path(out_dir)
    (out / "pdf").mkdir(parents=True, exist_ok=True)
    docs: list[GenDoc] = []
    shipments_meta = []

    # Assign each planted error to its own shipment so their effects never overlap.
    error_slots = {err: i + 2 for i, err in enumerate(PLANTED_ERRORS)}  # shipments S03..S08
    base_day = date(2026, 3, 2)

    for i in range(n_shipments):
        sid = f"S{i + 1:02d}"
        planted = {e for e, slot in error_slots.items() if slot == i}
        supplier = SUPPLIERS[i % len(SUPPLIERS)]
        carrier, owner, bl_prefix = CARRIERS[i % len(CARRIERS)]
        forwarder = FORWARDERS[i % len(FORWARDERS)]
        n_cont = rng.choice([1, 1, 2, 2, 3])
        containers = [make_container(owner, rng.randint(100000, 999999)) for _ in range(n_cont)]
        po = f"PO-2026-{1000 + i * 7 + rng.randint(0, 6)}"
        bl = f"{bl_prefix}{rng.randint(10**9, 10**10 - 1)}"
        ship_day = base_day + timedelta(days=i * 9 + rng.randint(0, 4))
        currency = "EUR" if supplier.name.startswith("Anatolia") else "USD"

        # Commercial invoice (supplier)
        ci_items = _line_items_goods(rng, supplier)
        ci = {"vendor": supplier, "invoice_number": f"CI-{rng.randint(10000, 99999)}", "invoice_date": ship_day - timedelta(days=3),
              "po_numbers": [po], "currency": currency, "container_numbers": containers, "line_items": ci_items}
        ci["printed_total"] = _total(ci_items)

        # Bill of lading (carrier)
        bol = {"carrier": carrier, "bl_number": bl, "issue_date": ship_day, "shipper": supplier, "container_numbers": containers,
               "seals": [f"SL{rng.randint(100000, 999999)}" for _ in containers], "po_numbers": [po],
               "vessel_voyage": f"{rng.choice(['MV Coral Dawn', 'MV Northern Reach', 'MV Silver Tide'])} / {rng.randint(100, 999)}E",
               "port_of_loading": POL[SUPPLIERS.index(supplier)], "port_of_discharge": rng.choice(POD), "rng": rng}

        # Freight invoice (forwarder)
        fr_items = _line_items_freight(rng, n_cont, outlier="amount_outlier" in planted)
        fr_extras = _extras(extra_rng, FORWARDER_EXTRAS, n_cont, 1 + (i % 2)) if accessorials and i % 3 == 1 else []
        fr_items = fr_items + [{"description": x["description"], "amount": x["amount"]} for x in fr_extras]
        fr_containers = list(containers)
        if "invalid_container" in planted:
            c = fr_containers[0]
            fr_containers[0] = c[:10] + str((int(c[10]) + 3) % 10)  # OCR-style digit error: check digit fails
        if "container_not_on_bl" in planted:
            fr_containers[0] = make_container(owner, rng.randint(100000, 999999))  # valid, but not this shipment's box
        fr = {"vendor": forwarder, "invoice_number": f"{forwarder.name[:2].upper()}-{rng.randint(100000, 999999)}",
              "invoice_date": ship_day + timedelta(days=rng.randint(18, 26)), "currency": "USD", "bl_number": bl,
              "container_numbers": fr_containers, "po_numbers": [po], "line_items": fr_items}
        fr["due_date"] = fr["invoice_date"] + timedelta(days=30)
        fr["extras"] = fr_extras
        fr["printed_total"] = _total(fr_items) + (Decimal("100.00") if "total_mismatch" in planted else 0)

        ship_docs = [("commercial_invoice", ci, supplier, f"Commercial invoice {ci['invoice_number']} / {po}")]
        if "missing_bl" not in planted:
            ship_docs.append(("bill_of_lading", bol, carrier, f"B/L {bl} - shipped on board"))
        ship_docs.append(("freight_invoice", fr, forwarder, f"Invoice {fr['invoice_number']} - B/L {bl}"))
        if rng.random() < 0.4:  # some shipments also get a trucking invoice
            tr_items = _line_items_trucking(rng, n_cont)
            tr_extras = _extras(extra_rng, TRUCKER_EXTRAS, n_cont, 2) if accessorials and i % 2 == 0 else []
            tr_items = tr_items + [{"description": x["description"], "amount": x["amount"]} for x in tr_extras]
            tr = {"vendor": TRUCKER, "invoice_number": f"MD{rng.randint(20000, 29999)}", "currency": "USD",
                  "invoice_date": ship_day + timedelta(days=rng.randint(24, 32)), "bl_number": bl,
                  "container_numbers": containers, "po_numbers": [po], "line_items": tr_items}
            tr["due_date"] = tr["invoice_date"] + timedelta(days=15)
            tr["printed_total"] = _total(tr_items)
            tr["extras"] = tr_extras
            ship_docs.append(("freight_invoice", tr, TRUCKER, f"Drayage invoice {tr['invoice_number']}"))
        if "duplicate_invoice" in planted:
            dup = dict(fr, copy=True)
            ship_docs.append(("freight_invoice_copy", dup, forwarder, f"RE: Invoice {fr['invoice_number']} - B/L {bl} (resent)"))

        shipment_errors = []
        for n, (dtype, d, sender, subject) in enumerate(ship_docs):
            real_type = "freight_invoice" if dtype == "freight_invoice_copy" else dtype
            variant = rng.randint(0, 2)
            pdf = RENDERERS[real_type](d, variant)
            fname = f"{sid}_{n + 1}_{real_type}{'_copy' if dtype.endswith('copy') else ''}.pdf"
            errs = []
            if real_type == "freight_invoice" and sender is forwarder:
                if dtype.endswith("copy"):
                    errs.append("duplicate_invoice")
                else:
                    errs += [e for e in ("total_mismatch", "invalid_container", "container_not_on_bl", "amount_outlier") if e in planted]
            gd = GenDoc(fname, real_type, sid, _gt_fields(real_type, d), errs, sender, subject)
            gd.fields["layout_variant"] = variant
            if d.get("extras"):
                gd.planted_charges = [{"code": x["code"], "description": x["description"], "amount": f"{x['amount']:.2f}"}
                                      for x in d["extras"]]
            docs.append(gd)
            (out / "pdf" / fname).write_bytes(pdf)
        if "missing_bl" in planted:
            shipment_errors.append("missing_bl")
        shipments_meta.append({"shipment_id": sid, "bl_number": bl, "po_numbers": [po], "container_numbers": containers,
                               "planted_errors": shipment_errors + sorted(planted - {"missing_bl"})})

    # Optional scanned copies (image-only) of a few clean documents.
    clean = [d for d in docs if not d.planted_errors]
    for d in rng.sample(clean, min(scanned, len(clean))):
        path = out / "pdf" / d.file
        path.write_bytes(make_scanned(path.read_bytes(), rng))
        d.scanned = True

    # Simulated email arrival, shuffled (documents of one shipment arrive out of order).
    rng.shuffle(docs)
    start = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    emails = []
    for n, d in enumerate(docs):
        emails.append({"file": d.file, "from": f"billing@{d.sender.domain}", "subject": d.subject,
                       "message_id": f"<synthetic-{seed}-{n}@{d.sender.domain}>",
                       "received_at": (start + timedelta(minutes=37 * n)).isoformat()})

    truth = {
        "seed": seed, "generated_at": datetime.now(timezone.utc).isoformat(),
        "documents": [{"file": d.file, "doc_type": d.doc_type, "shipment_id": d.shipment_id, "scanned": d.scanned,
                       "planted_errors": d.planted_errors, "fields": d.fields,
                       **({"planted_charges": d.planted_charges} if d.planted_charges else {})}
                      for d in sorted(docs, key=lambda x: x.file)],
        "shipments": shipments_meta,
    }
    (out / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    (out / "emails.json").write_text(json.dumps(emails, indent=2))
    return {"documents": len(docs), "shipments": n_shipments, "scanned": scanned, "out": str(out)}
