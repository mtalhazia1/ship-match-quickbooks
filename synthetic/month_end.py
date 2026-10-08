"""Opt-in synthetic inputs for month-end close: a vendor statement of account (PDF, XLSX, CSV) that disagrees
with ShipMatch in known ways, and shipments that have shipped but aren't (fully) invoiced yet.

Nothing here runs when the default dataset is generated: `generator.generate` never calls these helpers, so its
output is unchanged. Tests and `manage.py close_demo` use them.

A statement scenario starts from the vendor's invoices ShipMatch has (from a dataset's ground truth, or from
the database) and plants, on purpose:
  * one missing invoice: on the statement, never sent to ShipMatch;
  * one amount difference: an invoice listed for more than its printed total;
  * one unapplied credit: a credit note ShipMatch receives (its PDF is returned too) that the statement
    doesn't include.
Every other invoice is listed exactly as printed. The `expected` key says which bucket each planted line
belongs to.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from synthetic.generator import (
    CARRIERS,
    CONSIGNEE,
    FORWARDERS,
    SUPPLIERS,
    Party,
    money,
    render_bill_of_lading,
    render_commercial_invoice,
    render_freight_invoice,
)

HARBORLINK = FORWARDERS[0]
D = Decimal


@dataclass
class StatementLine:
    day: date
    kind: str            # Invoice | Credit note | Payment
    number: str
    reference: str
    amount: Decimal      # signed: invoices +, credits and payments -
    balance: Decimal = D("0.00")


def invoices_from_ground_truth(dataset_dir: str | Path, vendor: str = HARBORLINK.name) -> list[dict]:
    """The vendor's freight invoices in a generated dataset, without the planted resent copies."""
    truth = json.loads((Path(dataset_dir) / "ground_truth.json").read_text())
    out = []
    for d in truth["documents"]:
        f = d["fields"]
        if d["doc_type"] != "freight_invoice" or f.get("vendor_name") != vendor or "duplicate_invoice" in d[
                "planted_errors"]:
            continue
        out.append({"invoice_number": f["invoice_number"], "invoice_date": f["invoice_date"],
                    "bl_number": f.get("bl_number") or "", "total_amount": f["total_amount"],
                    "container_numbers": f.get("container_numbers") or []})
    return sorted(out, key=lambda i: (i["invoice_date"], i["invoice_number"]))


def scenario(invoices: list[dict], *, vendor: Party = HARBORLINK, statement_date: date | None = None,
             missing_number: str = "HA-990417", missing_amount: str = "485.00", differs_by: str = "125.00",
             credit_number: str = "CN-77031", credit_amount: str = "120.00", currency: str = "USD") -> dict:
    """A statement of account for `vendor` built from the invoices ShipMatch has (dicts with invoice_number,
    invoice_date, bl_number, total_amount), with one missing invoice, one amount difference and one credit note
    the vendor hasn't applied."""
    if len(invoices) < 2:
        raise ValueError("A statement scenario needs at least two invoices from the vendor.")
    invoices = sorted(invoices, key=lambda i: (i["invoice_date"], i["invoice_number"]))
    last = date.fromisoformat(invoices[-1]["invoice_date"])
    statement_date = statement_date or last + timedelta(days=5)
    differs = invoices[1]
    credited = invoices[0]
    lines: list[StatementLine] = []
    for inv in invoices:
        amount = money(inv["total_amount"])
        if inv is differs:
            amount += money(differs_by)
        lines.append(StatementLine(date.fromisoformat(inv["invoice_date"]), "Invoice", inv["invoice_number"],
                                   inv.get("bl_number") or "", amount))
    missing_day = statement_date - timedelta(days=3)
    lines.append(StatementLine(missing_day, "Invoice", missing_number, invoices[-1].get("bl_number") or "",
                               money(missing_amount)))
    lines.sort(key=lambda ln: (ln.day, ln.number))
    running = D("0.00")
    for ln in lines:
        running += ln.amount
        ln.balance = running
    credit_day = date.fromisoformat(credited["invoice_date"]) + timedelta(days=12)
    credit_pdf, credit_truth = _credit_note(credit_number, credited, vendor, credit_day, credit_amount, currency)
    return {
        "vendor": vendor, "statement_date": statement_date, "currency": currency, "lines": lines,
        "closing_balance": running,
        "credit_note": (credit_pdf, credit_truth),
        "expected": {
            "missing": [missing_number], "amount_differs": [differs["invoice_number"]],
            "credit_not_applied": [credit_number],
            "matched": [i["invoice_number"] for i in invoices if i is not differs],
            "difference": f"{money(missing_amount) + money(differs_by) + money(credit_amount):.2f}",
        },
    }


