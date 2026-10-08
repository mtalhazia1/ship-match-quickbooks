"""Spreadsheet invoices (.xlsx, .csv).

Three things happen to a spreadsheet:
  * it is turned into a faithful text form, one row per line with cells separated by " | ", which the
    classifier, the AI reader and grounding all use like the text of a PDF;
  * a readable PDF copy is drawn (reportlab) so the review screen and QuickBooks still get a document;
  * the rules provider reads it as a table (`sheet_rules`): it finds the header row, maps the usual
    columns (description, qty, rate, amount, container, B/L, invoice number and date, vendor) and reads
    the label/value cells around the table, so a typical carrier spreadsheet extracts without AI.

Values are shown as Excel shows them (dates as YYYY-MM-DD, amounts with their two decimals). Nothing is
calculated: a total is read from the sheet's own total row or total cell, or left for a reviewer.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from django.conf import settings

from apps.documents.schemas import SCHEMAS
from apps.documents.services.extract_rules import COMPANY_SUFFIX, GUESS_CONF, LABEL_CONF, PATTERN_CONF, RuleResult
from apps.documents.services.ingest import RejectedFile
from apps.documents.services.normalize import parse_date, parse_money
from apps.shipments.services.containers import find_containers

CELL_SEP = " | "
MARKER_RE = re.compile(r"^\[Spreadsheet (?P<name>.+?): (?P<desc>.+)\]$")
XLSX_MAX_UNPACKED = 80 * 1024 * 1024
XLSX_MAX_RATIO = 200


@dataclass
class Sheet:
    name: str
    rows: list[list[str]]
    truncated: bool = False
    header_row: int | None = None  # index into rows, when a line-item header was found


# --------------------------------------------------------------------------- reading files


def load(filename: str, content: bytes, subtype: str) -> list[Sheet]:
    sheets = _load_csv(filename, content) if subtype == "csv" else _load_xlsx(filename, content)
    sheets = [s for s in (_tidy(s) for s in sheets) if s.rows]
    if not sheets:
        raise RejectedFile(f"{filename}: the spreadsheet is empty. Check that the invoice is on a visible sheet.")
    for s in sheets:
        s.header_row = find_header(s.rows)[0]
    return sheets


def _load_xlsx(filename: str, content: bytes) -> list[Sheet]:
    import openpyxl  # uses defusedxml when installed, against XML entity attacks

    _check_packed_size(filename, content)
    max_rows, max_cols = settings.INTAKE_SHEET_MAX_ROWS, settings.INTAKE_SHEET_MAX_COLUMNS
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:  # openpyxl raises many types for damaged files
        raise RejectedFile(f"{filename}: the Excel file can't be opened ({e.__class__.__name__}). "
                           "Open it in Excel, save it again as .xlsx and send that.") from e
    sheets = []
    try:
        for ws in wb.worksheets:
            if getattr(ws, "sheet_state", "visible") != "visible":
                continue
            rows, truncated = [], False
            for n, row in enumerate(ws.iter_rows(), 1):
                if n > max_rows:
                    truncated = True
                    break
                rows.append([display_value(c.value, getattr(c, "number_format", None)) for c in row[:max_cols]])
            sheets.append(Sheet(str(ws.title)[:80], rows, truncated))
    except RejectedFile:
        raise
    except Exception as e:
        raise RejectedFile(f"{filename}: the Excel file can't be read ({e.__class__.__name__}). "
                           "Open it in Excel, save it again as .xlsx and send that.") from e
    finally:
        wb.close()
    return sheets


def _check_packed_size(filename: str, content: bytes) -> None:
    """An .xlsx is a ZIP of XML files: refuse one that unpacks to something huge before parsing it."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            total = 0
            for info in zf.infolist():
                total += info.file_size
                if info.file_size > 1024 * 1024 and info.file_size / max(info.compress_size, 1) > XLSX_MAX_RATIO:
                    raise RejectedFile(f"{filename}: the Excel file is packed in a suspicious way and was not opened.")
            if total > XLSX_MAX_UNPACKED:
                raise RejectedFile(f"{filename}: the Excel file is too large to read ({total // (1024 * 1024)} MB "
                                   "unpacked). Send only the invoice sheet.")
    except zipfile.BadZipFile as e:
        raise RejectedFile(f"{filename}: the Excel file is damaged and can't be opened.") from e


