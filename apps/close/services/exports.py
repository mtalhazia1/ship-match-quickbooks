"""Accrual exports: a CSV of every line, and an Excel journal entry template with its reversing entry.

Both are written from a report dict (live, or a locked version exactly as stored), so what auditors
download for a locked period is what was booked.

The journal entry debits each line's account (the vendor's account where set, else freight expense or
inventory) per vendor and account, and credits the accrued liabilities account with the total. Credit
notes reduce the debit; a net credit for an account and vendor is shown in the credit column. The
reversing entry is the same entry with debits and credits swapped, dated the first day of the next
period, so the accrual disappears when the real bills are posted.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from apps.core import csvsafe

from .accruals import KIND_LABELS, counted

CSV_HEADER = ["Period end", "Version", "Type", "Shipment", "B/L", "Shipped", "Vendor", "Charges", "Document",
              "Invoice or credit note", "Invoice date", "Currency", "Amount", "Amount (home currency)",
              "Home currency", "Method", "Confidence", "Basis", "Account", "Account ID", "Status"]


def _d(value) -> Decimal | None:
    return Decimal(str(value)) if value not in (None, "") else None


def csv_bytes(report: dict, version: int | None = None) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_HEADER)
    for ln in report["lines"]:
        w.writerow(csvsafe.row([
            report["period_end"], version or "live", KIND_LABELS.get(ln["kind"], ln["kind"]), ln["shipment_ref"],
            ln["bl_number"], ln["ship_date"], ln["vendor_name"], ln["group_label"], ln["document_name"],
            ln["invoice_number"], ln["invoice_date"], ln["currency"], ln["amount"] or "", ln["amount_home"] or "",
            report["currency"], ln["method_label"], ln["confidence_label"], ln["basis"], ln["account_name"],
            ln["account_id"], ln["status"],
        ]))
    t = report["totals"]
    w.writerow([])
    w.writerow(csvsafe.row(["Total", "", "", "", "", "", "", "", "", "", "", "", "", t["total"], report["currency"]]))
    return out.getvalue().encode("utf-8-sig")


def journal_lines(report: dict) -> tuple[list[dict], Decimal]:
    """Debit lines per (account, vendor) and the credit total."""
    sums: dict[tuple, Decimal] = defaultdict(lambda: Decimal("0.00"))
    meta: dict[tuple, dict] = {}
    for ln in counted(report["lines"]):
        key = (ln["account_name"], ln["account_id"], ln["vendor_name"])
        sums[key] += Decimal(ln["amount_home"])
        meta.setdefault(key, {"shipments": set(), "estimated": False, "goods": True})
        if ln["shipment_ref"]:
            meta[key]["shipments"].add(ln["shipment_ref"])
        meta[key]["estimated"] |= ln["kind"] == "estimate"
        meta[key]["goods"] &= ln["group"] == "goods"
    rows = []
    for (account, account_id, vendor), amount in sorted(sums.items(), key=lambda kv: (kv[0][0], kv[0][2])):
        if amount == 0:
            continue
        ships = sorted(meta[(account, account_id, vendor)]["shipments"])
        m = meta[(account, account_id, vendor)]
        what = "estimated and received" if m["estimated"] else "received, not posted"
        label = "Goods accrual" if m["goods"] else "Freight accrual"
        rows.append({"account": account, "account_id": account_id, "vendor": vendor, "amount": amount,
                     "description": f"{label} ({what}): {', '.join(ships[:12])}"
                                    + (f" and {len(ships) - 12} more" if len(ships) > 12 else "")})
    return rows, sum((r["amount"] for r in rows), Decimal("0.00"))


def journal_xlsx(report: dict, org_name: str, credit_account: str, credit_account_id: str = "",
                 version: int | None = None, locked_by: str = "", locked_at: str = "", checksum: str = "") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    period = date.fromisoformat(report["period_end"])
    reverse_on = period + timedelta(days=1)
    cur = report["currency"]
    tag = f"v{version}" if version else "draft"
    number = f"ACCR-{period:%Y-%m}-{tag}"
    rows, total = journal_lines(report)
    memo = (f"Freight accrual for the period ending {period.day} {period:%b %Y}: shipments not yet invoiced and invoices "
            f"received but not posted. Reverse on {reverse_on.day} {reverse_on:%b %Y}.")
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="EEF2F7")
    money_fmt = "#,##0.00"

    wb = Workbook()

    def entry_sheet(ws, title: str, journal_no: str, day: date, reverse: bool):
        """One journal entry, header in row 1 and nothing else, so the sheet can be imported as it is."""
        ws.title = title
        header = ["Journal No", "Journal Date", "Account", "Account ID", "Debits", "Credits", "Description", "Name",
                  "Currency", "Memo"]
        ws.append(header)
        for c in range(1, len(header) + 1):
            ws.cell(row=1, column=c).font = bold
            ws.cell(row=1, column=c).fill = head_fill
        line_memo = memo if not reverse else f"Reverses {number}: the real bills replace the accrual."
        for r in rows:
            debit, credit = (r["amount"], None) if r["amount"] > 0 else (None, -r["amount"])
            if reverse:
                debit, credit = credit, debit
            ws.append(csvsafe.row([journal_no, day, r["account"], r["account_id"],
                                   float(debit) if debit is not None else None,
                                   float(credit) if credit is not None else None,
                                   r["description"], r["vendor"], cur, line_memo]))
        liab_debit, liab_credit = (None, total) if total >= 0 else (-total, None)
        if reverse:
            liab_debit, liab_credit = liab_credit, liab_debit
        ws.append(csvsafe.row([journal_no, day, credit_account, credit_account_id,
                               float(liab_debit) if liab_debit is not None else None,
                               float(liab_credit) if liab_credit is not None else None,
                               "Accrued freight payable" + (" (reversal)" if reverse else ""), "", cur, line_memo]))
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            row[1].number_format = "yyyy-mm-dd"
            row[4].number_format = row[5].number_format = money_fmt
        for col, width in zip(range(1, 11), (20, 13, 30, 12, 14, 14, 60, 34, 9, 40)):
            ws.column_dimensions[get_column_letter(col)].width = width
        ws.freeze_panes = "A2"

    entry_sheet(wb.active, "Journal entry", number, period, reverse=False)
    entry_sheet(wb.create_sheet(), "Reversing entry", f"{number}-R", reverse_on, reverse=True)

    ws = wb.create_sheet("Lines")
    ws.append(CSV_HEADER)
    for c in range(1, len(CSV_HEADER) + 1):
        ws.cell(row=1, column=c).font = bold
        ws.cell(row=1, column=c).fill = head_fill
    for ln in report["lines"]:
        amount, home = _d(ln["amount"]), _d(ln["amount_home"])
        ws.append(csvsafe.row([
            report["period_end"], version or "live", KIND_LABELS.get(ln["kind"], ln["kind"]), ln["shipment_ref"],
            ln["bl_number"], ln["ship_date"], ln["vendor_name"], ln["group_label"], ln["document_name"],
            ln["invoice_number"], ln["invoice_date"], ln["currency"], float(amount) if amount is not None else None,
            float(home) if home is not None else None, cur, ln["method_label"], ln["confidence_label"], ln["basis"],
            ln["account_name"], ln["account_id"], ln["status"]]))
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        row[12].number_format = row[13].number_format = money_fmt
    for col, width in zip(range(1, len(CSV_HEADER) + 1),
                          (11, 8, 20, 12, 16, 11, 30, 26, 28, 18, 12, 9, 12, 14, 9, 26, 12, 70, 24, 11, 34)):
        ws.column_dimensions[get_column_letter(col)].width = width

    ws = wb.create_sheet("Notes")
    t = report["totals"]
    notes = [
        ("Organization", org_name),
        ("Period end", period),
        ("Journal number", number),
        ("Version", f"Version {version}, locked" if version else "Not locked: a live preview"),
        ("Locked by", locked_by or ""),
        ("Locked at", locked_at or ""),
        ("Report fingerprint (SHA-256)", checksum or ""),
        ("Total accrued", float(Decimal(t["total"]))),
        ("Journal entry check", f"Debits {total:,.2f} = credits {total:,.2f} {cur}" if rows else "No lines to book"),
        ("Received, not booked", float(Decimal(t["received"]))),
        ("Not yet invoiced (estimated)", float(Decimal(t["estimated"]))),
        ("Lines not in the total", t["not_counted"]),
        ("Reversing entry", f"Post the reversing entry on {reverse_on.day} {reverse_on:%b %Y} (sheet 'Reversing entry'), so the "
                            "accrual is replaced by the real bills as they are posted."),
        ("How amounts were found", "Received invoices at their own amount; missing charges from the quote on file, "
                                   "else the vendor's median on the lane, else the organization's median per "
                                   "container. Each line's method, basis and confidence are on the 'Lines' sheet."),
    ]
    for label, value in notes:
        ws.append(csvsafe.row([label, value]))
        ws.cell(row=ws.max_row, column=1).font = bold
    for warning in report.get("warnings") or []:
        ws.append(csvsafe.row(["Warning", warning]))
        ws.cell(row=ws.max_row, column=1).font = bold
    ws["B2"].number_format = "yyyy-mm-dd"
    for r in (8, 10, 11):
        ws.cell(row=r, column=2).number_format = money_fmt
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 110
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
