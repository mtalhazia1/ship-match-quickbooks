"""Opt-in synthetic documents for landed cost and shared invoices.

  * a commercial invoice whose product lines carry a SKU, HS code, weight and volume (printed on a detail
    line under each product, as many suppliers do);
  * a forwarder invoice that covers two or three shipments, with lines that name each B/L
    (`attributed=True`) or only totals (`attributed=False`);
  * `scenario()`: the documents of three shipments plus the shared invoice, in arrival order, for tests
    and `python manage.py seed_landed`.

Nothing here is used by `generator.generate`, so the default dataset doesn't change.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from apps.shipments.services.containers import make_container
from synthetic.generator import (
    CARRIERS,
    CONSIGNEE,
    FORWARDERS,
    SUPPLIERS,
    Page,
    Party,
    fmt_money,
    money,
    render_bill_of_lading,
    render_freight_invoice,
)

APEX = SUPPLIERS[1]
HARBORLINK = FORWARDERS[0]

# SKU, description, HS code, unit price, kg per unit, cbm per unit
CATALOG = [
    ("APX-CW10", "Stainless cookware set 10pc", "7323.93", Decimal("42.00"), Decimal("6.200"), Decimal("0.0310")),
    ("APX-GC03", "Glass food container 3pc", "7013.49", Decimal("6.80"), Decimal("1.450"), Decimal("0.0060")),
    ("APX-SU05", "Silicone utensil set", "3924.10", Decimal("4.35"), Decimal("0.420"), Decimal("0.0021")),
    ("APX-CM35", "Ceramic mug 350ml", "6912.00", Decimal("1.90"), Decimal("0.380"), Decimal("0.0012")),
]


@dataclass
class GoodsLine:
    sku: str
    description: str
    hs_code: str
    quantity: int
    unit_price: Decimal
    weight_kg: Decimal | None = None
    volume_cbm: Decimal | None = None

    @property
    def amount(self) -> Decimal:
        return money(self.unit_price * self.quantity)


def goods(quantities: dict[str, int], price_change: Decimal = Decimal("0"), weights: bool = True) -> list[GoodsLine]:
    """Catalog lines for {sku: quantity}; price_change 0.05 = 5% dearer."""
    out = []
    for sku, desc, hs, price, kg, cbm in CATALOG:
        if sku in quantities:
            q = quantities[sku]
            out.append(GoodsLine(sku, desc, hs, q, money(price * (1 + price_change)),
                                 (kg * q).quantize(Decimal("0.1")) if weights else None,
                                 (cbm * q).quantize(Decimal("0.01")) if weights else None))
    return out


def commercial_invoice(number: str, po: str, containers: list[str], lines: list[GoodsLine], *,
                       vendor: Party = APEX, currency: str = "USD", day: date = date(2026, 4, 1),
                       details: bool = True) -> tuple[bytes, dict]:
    """A supplier invoice; details=True prints "SKU ... | HS ... | ... kg | ... cbm" under each product."""
    p = Page(0)
    p.line((50, vendor.name), (545, "COMMERCIAL INVOICE", "r"), size=13, bold=True, step=16)
    p.line((50, vendor.address), size=9)
    p.gap()
    p.line((50, "Invoice No.:"), (160, number))
    p.line((50, "Date:"), (160, day.isoformat()))
    p.line((50, "PO No.:"), (160, po))
    p.line((50, "Currency:"), (160, currency))
    p.line((50, "Container(s):"), (160, ", ".join(containers)))
    p.gap()
    p.line((50, "Buyer:"), (160, CONSIGNEE.name))
    p.gap(14)
    p.line((50, "Description"), (360, "Qty", "r"), (450, "Unit Price", "r"), (545, "Amount", "r"), bold=True)
    p.rule()
    for li in lines:
        p.line((50, li.description), (360, str(li.quantity), "r"), (450, fmt_money(li.unit_price, 0), "r"),
               (545, fmt_money(li.amount, 0), "r"))
        if details:
            bits = [f"SKU {li.sku}", f"HS {li.hs_code}"]
            if li.weight_kg is not None:
                bits.append(f"{li.weight_kg} kg")
            if li.volume_cbm is not None:
                bits.append(f"{li.volume_cbm} cbm")
            p.line((62, " | ".join(bits)), size=8, step=13)
    p.rule()
    total = sum((li.amount for li in lines), Decimal("0.00"))
    p.line((330, f"TOTAL ({currency}):"), (545, fmt_money(total, 0), "r"), bold=True)
    p.gap(30)
    p.line((50, "Terms: FOB origin. Payment 60 days from B/L date."), size=8)
    truth = {"vendor_name": vendor.name, "invoice_number": number, "invoice_date": day.isoformat(),
             "currency": currency, "po_numbers": [po], "container_numbers": containers,
             "total_amount": f"{total:.2f}",
             "line_items": [{"description": li.description, "quantity": str(li.quantity),
                             "unit_price": f"{li.unit_price:.2f}", "amount": f"{li.amount:.2f}",
                             **({"sku": li.sku, "hs_code": li.hs_code} if details else {}),
                             **({"weight_kg": str(li.weight_kg)} if details and li.weight_kg is not None else {}),
                             **({"volume_cbm": str(li.volume_cbm)} if details and li.volume_cbm is not None else {})}
                            for li in lines]}
    return p.pdf(), truth


def bill_of_lading(bl: str, containers: list[str], po: str, *, day: date = date(2026, 3, 20),
                   shipper: Party = APEX, seed: int = 1) -> tuple[bytes, dict]:
    carrier = CARRIERS[0][0]
    d = {"carrier": carrier, "bl_number": bl, "issue_date": day, "shipper": shipper, "container_numbers": containers,
         "seals": [f"SL{700000 + i}" for i in range(len(containers))], "po_numbers": [po],
         "vessel_voyage": "MV Coral Dawn / 512E", "port_of_loading": "Ningbo", "port_of_discharge": "Long Beach, CA",
         "rng": random.Random(seed)}
    return render_bill_of_lading(d, 0), {"bl_number": bl, "container_numbers": containers, "po_numbers": [po]}


@dataclass
class Leg:
    """One shipment on a shared invoice."""
    bl: str
    containers: list[str]
    po: str = ""
    freight_per_box: Decimal = Decimal("2150.00")
    thc_per_box: Decimal = Decimal("310.00")
    extras: list[tuple[str, Decimal]] = field(default_factory=list)


def shared_freight_invoice(number: str, legs: list[Leg], *, attributed: bool = True, vendor: Party = HARBORLINK,
                           day: date = date(2026, 4, 18), currency: str = "USD",
                           shared_charges: list[tuple[str, str]] | None = None) -> tuple[bytes, dict]:
    """A forwarder invoice for several B/Ls. The first leg's B/L is printed as "B/L No." (so the invoice is
    matched to that shipment); the others are listed under "Also covers". attributed=True: each freight
    and handling line names its B/L; False: lines are totals over every container."""
    shared_charges = shared_charges if shared_charges is not None else [("Documentation Fee", "75.00"),
                                                                       ("Customs Clearance", "175.00")]
    items = []
    if attributed:
        for leg in legs:
            n = len(leg.containers)
            items.append({"description": f"Ocean Freight B/L {leg.bl} {n} x 40HC",
                          "amount": money(leg.freight_per_box * n), "bl": leg.bl})
            items.append({"description": f"Terminal Handling B/L {leg.bl}", "amount": money(leg.thc_per_box * n),
                          "bl": leg.bl})
            for desc, amount in leg.extras:
                items.append({"description": f"{desc} B/L {leg.bl}", "amount": money(amount), "bl": leg.bl})
    else:
        boxes = sum(len(leg.containers) for leg in legs)
        items.append({"description": f"Ocean Freight {boxes} x 40HC",
                      "amount": money(sum((leg.freight_per_box * len(leg.containers) for leg in legs), Decimal(0)))})
        items.append({"description": f"Terminal Handling Charge (THC) {boxes} x 40HC",
                      "amount": money(sum((leg.thc_per_box * len(leg.containers) for leg in legs), Decimal(0)))})
        for leg in legs:
            for desc, amount in leg.extras:
                items.append({"description": desc, "amount": money(amount)})
    for desc, amount in shared_charges:
        items.append({"description": desc, "amount": money(amount)})
    total = sum((i["amount"] for i in items), Decimal("0.00"))
    containers = [c for leg in legs for c in leg.containers]
    d = {"vendor": vendor, "invoice_number": number, "invoice_date": day, "due_date": day + timedelta(days=30),
         "bl_number": legs[0].bl, "container_numbers": containers, "po_numbers": [legs[0].po] if legs[0].po else [],
         "currency": currency, "line_items": items, "printed_total": total}
    pdf = _with_other_bls(d, [leg.bl for leg in legs[1:]])
    truth = {"vendor_name": vendor.name, "invoice_number": number, "invoice_date": day.isoformat(),
             "bl_number": legs[0].bl, "container_numbers": containers, "currency": currency,
             "total_amount": f"{total:.2f}",
             "line_items": [{"description": i["description"], "amount": f"{i['amount']:.2f}"} for i in items],
             "legs": [{"bl": leg.bl, "containers": leg.containers} for leg in legs]}
    return pdf, truth


def _with_other_bls(d: dict, others: list[str]) -> bytes:
    """The generator's first freight invoice layout, with an "Also covers B/L" line under the B/L number."""
    if not others:
        return render_freight_invoice(d, 0)
    p = Page(0)
    ven = d["vendor"]
    p.line((50, ven.name), (545, "FREIGHT INVOICE", "r"), size=13, bold=True, step=16)
    p.line((50, ven.address), size=9)
    p.gap()
    p.line((50, "Invoice No.:"), (170, d["invoice_number"]))
    p.line((50, "Invoice Date:"), (170, d["invoice_date"].isoformat()))
    p.line((50, "Due Date:"), (170, d["due_date"].isoformat()))
    p.line((50, "B/L No.:"), (170, d["bl_number"]))
    p.line((50, "Also covers B/L:"), (170, ", ".join(others)))
    p.line((50, "Container(s):"), (170, ", ".join(d["container_numbers"])))
    if d["po_numbers"]:
        p.line((50, "Customer Ref / PO:"), (170, ", ".join(d["po_numbers"])))
    p.line((50, "Bill To:"), (170, CONSIGNEE.name))
    p.gap(14)
    p.line((50, "Charge Description"), (545, f"Amount ({d['currency']})", "r"), bold=True)
    p.rule()
    for li in d["line_items"]:
        p.line((50, li["description"]), (545, fmt_money(li["amount"], 0), "r"))
    p.rule()
    p.line((330, "TOTAL DUE:"), (545, fmt_money(d["printed_total"], 0), "r"), bold=True)
    p.gap(30)
    p.line((50, f"Please remit to {ven.name}. Reference our invoice number on payment."), size=8)
    return p.pdf()