def _decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def _load_csv(filename: str, content: bytes) -> list[Sheet]:
    text = _decode(content)
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows, truncated = [], False
    max_rows, max_cols = settings.INTAKE_SHEET_MAX_ROWS, settings.INTAKE_SHEET_MAX_COLUMNS
    try:
        for n, row in enumerate(csv.reader(io.StringIO(text), dialect), 1):
            if n > max_rows:
                truncated = True
                break
            rows.append([display_value(c) for c in row[:max_cols]])
    except csv.Error as e:
        raise RejectedFile(f"{filename}: the CSV file can't be read ({e}). Export it again from the source system.") from e
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return [Sheet(stem[:80], rows, truncated)]


def display_value(value, number_format: str | None = None) -> str:
    """A cell as a person sees it in Excel."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0, 0) else value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, (int, float, Decimal)):
        fmt = (number_format or "General").lower()
        if "%" in fmt:
            return f"{float(value) * 100:.{_decimals(fmt)}f}%"
        if "0.0" in fmt or "#,##0" in fmt:
            places = _decimals(fmt)
            return f"{value:,.{places}f}" if "#,##" in fmt else f"{value:.{places}f}"
        if float(value).is_integer():
            return str(int(value))
        if isinstance(value, Decimal):
            return str(value)
        shown = f"{value:.10g}"
        return f"{value:f}".rstrip("0").rstrip(".") if "e" in shown else shown
    return re.sub(r"\s+", " ", str(value)).strip()


def _decimals(fmt: str) -> int:
    m = re.search(r"0\.(0+)", fmt)
    return len(m.group(1)) if m else 0


def _tidy(sheet: Sheet) -> Sheet:
    """Drop empty columns and outer empty rows; keep one empty row where a gap separates blocks."""
    rows = [list(r) for r in sheet.rows]
    width = max((len(r) for r in rows), default=0)
    used = [c for c in range(width) if any(c < len(r) and r[c] for r in rows)]
    out: list[list[str]] = []
    for r in rows:
        cells = [r[c] if c < len(r) else "" for c in used]
        while cells and not cells[-1]:
            cells.pop()
        if cells or (out and out[-1]):
            out.append(cells)
    while out and not out[-1]:
        out.pop()
    sheet.rows = out
    return sheet


# --------------------------------------------------------------------------- text form


def _escape(cell: str) -> str:
    return cell.replace("|", "\\|")


def to_text(filename: str, sheets: list[Sheet]) -> str:
    parts = []
    for i, s in enumerate(sheets):
        lines = [] if i == 0 else [f"Sheet: {s.name}"]
        lines += [CELL_SEP.join(_escape(c) for c in row) for row in s.rows]
        if s.truncated:
            lines.append(f"(Only the first {settings.INTAKE_SHEET_MAX_ROWS:,} rows of this sheet were read.)")
        parts.append("\n".join(lines))
    names = ", ".join(s.name for s in sheets)
    desc = f"sheet {names}" if len(sheets) == 1 else f"{len(sheets)} sheets ({names})"
    return "\n\f\n".join(parts) + f"\n[Spreadsheet {filename}: {desc}]"


def is_sheet_text(text: str) -> bool:
    last = (text or "").rstrip().rsplit("\n", 1)[-1]
    return bool(MARKER_RE.match(last.strip()))


def parse_text(text: str) -> list[list[list[str]]]:
    """Rows of cells per sheet, back from the text form."""
    body = text.rstrip().rsplit("\n", 1)[0]
    sheets = []
    for part in body.split("\f"):
        rows = []
        for line in part.strip("\n").split("\n"):
            if line.startswith("Sheet: ") and not rows:
                continue
            if line.startswith("(Only the first "):
                continue
            rows.append([c.replace("\\|", "|").strip() for c in line.split(CELL_SEP)] if line.strip() else [])
        sheets.append(rows)
    return sheets


def spreadsheet_to_pdf(filename: str, content: bytes, subtype: str) -> tuple[bytes, dict]:
    sheets = load(filename, content, subtype)
    pdf = render_pdf(filename, sheets)
    info = {"sheets": [s.name for s in sheets], "rows": sum(len(s.rows) for s in sheets),
            "truncated": any(s.truncated for s in sheets)}
    return pdf, info


def text_for(filename: str, content: bytes, subtype: str) -> str:
    return to_text(filename, load(filename, content, subtype))


# --------------------------------------------------------------------------- PDF copy


def render_pdf(filename: str, sheets: list[Sheet]) -> bytes:
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    page = landscape(A4)
    margin = 12 * mm
    usable = page[0] - 2 * margin
    title = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=11, leading=14)
    note = ParagraphStyle("n", fontName="Helvetica", fontSize=8, leading=10, textColor=colors.HexColor("#555555"))
    story = []
    for i, s in enumerate(sheets):
        if i:
            story.append(PageBreak())
        story.append(Paragraph(escape(f"{filename}, sheet {s.name}"), title))
        story.append(Paragraph("Copy drawn by ShipMatch from the spreadsheet as received. "
                               "The original file can be downloaded from the document page.", note))
        story.append(Spacer(1, 4 * mm))
        width = max(len(r) for r in s.rows)
        rows = [r + [""] * (width - len(r)) for r in s.rows]
        size = 8 if width <= 8 else 7 if width <= 14 else 6
        cell = ParagraphStyle("c", fontName="Helvetica", fontSize=size, leading=size + 2)
        natural = []
        for c in range(width):
            longest = max((stringWidth(r[c], "Helvetica", size) for r in rows[:300]), default=0)
            natural.append(min(max(longest + 8, 28), 220))
        scale = min(1.0, usable / sum(natural))
        col_widths = [w * scale for w in natural]
        data = []
        for r in rows:
            out = []
            for c, value in enumerate(r):
                if stringWidth(value, "Helvetica", size) + 6 > col_widths[c]:
                    out.append(Paragraph(escape(value), cell))  # wraps inside the column
                else:
                    out.append(value)
            data.append(out)
        style = [
            ("FONT", (0, 0), (-1, -1), "Helvetica", size),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c3cdd8")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]
        numeric = [(c, r) for r, row in enumerate(rows) for c, v in enumerate(row) if v and _is_number(v)]
        if len(numeric) <= 20000:  # amounts line up on the right, like in Excel
            style += [("ALIGN", (c, r), (c, r), "RIGHT") for c, r in numeric]
        if s.header_row is not None:
            h = s.header_row
            style += [("FONT", (0, h), (-1, h), "Helvetica-Bold", size),
                      ("BACKGROUND", (0, h), (-1, h), colors.HexColor("#eef2f7"))]
        table = LongTable(data, colWidths=col_widths, repeatRows=0, hAlign="LEFT")
        table.setStyle(TableStyle(style))
        story.append(table)
        if s.truncated:
            story.append(Spacer(1, 3 * mm))
            story.append(Paragraph(f"Only the first {settings.INTAKE_SHEET_MAX_ROWS:,} rows were read.", note))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(margin, 7 * mm, f"{filename}, copy of the spreadsheet, page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=margin, rightMargin=margin, topMargin=margin,
                            bottomMargin=margin + 4 * mm, title=filename, author="ShipMatch")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _is_number(value: str) -> bool:
    return bool(re.fullmatch(r"[-+(]?[A-Z]{0,3}\s?[\d,]+(\.\d+)?\)?%?", value.strip()))


# --------------------------------------------------------------------------- reading the table (rules)

# Column headings and labels, normalized (lower case, no punctuation except / and #).
SYNONYMS = {
    "description": ["description", "charge description", "charge", "charges", "item", "item description", "details",
                    "service", "particulars", "charge type", "charge name", "goods", "product", "line description",
                    "description of goods", "description of charges", "narrative"],
    "quantity": ["qty", "quantity", "units", "unit qty", "no of units", "pcs", "pieces", "count"],
    "unit_price": ["rate", "unit price", "price", "unit rate", "unit cost", "price per unit", "rate per unit",
                   "unit amount", "tariff"],
    "amount": ["amount", "line total", "total", "ext amount", "extended amount", "extended", "value", "net amount",
               "line amount", "charge amount", "total amount", "amount due", "sum", "credit amount", "amount credited"],
    "container_numbers": ["container", "container no", "container number", "container #", "containers", "cntr",
                          "cntr no", "equipment", "equipment no", "box no"],
    "bl_number": ["b/l", "bl", "b/l no", "b/l number", "b/l #", "bl no", "bl number", "bill of lading",
                  "bill of lading no", "bill of lading number", "mbl", "hbl", "bol", "mbl no", "hbl no", "master b/l",
                  "house b/l"],
    "invoice_number": ["invoice no", "invoice number", "invoice #", "inv no", "inv #", "invoice", "invoice ref",
                       "invoice reference"],
    "invoice_date": ["invoice date", "inv date", "date", "document date", "billing date", "date of issue",
                     "issue date", "credit note date", "credit date"],
    "due_date": ["due date", "payment due", "due"],
    "vendor_name": ["vendor", "supplier", "carrier", "billed by", "bill from", "vendor name", "supplier name",
                    "carrier name"],
    "po_numbers": ["po", "po no", "po number", "po #", "p/o", "purchase order", "purchase order no",
                   "customer ref", "customer reference", "customer ref / po", "your ref"],
    "currency": ["currency", "curr", "ccy", "cur"],
    "credit_note_number": ["credit note no", "credit note number", "credit note #", "credit memo no",
                           "credit memo number", "credit memo #", "cn no", "credit no"],
    "original_invoice_number": ["original invoice", "original invoice no", "original invoice number",
                                "against invoice", "invoice credited", "applies to invoice", "reference invoice"],
}
TOTAL_LABELS = {"total", "grand total", "total due", "amount due", "invoice total", "balance due", "total amount",
                "total payable", "total credit", "credit total", "total charges", "net total", "total to pay"}
SUBTOTAL_LABELS = {"subtotal", "sub total", "sub-total", "total before tax", "carried forward"}
CURRENCIES = {"USD", "EUR", "GBP", "CNY", "AED", "SAR", "PKR", "INR", "JPY", "CAD", "AUD", "TRY", "VND", "SGD", "HKD"}
TITLE_RE = re.compile(r"\b(commercial invoice|freight invoice|tax invoice|credit note|credit memo|"
                      r"bill of lading|invoice|statement)\b", re.I)

_LOOKUP = {s: name for name, words in SYNONYMS.items() for s in words}


def norm_label(cell: str) -> tuple[str, str | None]:
    """'Amount (USD):' -> ('amount', 'USD'). The currency found in a heading is returned separately."""
    s = (cell or "").strip()
    currency = None
    m = re.search(r"[\(\[]\s*([A-Za-z]{3})\s*[\)\]]", s)
    if m and m.group(1).upper() in CURRENCIES:
        currency = m.group(1).upper()
        s = s[:m.start()] + s[m.end():]
    s = s.lower().replace("no.", "no").replace("nr.", "no").replace("num.", "number")
    s = re.sub(r"[^a-z0-9/# ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    words = s.split(" ")
    if len(words) > 1 and words[-1].upper() in CURRENCIES:
        currency = currency or words[-1].upper()
        s = " ".join(words[:-1])
    return s, currency


def column_for(cell: str) -> str | None:
    return _LOOKUP.get(norm_label(cell)[0])


def find_header(rows: list[list[str]]) -> tuple[int | None, dict[str, int]]:
    """The line-item header row: the row naming the most known columns, with an amount column
    (or a description with a price). Returns (row index, {field: column index})."""
    best, best_map = None, {}
    for i, row in enumerate(rows[:200]):
        mapping: dict[str, int] = {}
        for c, cell in enumerate(row):
            name = column_for(cell) if cell and not _is_number(cell) and len(cell) <= 40 else None
            if name and name not in mapping:
                mapping[name] = c
        usable = "amount" in mapping or ("description" in mapping and "unit_price" in mapping)
        if len(mapping) >= 2 and usable and len(mapping) > len(best_map):
            best, best_map = i, mapping
    return best, best_map


def _money(raw: str, positive: bool = False) -> Decimal | None:
    s = (raw or "").strip()
    if not s or not re.search(r"\d", s) or parse_date(s):
        return None
    negative = s.startswith("(") and s.endswith(")") or s.startswith("-") or s.upper().endswith(" CR")
    value = parse_money(s.strip("()").replace("CR", "").replace("cr", ""))
    if value is None:
        return None
    value = abs(value)
    return value if positive or not negative else -value


def _quantity(raw: str) -> str | None:
    m = re.fullmatch(r"\s*([\d,]+(?:\.\d+)?)\s*", raw or "")
    if not m:
        return None
    try:
        q = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return str(q.normalize()) if q != q.to_integral() else str(int(q))


TOTAL_WORDS = {"due", "amount", "payable", "invoice", "charges", "credit", "net", "to", "pay", "incl", "including",
               "vat", "tax", "sum", "balance", "of", "this"}


def _is_total_label(cell: str) -> str | None:
    """'Total due', 'TOTAL (USD)', 'Grand total' -> 'total'; 'Subtotal' -> 'subtotal'; 'Total Logistics Fee' -> None."""
    s, _ = norm_label(cell)
    s = s.rstrip(" :")
    if s in SUBTOTAL_LABELS or s.startswith("subtotal") or s.startswith("sub total"):
        return "subtotal"
    words = s.split(" ")
    if s in TOTAL_LABELS or (words[0] == "total" and all(w in TOTAL_WORDS for w in words[1:])):
        return "total"
    return None


def _date_value(raw: str) -> str | None:
    d = parse_date((raw or "").split(" ")[0]) or parse_date(raw)
    return d.isoformat() if d else None


@dataclass
class _Found:
    values: dict = field(default_factory=dict)
    conf: dict = field(default_factory=dict)

    def put(self, name, value, conf=LABEL_CONF, override=False):
        if value in (None, "", []):
            return
        if name in self.values and not override:
            return
        self.values[name] = value
        self.conf[name] = conf


def _key_values(rows: list[list[str]], found: _Found, credit: bool) -> list[str]:
    """Label/value cells outside the line-item table ('Invoice No.' | 'HL-1', or 'Invoice No.: HL-1').
    Returns the rows flattened to 'label: value' lines for the text-based rules."""
    flat = []
    for row in rows:
        cells = [c for c in row if c]
        if not cells:
            continue
        i = 0
        while i < len(cells):
            cell = cells[i]
            label, value = None, None
            if ":" in cell and not re.match(r"^\d{1,2}:\d{2}", cell):
                left, right = cell.split(":", 1)
                if right.strip() and (column_for(left) or _is_total_label(left)):
                    label, value = left, right.strip()
            if label is None and (column_for(cell) or _is_total_label(cell)) and i + 1 < len(cells):
                nxt = cells[i + 1]
                if not (column_for(nxt) or _is_total_label(nxt)):
                    label, value = cell, nxt
                    i += 1
            if label is None:
                flat.append(cell)
                i += 1
                continue
            flat.append(f"{label.rstrip(' :')}: {value}")
            _put_labelled(found, label, value, credit)
            i += 1
    return flat


def _put_labelled(found: _Found, label: str, value: str, credit: bool) -> None:
    _, heading_currency = norm_label(label)
    total = _is_total_label(label)
    if total == "total":
        amount = _money(value, positive=credit)
        if amount is not None:
            found.put("total_amount", str(amount))
            found.put("currency", heading_currency or _currency_in(value))
        return
    name = column_for(label)
    if name in ("invoice_date", "due_date"):
        found.put(name, _date_value(value))
    elif name == "container_numbers":
        found.put(name, find_containers(value))
    elif name == "po_numbers":
        found.put(name, [p.strip() for p in re.split(r"[,;]", value) if p.strip()])
    elif name == "currency":
        m = re.search(r"\b([A-Z]{3})\b", value.upper())
        found.put(name, m.group(1) if m and m.group(1) in CURRENCIES else None)
    elif name == "invoice_number" and credit:
        found.put("original_invoice_number", value.split()[0])
    elif name in ("invoice_number", "bl_number", "credit_note_number", "original_invoice_number"):
        found.put(name, value.split()[0] if value.split() else None)
    elif name == "vendor_name":
        found.put(name, value)


def _currency_in(value: str) -> str | None:
    m = re.search(r"\b([A-Z]{3})\b", (value or "").upper())
    return m.group(1) if m and m.group(1) in CURRENCIES else None


def _vendor_guess(flat: list[str]) -> tuple[str | None, float]:
    for line in flat[:5]:
        candidate = TITLE_RE.sub("", line).strip(" -:|")
        if len(candidate) < 3 or re.match(r"^\d", candidate) or ":" in candidate or _is_number(candidate):
            continue
        if column_for(candidate) or _is_total_label(candidate):
            continue
        return candidate, (LABEL_CONF if COMPANY_SUFFIX.search(candidate) else GUESS_CONF)
    return None, 0.0


def _table(rows: list[list[str]], h: int, cols: dict[str, int], found: _Found, credit: bool) -> tuple[list[dict], int]:
    """Line items below the header, up to the total row. Returns (items, index after the table)."""
    items: list[dict] = []
    refs: dict[str, list[str]] = {k: [] for k in ("container_numbers", "bl_number", "invoice_number", "invoice_date",
                                                     "vendor_name", "po_numbers", "currency", "due_date")}
    end, blank_run = len(rows), 0
    _, amount_currency = norm_label(rows[h][cols["amount"]]) if "amount" in cols else (None, None)
    if "invoice_date" in cols and norm_label(rows[h][cols["invoice_date"]])[0] == "date":
        cols = {k: v for k, v in cols.items() if k != "invoice_date"}  # a plain 'Date' column is a service date

    def cell(row, name):
        c = cols.get(name)
        return row[c] if c is not None and c < len(row) else ""

    for i in range(h + 1, len(rows)):
        row = rows[i]
        if not any(row):
            blank_run += 1
            if blank_run >= 2 and items:
                end = i
                break
            continue
        blank_run = 0
        kind = next((k for k in (_is_total_label(c) for c in row if c) if k), None)
        if kind:
            amount = _money(cell(row, "amount"), positive=credit)
            if amount is None:  # total printed in another column: the last amount on the row
                amounts = [m for m in (_money(c, positive=credit) for c in row if c) if m is not None]
                amount = amounts[-1] if amounts else None
            if kind == "total" and amount is not None:
                found.put("total_amount", str(amount))
                end = i + 1
                break
            continue  # a subtotal: keep looking for the total
        amount = _money(cell(row, "amount"), positive=credit)
        if amount is None:
            continue
        description = cell(row, "description")
        if not description:
            texts = [c for j, c in enumerate(row) if c and not _is_number(c) and j not in cols.values()]
            description = " ".join(texts) or "Charge"
        item = {"description": description, "amount": str(amount)}
        qty = _quantity(cell(row, "quantity"))
        price = _money(cell(row, "unit_price"), positive=True)
        if qty is not None:
            item["quantity"] = qty
        if price is not None:
            item["unit_price"] = str(price)
        items.append(item)
        for name in refs:
            value = cell(row, name)
            if value and value not in refs[name]:
                refs[name].append(value)

    containers = [c for v in refs["container_numbers"] for c in (find_containers(v) or [v.upper().replace(" ", "")])]
    found.put("container_numbers", list(dict.fromkeys(containers)))
    found.put("po_numbers", refs["po_numbers"])
    for name in ("bl_number", "vendor_name", "currency"):
        if refs[name]:
            found.put(name, refs[name][0], LABEL_CONF if len(refs[name]) == 1 else PATTERN_CONF)
    if refs["invoice_number"]:
        target = "original_invoice_number" if credit else "invoice_number"
        found.put(target, refs["invoice_number"][0], LABEL_CONF if len(refs["invoice_number"]) == 1 else PATTERN_CONF)
    for name in ("invoice_date", "due_date"):
        if refs[name]:
            found.put(name, _date_value(refs[name][0]))
    found.put("currency", amount_currency)
    return items, end


def sheet_rules(doc_type: str, text: str) -> RuleResult:
    """Rule-based extraction for the text form of a spreadsheet."""
    from apps.documents.services.extract_rules import extract_rules

    credit = doc_type == "credit_note"
    rows = [r for sheet in parse_text(text) for r in sheet]
    h, cols = find_header(rows)
    found = _Found()
    items: list[dict] = []
    outside = rows
    if h is not None:
        items, end = _table(rows, h, cols, found, credit)
        outside = rows[:h] + rows[end:]
    flat = _key_values(outside, found, credit)
    if h is not None:  # the heading row itself can carry a currency, e.g. 'Amount (EUR)'
        for c in rows[h]:
            found.put("currency", norm_label(c)[1])

    # Labels written in other ways ('MBL:', 'Customer Ref / PO:') are read by the text rules.
    flat_text = "\n".join(flat)
    if credit:
        from .credit import credit_note_rules

        base = credit_note_rules(flat_text)
    else:
        base = extract_rules(doc_type, flat_text)
    r = RuleResult()
    vendor, vendor_conf = _vendor_guess(flat)
    if doc_type == "bill_of_lading":
        found.put("carrier_name", found.values.get("vendor_name") or vendor, vendor_conf or LABEL_CONF)
        found.values.pop("vendor_name", None)
    else:
        found.put("vendor_name", vendor, vendor_conf)
    for name, value in found.values.items():
        r.put(name, value, found.conf[name])
    for name, value in base.values.items():
        if name not in r.values and name != "line_items":
            r.put(name, value, base.confidence[name])
    if items and doc_type != "bill_of_lading":
        r.put("line_items", items, LABEL_CONF)
    elif base.values.get("line_items") and doc_type != "bill_of_lading":
        r.put("line_items", base.values["line_items"], base.confidence["line_items"])
    every = find_containers("\n".join(CELL_SEP.join(row) for row in rows))
    if every:
        merged = list(dict.fromkeys((r.values.get("container_numbers") or []) + every))
        r.put("container_numbers", merged, r.confidence.get("container_numbers", LABEL_CONF))
    allowed = set(SCHEMAS[doc_type].model_fields) if doc_type in SCHEMAS else set()
    for name in [n for n in r.values if n not in allowed]:
        r.values.pop(name)
        r.confidence.pop(name)
    return r