def _credit_note(number: str, invoice: dict, vendor: Party, day: date, amount: str, currency: str):
    from synthetic.extra import credit_note

    return credit_note(number, invoice["invoice_number"], vendor=vendor, bl=invoice.get("bl_number") or None,
                       containers=invoice.get("container_numbers") or None, day=day, currency=currency,
                       charges=[("Terminal Handling Charge (THC) overcharge", amount)])


def _fmt(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def statement_pdf(s: dict) -> bytes:
    """The statement as a one-or-more page PDF with a text layer: letterhead, header block, a table of
    date, type, number, reference, amount and running balance, and the balance due."""
    vendor: Party = s["vendor"]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    page = 1

    def header(first: bool) -> float:
        y = 800
        c.setFont("Helvetica-Bold", 13)
        c.drawString(50, y, vendor.name)
        c.drawRightString(545, y, "STATEMENT OF ACCOUNT")
        y -= 16
        c.setFont("Helvetica", 9)
        c.drawString(50, y, vendor.address)
        y -= 24
        if first:
            c.setFont("Helvetica", 10)
            for label, value in (("Statement date:", s["statement_date"].isoformat()), ("Account:", CONSIGNEE.name),
                                 ("Currency:", s["currency"])):
                c.drawString(50, y, label)
                c.drawString(160, y, value)
                y -= 15
            y -= 10
        c.setFont("Helvetica-Bold", 10)
        for x, text, right in ((50, "Date", False), (120, "Type", False), (200, "Invoice No.", False),
                               (300, "Reference", False), (470, "Amount", True), (545, "Balance", True)):
            (c.drawRightString if right else c.drawString)(x, y, text)
        return y - 16

    y = header(True)
    c.setFont("Helvetica", 10)
    for ln in s["lines"]:
        if y < 90:
            c.setFont("Helvetica", 8)
            c.drawRightString(545, 30, f"Page {page}")
            c.showPage()
            page += 1
            y = header(False)
            c.setFont("Helvetica", 10)
        c.drawString(50, y, ln.day.isoformat())
        c.drawString(120, y, ln.kind)
        c.drawString(200, y, ln.number)
        c.drawString(300, y, ln.reference[:24])
        c.drawRightString(470, y, _fmt(ln.amount))
        c.drawRightString(545, y, _fmt(ln.balance))
        y -= 14
    y -= 8
    c.setFont("Helvetica-Bold", 10)
    c.drawString(300, y, f"Balance due ({s['currency']}):")
    c.drawRightString(545, y, _fmt(s["closing_balance"]))
    y -= 30
    c.setFont("Helvetica", 8)
    c.drawString(50, y, "Please check this statement against your records and tell us about any differences.")
    c.drawRightString(545, 30, f"Page {page}")
    c.showPage()
    c.save()
    return buf.getvalue()


def statement_xlsx(s: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    vendor: Party = s["vendor"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Statement"
    ws["A1"] = vendor.name
    ws["A1"].font = Font(bold=True, size=14)
    ws["E1"] = "STATEMENT OF ACCOUNT"
    ws["A2"] = vendor.address
    ws["A4"], ws["B4"] = "Statement date", s["statement_date"]
    ws["A5"], ws["B5"] = "Account", CONSIGNEE.name
    ws["A6"], ws["B6"] = "Currency", s["currency"]
    for col, head in zip("ABCDEF", ["Date", "Type", "Invoice No.", "Reference", "Amount", "Balance"]):
        ws[f"{col}8"] = head
        ws[f"{col}8"].font = Font(bold=True)
    row = 9
    for ln in s["lines"]:
        ws[f"A{row}"], ws[f"B{row}"], ws[f"C{row}"], ws[f"D{row}"] = ln.day, ln.kind, ln.number, ln.reference
        ws[f"E{row}"], ws[f"F{row}"] = float(ln.amount), float(ln.balance)
        ws[f"E{row}"].number_format = ws[f"F{row}"].number_format = "#,##0.00"
        ws[f"A{row}"].number_format = "yyyy-mm-dd"
        row += 1
    ws[f"E{row + 1}"], ws[f"F{row + 1}"] = "Balance due", float(s["closing_balance"])
    ws[f"F{row + 1}"].number_format = "#,##0.00"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def statement_csv(s: dict) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Vendor", s["vendor"].name])
    w.writerow(["Statement date", s["statement_date"].isoformat()])
    w.writerow(["Currency", s["currency"]])
    w.writerow([])
    w.writerow(["Date", "Type", "Invoice No.", "Reference", "Amount", "Balance"])
    for ln in s["lines"]:
        w.writerow([ln.day.isoformat(), ln.kind, ln.number, ln.reference, f"{ln.amount:.2f}", f"{ln.balance:.2f}"])
    w.writerow(["", "", "", "Balance due", "", f"{s['closing_balance']:.2f}"])
    return out.getvalue().encode("utf-8")


# --------------------------------------------------------------------------- shipments not (fully) invoiced


def arrived_not_invoiced(ship_day: date, *, seed_number: int = 1) -> list[tuple[str, bytes, dict]]:
    """Two shipments that shipped on `ship_day` and are not fully billed, as (filename, PDF, truth):

    * one with only its bill of lading and commercial invoice (no freight invoice yet);
    * one whose forwarder billed the ocean freight only (destination charges and delivery still to come).
    """
    from apps.shipments.services.containers import make_container

    out = []
    for n, partial in enumerate((False, True)):
        supplier = SUPPLIERS[(seed_number + n) % len(SUPPLIERS)]
        carrier, owner, prefix = CARRIERS[(seed_number + n) % len(CARRIERS)]
        forwarder = FORWARDERS[0]
        containers = [make_container(owner, 410000 + seed_number * 37 + n * 11 + i) for i in range(2)]
        bl = f"{prefix}{8800000000 + seed_number * 100 + n}"
        po = f"PO-2026-{8800 + seed_number * 10 + n}"
        day = ship_day - timedelta(days=n * 2)
        tag = f"ME{seed_number:02d}{n + 1}"
        items = [{"description": "Cotton bath towel 70x140", "quantity": 800, "unit_price": money("5.60"),
                  "amount": money("4480.00")},
                 {"description": "Bed sheet set queen", "quantity": 300, "unit_price": money("17.90"),
                  "amount": money("5370.00")}]
        ci = {"vendor": supplier, "invoice_number": f"CI-{tag}", "invoice_date": day - timedelta(days=3),
              "po_numbers": [po], "currency": "USD", "container_numbers": containers, "line_items": items,
              "printed_total": sum((i["amount"] for i in items), D("0.00"))}
        bol = {"carrier": carrier, "bl_number": bl, "issue_date": day, "shipper": supplier,
               "container_numbers": containers, "seals": [f"SL9{n}{i}2026" for i in range(2)], "po_numbers": [po],
               "vessel_voyage": "MV Silver Tide / 418E", "port_of_loading": "Ningbo",
               "port_of_discharge": "Long Beach, CA", "rng": _FixedRng()}
        out.append((f"{tag}_commercial_invoice.pdf", render_commercial_invoice(ci, 0), {"bl_number": bl}))
        out.append((f"{tag}_bill_of_lading.pdf", render_bill_of_lading(bol, 0), {"bl_number": bl}))
        if partial:
            fr_items = [{"description": "Ocean Freight", "amount": money("4700.00")},
                        {"description": "Documentation Fee", "amount": money("75.00")}]
            fr = {"vendor": forwarder, "invoice_number": f"HA-{880000 + seed_number * 10 + n}",
                  "invoice_date": day + timedelta(days=4), "due_date": day + timedelta(days=34), "bl_number": bl,
                  "container_numbers": containers, "po_numbers": [po], "currency": "USD", "line_items": fr_items,
                  "printed_total": sum((i["amount"] for i in fr_items), D("0.00"))}
            out.append((f"{tag}_freight_invoice.pdf", render_freight_invoice(fr, 0), {"bl_number": bl}))
    return out


class _FixedRng:
    """The B/L renderer asks for a random gross weight; a fixed one keeps these files identical every run."""

    def randint(self, a, b):
        return (a + b) // 2
