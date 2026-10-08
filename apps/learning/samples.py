"""A fictional customs broker whose invoices the rule reader gets wrong until a reviewer corrects one.

Its invoice number sits after "Ref No." (the rules look for "Invoice No.") and its dates are printed
day first (03/08/2026 is 3 August). Used by `reset_demo` to show learning in the demo, and by tests.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from synthetic.generator import CONSIGNEE, Page

VENDOR = "Coastline Customs Brokers Ltd."
ADDRESS = "12 Quay Street, Felixstowe IP11 3SY, United Kingdom"


def broker_invoice_pdf(ref: str, invoice_date: date, bl_number: str = "", containers: list[str] | None = None,
                       po_numbers: list[str] | None = None, charges: list[tuple[str, Decimal]] | None = None) -> bytes:
    charges = charges or [("Customs Clearance", Decimal("195.00")), ("Documentation Fee", Decimal("65.00")),
                          ("Port Security Fee", Decimal("40.00"))]
    total = sum((amount for _, amount in charges), Decimal("0.00"))
    p = Page(0)
    p.line((50, VENDOR), (545, "FREIGHT INVOICE", "r"), size=13, bold=True, step=16)
    p.line((50, ADDRESS), size=9)
    p.gap()
    p.line((50, "Ref No.:"), (170, ref))
    p.line((50, "Invoice Date:"), (170, invoice_date.strftime("%d/%m/%Y")))
    p.line((50, "Payment Terms:"), (170, "30 days"))
    if bl_number:
        p.line((50, "B/L No.:"), (170, bl_number))
    if containers:
        p.line((50, "Container(s):"), (170, ", ".join(containers)))
    if po_numbers:
        p.line((50, "Customer Ref / PO:"), (170, ", ".join(po_numbers)))
    p.line((50, "Bill To:"), (170, CONSIGNEE.name))
    p.gap(14)
    p.line((50, "Charge Description"), (545, "Amount (USD)", "r"), bold=True)
    p.rule()
    for desc, amount in charges:
        p.line((50, desc), (545, f"{amount:,.2f}", "r"))
    p.rule()
    p.line((330, "Amount Due:"), (545, f"{total:,.2f}", "r"), bold=True)
    p.gap(30)
    p.line((50, f"Please remit to {VENDOR} within 30 days."), size=8)
    return p.pdf()


def broker_pair(first_day: date = date(2026, 8, 3)) -> list[tuple[str, str, date, bytes]]:
    """Two invoices from the broker: (file name, ref, invoice date, pdf). Both dates are ambiguous (day <= 12)."""
    second_day = first_day + timedelta(days=32)
    out = []
    for ref, day in (("CCB/24117", first_day), ("CCB/24206", second_day)):
        name = f"coastline-{ref.split('/')[1]}.pdf"
        out.append((name, ref, day, broker_invoice_pdf(ref, day)))
    return out