@dataclass
class ScenarioShipment:
    bl: str
    containers: list[str]
    po: str
    ci_number: str
    lines: list[GoodsLine]
    day: date


def scenario(seed: int = 7, attributed: bool = True, legs: int = 3) -> dict:
    """Three shipments from one supplier (commercial invoice with SKUs and weights, B/L) and one forwarder
    invoice that covers them all. Returns {"files": [(name, bytes)], "shipments": [...], "invoice": truth};
    the shared invoice is last, as it usually arrives after the B/Ls."""
    rng = random.Random(seed)
    step = (seed - 7) % 8                 # later rounds: about a month apart, dearer goods and freight
    base = date(2026, 1, 5) + timedelta(days=35 * step)
    plans = [
        {"APX-CW10": 400, "APX-GC03": 1200, "APX-SU05": 800},
        {"APX-CW10": 250, "APX-CM35": 2400},
        {"APX-GC03": 900, "APX-SU05": 600, "APX-CM35": 1200},
    ][:legs]
    ships, files = [], []
    for i, plan in enumerate(plans):
        boxes = [make_container("OSL", rng.randint(100000, 999999)) for _ in range(1 + (i % 2))]
        bl = f"OSLN{rng.randint(10**9, 10**10 - 1)}"   # random: similar numbers would be near-matches
        po = f"PO-2026-{5100 + seed * 10 + i}"
        day = base + timedelta(days=9 * i)
        lines = goods(plan, price_change=Decimal(i) / 50 + Decimal(step % 5) / 100)
        ci_number = f"CI-{60000 + seed * 100 + i}"
        ships.append(ScenarioShipment(bl, boxes, po, ci_number, lines, day))
        files.append((f"L{seed}_{i + 1}_bill_of_lading.pdf", bill_of_lading(bl, boxes, po, day=day + timedelta(days=3),
                                                                           seed=seed + i)[0]))
        files.append((f"L{seed}_{i + 1}_commercial_invoice.pdf",
                      commercial_invoice(ci_number, po, boxes, lines, day=day)[0]))
    extras = [[], [("Demurrage 3 days", Decimal("525.00"))], []]
    freight = Decimal("2150.00") + 60 * (step % 4)
    inv_legs = [Leg(s.bl, s.containers, s.po if n == 0 else "", freight_per_box=freight,
                    extras=extras[n] if attributed else []) for n, s in enumerate(ships)]
    pdf, truth = shared_freight_invoice(f"HL-{880000 + seed * 10 + rng.randint(0, 9)}", inv_legs,
                                        attributed=attributed, day=base + timedelta(days=40))
    files.append((f"L{seed}_shared_freight_invoice.pdf", pdf))
    return {"files": files, "shipments": ships, "invoice": truth}
