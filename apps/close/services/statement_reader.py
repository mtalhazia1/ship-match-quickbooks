"""Read a vendor statement (PDF, XLSX or CSV) into its header and lines.

Like invoices: the file's bytes decide its type (apps/intake/services/formats.py), spreadsheets are read
cell by cell with the intake spreadsheet reader (apps/intake/services/sheets.py), PDFs through the normal
text layer / OCR path. With AI reading on, the AI reads the statement into the schema in
apps/close/schemas.py and every amount it returns must be printed in the statement (lines that aren't are
dropped and the reviewer is told); without AI, or if the AI fails, the rules below read it.

Rules for spreadsheets: find the header row (a row naming a number or date column and an amount, debit,
credit or balance column), read each row below it, take the statement date, vendor, currency and balances
from label/value cells around the table.

Rules for PDFs: each text line with a date and an amount is a statement line; the last amount is the
running balance when the table has a balance column. "Balance brought forward" lines are the opening
balance and "Balance due" / "Total" lines the closing balance.

Line types come from a type column or words on the line (invoice, credit note, payment); otherwise a
positive amount is an invoice and a negative one a credit (a payment when the line says so).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from django.conf import settings

from apps.documents.services.extract_rules import COMPANY_SUFFIX
from apps.documents.services.ingest import RejectedFile
from apps.documents.services.normalize import numbers_in_text, parse_date, parse_money
from apps.intake.services import formats, sheets

from ..schemas import StatementSchema, wire_schema

log = logging.getLogger(__name__)
CENT = Decimal("0.01")
INVOICE, CREDIT, PAYMENT, OPENING = "invoice", "credit", "payment", "opening"


class StatementUnreadable(ValueError):
    """The file is a supported type but no statement lines could be read from it."""


@dataclass
class ParsedLine:
    kind: str
    amount: Decimal                 # signed: invoices +, credits and payments -, opening as printed
    number: str = ""
    date: date | None = None
    reference: str = ""
    balance: Decimal | None = None
    raw: str = ""


@dataclass
class ParsedStatement:
    vendor_name: str = ""
    statement_date: date | None = None
    currency: str = ""
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    lines: list[ParsedLine] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reader: str = "rules"
    text: str = ""
    source_format: str = ""
    usage: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- shared helpers

_PAYMENT = re.compile(r"\b(payments?|pmt|paid|remittance|receipt|wire|ach|cheque|check|eft|transfer|bank)\b", re.I)
_CREDIT = re.compile(r"\b(credit\s*notes?|credit\s*memos?|credits?|crn|cn|cm|refund|rebate|allowance)\b", re.I)
_CREDIT_NUMBER = re.compile(r"^(CN|CR|CM|CRN)[-\s/]?\d", re.I)
_INVOICE = re.compile(r"\b(invoices?|inv|bill|debit\s*note|charges?)\b", re.I)
_OPENING = re.compile(r"\b(balance\s+(brought|b/?)\s*forward|opening\s+balance|balance\s+forward|previous\s+balance|"
                      r"brought\s+forward|b/f|carried\s+forward\s+from)\b", re.I)
_CLOSING = re.compile(r"^\s*(closing\s+balance|balance\s+due|total\s+due|amount\s+due|total\s+outstanding|"
                      r"total\s+balance|balance\s+outstanding|total\s+amount\s+due|new\s+balance|total|balance)\b"
                      r"(?!\s+(brought|b/?f|forward))", re.I)
_MONEY = re.compile(r"(?<![\w.])\(?-?(?:[A-Z]{3}\s?)?\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})\)?(?:\s?CR\b|\s?DR\b)?|"
                    r"(?<![\w.])\(?-?\$?\d+\.\d{2}\)?(?:\s?CR\b|\s?DR\b)?")
_DATE_SHAPES = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}|\d{1,2}[ -](?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|"
    r"nov|dec)[a-z]*[ -]\d{2,4}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]* \d{1,2},? \d{4})\b",
    re.I)
_NUMBER_TOKEN = re.compile(r"^(?=[A-Z0-9/#-]*\d)[A-Z0-9][A-Z0-9/#.-]{2,}$", re.I)
_TITLE_WORDS = re.compile(r"\b(statement of accounts?|account statement|statement|vendor|supplier|customer|"
                          r"page \d+( of \d+)?)\b", re.I)
_CUR = re.compile(r"\b(" + "|".join(sorted(sheets.CURRENCIES)) + r")\b")


def amount_of(raw) -> Decimal | None:
    """'1,234.50' -> 1234.50; '(1,234.50)', '-1,234.50', '1,234.50 CR' -> -1234.50."""
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        try:
            return Decimal(str(raw)).quantize(CENT)
        except InvalidOperation:
            return None
    s = str(raw).strip()
    if not s or not re.search(r"\d", s):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", s):
        return None
    negative = (s.startswith("(") and s.endswith(")")) or s.lstrip("$ ").startswith("-") or \
        bool(re.search(r"\bCR$", s, re.I)) or bool(re.search(r"^-|\s-\$?\d", s))
    cleaned = re.sub(r"\b(CR|DR)$", "", s, flags=re.I).strip("() ")
    cleaned = _CUR.sub("", cleaned).replace("$", "").strip()
    value = parse_money(cleaned.lstrip("-"))
    if value is None:
        return None
    return -abs(value) if negative else abs(value)


def date_of(raw) -> date | None:
    if isinstance(raw, date):
        return raw
    s = str(raw or "").strip()
    if not s:
        return None
    d = parse_date(s.split(" ")[0]) or parse_date(s)
    if d:
        return d
    m = _DATE_SHAPES.search(s)
    return parse_date(m.group(1).replace("Sept", "Sep").replace("sept", "sep")) if m else None


def kind_from(words: str, number: str, signed: Decimal | None) -> str | None:
    text = f"{words} {number}"
    if _OPENING.search(text):
        return OPENING
    if _PAYMENT.search(words or ""):
        return PAYMENT
    if _CREDIT_NUMBER.match(number or "") or _CREDIT.search(words or ""):
        return CREDIT
    if _INVOICE.search(words or ""):
        return INVOICE
    if signed is not None and signed < 0:
        return CREDIT
    return None


def signed_for(kind: str, amount: Decimal) -> Decimal:
    if kind == INVOICE:
        return abs(amount)
    if kind in (CREDIT, PAYMENT):
        return -abs(amount)
    return amount


# --------------------------------------------------------------------------- entry point


ACCEPTED = "Statements are read from PDF, Excel (.xlsx) or CSV files"


def read(filename: str, content: bytes) -> ParsedStatement:
    try:
        kind = formats.detect(filename, content)
    except RejectedFile as e:
        if "not a PDF, image, spreadsheet or ZIP" in str(e):
            raise RejectedFile(f"{filename}: this isn't a file ShipMatch can read. {ACCEPTED}; ask the vendor for one "
                               "of those.") from e
        raise
    formats.check_size(filename, content, kind)
    if kind.kind == formats.SPREADSHEET:
        loaded = sheets.load(filename, content, kind.subtype)
        text = sheets.to_text(filename, loaded)
        pdf = None
        rules = lambda: read_sheet_rows(loaded)  # noqa: E731
    elif kind.kind == formats.PDF:
        from apps.documents.services.ocr import read_text

        result = read_text(content)
        text, pdf = result.text, content
        if result.needs_ocr:
            from apps.documents.services import llm

            if not llm.can_read_pdfs():
                raise StatementUnreadable(
                    f"{filename} is a scanned PDF with no text, and no OCR is set up. Ask the vendor for the "
                    "statement as a PDF with text or as an Excel file.")
            text = ""
        rules = lambda: read_pdf_text(text)  # noqa: E731
    else:
        raise RejectedFile(f"{filename}: statements are read from PDF, Excel (.xlsx) or CSV files. Ask the vendor "
                           f"for one of those (this file is a {kind.label}).")
    parsed = None
    notes: list[str] = []
    from apps.documents.services import llm

    if llm.is_enabled():
        try:
            parsed = read_with_ai(text, pdf if (pdf and (not text.strip() or settings.LLM_INPUT == "pdf")) else None)
            notes += parsed.notes
            if not parsed.lines:
                notes.append("The AI reader found no lines, so the statement was read with the rules.")
                parsed = None
        except Exception as e:  # LLMError, network, malformed answer: never lose the upload over it
            log.warning("AI statement reading failed: %s", e)
            notes.append("The AI reader could not read this statement, so it was read with the rules.")
            parsed = None
    if parsed is None:
        if not text.strip():
            raise StatementUnreadable(f"{filename}: no text could be read from this statement.")
        parsed = rules()
        parsed.notes = notes + parsed.notes
    parsed.text = text
    parsed.source_format = kind.subtype
    _finish(parsed)
    if not parsed.lines:
        raise StatementUnreadable(
            f"No statement lines could be read from {filename}. Check that it is the vendor's statement of account "
            "(a list of invoices, credits and payments with amounts); a PDF statement must have a table with a "
            "date and an amount on each line.")
    return parsed


def _finish(p: ParsedStatement) -> None:
    limit = getattr(settings, "CLOSE_STATEMENT_MAX_LINES", 2000)
    if len(p.lines) > limit:
        p.notes.append(f"Only the first {limit:,} lines were read.")
        p.lines = p.lines[:limit]
    _open_amounts(p)
    openings = [ln for ln in p.lines if ln.kind == OPENING]
    if openings and p.opening_balance is None:
        p.opening_balance = openings[0].amount
    if p.statement_date is None:
        dated = [ln.date for ln in p.lines if ln.date]
        if dated:
            p.statement_date = max(dated)
            p.notes.append("No statement date was printed; the date of the last line is used.")
    if p.closing_balance is None:
        with_balance = [ln for ln in p.lines if ln.balance is not None]
        if with_balance:
            p.closing_balance = with_balance[-1].balance
    p.currency = (p.currency or "").upper()[:3]


def _open_amounts(p: ParsedStatement) -> None:
    """A 'balance' column is either a running balance (each line's balance = the previous one + its amount) or,
    on open-item statements, what is still open on each line. In the second case the open amount is what the
    vendor says is owed, so it becomes the line's amount."""
    rows = [ln for ln in p.lines if ln.kind != OPENING]
    with_balance = [ln for ln in rows if ln.balance is not None]
    if len(with_balance) < 2 or len(with_balance) < len(rows):
        return
    running = p.opening_balance if p.opening_balance is not None else next(
        (ln.amount for ln in p.lines if ln.kind == OPENING), Decimal("0.00"))
    fits = 0
    for ln in rows:
        running += ln.amount
        if abs(running - ln.balance) <= CENT:
            fits += 1
        running = ln.balance
    if fits >= 0.6 * len(rows):
        return
    for ln in rows:
        ln.amount = signed_for(ln.kind, ln.balance) if ln.kind != OPENING else ln.balance
        ln.balance = None
    p.notes.append("The statement shows what is still open on each line (not a running balance); open amounts are "
                   "compared.")


# --------------------------------------------------------------------------- AI reader

AI_SYSTEM = (
    "You read vendor statements of account sent to an importer's accounts payable team. Return the statement's "
    "header and every line exactly as printed: invoices, credit notes, payments received and the opening balance "
    "(balance brought forward). Amounts are positive numbers; the type says which way each line moves the "
    "balance. Do not add lines that are not printed, do not include ageing summaries or totals as lines, and use "
    "null for anything not printed."
)


def read_with_ai(text: str, pdf: bytes | None = None) -> ParsedStatement:
    from apps.documents.services import llm

    with llm.track_usage() as calls:
        user = "Read this vendor statement." + (f"\n\n<statement>\n{text[:60000]}\n</statement>" if text else "")
        answer = llm.structured_call(AI_SYSTEM, user, wire_schema(), name="vendor_statement",
                                     purpose="read_statement", pdf=pdf)
    data = StatementSchema.model_validate(answer or {})
    p = ParsedStatement(vendor_name=(data.vendor_name or "").strip(), statement_date=data.statement_date,
                        currency=data.currency or "", opening_balance=data.opening_balance,
                        closing_balance=data.closing_balance, reader=llm.provider(), usage=llm.summarize(calls))
    printed = numbers_in_text(text) if text else None
    dropped = 0
    for line in data.lines:
        amount = Decimal(str(line.amount)).quantize(CENT)
        if printed is not None and abs(amount) not in printed:
            dropped += 1
            continue
        kind = {"opening_balance": OPENING}.get(line.type, line.type)
        p.lines.append(ParsedLine(kind=kind, amount=signed_for(kind, amount) if kind != OPENING else amount,
                                  number=(line.invoice_number or "").strip()[:80], date=line.date,
                                  reference=(line.reference or "").strip()[:200],
                                  balance=Decimal(str(line.balance)).quantize(CENT) if line.balance is not None
                                  else None, raw=""))
    if dropped:
        word, verb = ("lines", "were") if dropped != 1 else ("line", "was")
        p.notes.append(f"{dropped} {word} the AI returned {verb} not printed in the statement and {verb} left out.")
    if printed is None:
        p.notes.append("This scanned statement was read by AI; its amounts could not be checked against a text layer.")
    return p


# --------------------------------------------------------------------------- spreadsheets

COLUMNS = {
    "number": ["invoice no", "invoice number", "invoice #", "inv no", "inv #", "invoice", "document no",
               "document number", "doc no", "doc #", "document", "number", "no", "#", "transaction no",
               "transaction number", "trans no", "ref no", "reference no", "your invoice", "invoice/credit no",
               "document ref"],
    "date": ["date", "invoice date", "doc date", "document date", "transaction date", "trans date", "posting date",
             "inv date"],
    "type": ["type", "doc type", "document type", "transaction type", "trans type", "transaction"],
    "reference": ["reference", "ref", "your ref", "your reference", "customer ref", "customer reference",
                  "description", "details", "narrative", "particulars", "memo", "b/l", "bl", "b/l no", "bl no",
                  "b/l number", "bl number", "bill of lading", "mbl", "hbl", "shipment", "po", "po no", "po number",
                  "container"],
    "amount": ["amount", "original amount", "invoice amount", "total", "value", "net amount", "gross amount",
               "open amount", "outstanding", "amount outstanding", "open"],
    "debit": ["debit", "debits", "dr", "charges", "invoiced", "debit amount"],
    "credit": ["credit", "credits", "cr", "payments", "payment", "paid", "payments/credits", "credits/payments",
               "payment/credit", "credit/payment", "credit amount"],
    "balance": ["balance", "running balance", "cumulative balance", "balance due"],
    "currency": ["currency", "curr", "ccy", "cur"],
    "due_date": ["due date", "due"],
}
_COL_LOOKUP = {s: name for name, words in COLUMNS.items() for s in words}
HEADER_LABELS = {
    "statement_date": ["statement date", "date", "as of", "as at", "statement as of", "statement as at",
                       "as of date", "period ending", "period end"],
    "vendor_name": ["vendor", "supplier", "from", "company", "vendor name", "supplier name"],
    "currency": ["currency", "statement currency", "ccy"],
    "closing_balance": ["balance due", "closing balance", "total due", "amount due", "total outstanding",
                        "total balance", "balance outstanding", "total amount due", "new balance", "balance"],
    "opening_balance": ["opening balance", "balance brought forward", "balance forward", "previous balance",
                        "brought forward"],
}
_HEADER_LOOKUP = {s: name for name, words in HEADER_LABELS.items() for s in words}


def _col(cell: str) -> tuple[str | None, str | None]:
    label, cur = sheets.norm_label(cell)
    label = label.rstrip(" :")
    return _COL_LOOKUP.get(label), cur


def find_columns(rows: list[list[str]]) -> tuple[int | None, dict[str, list[int]], str | None]:
    best, best_map, best_cur = None, {}, None
    for i, row in enumerate(rows[:150]):
        mapping: dict[str, list[int]] = {}
        heading_cur = None
        for c, cell in enumerate(row):
            if not cell or len(cell) > 40 or amount_of(cell) is not None and not re.search(r"[A-Za-z#]", cell):
                continue
            name, cur = _col(cell)
            if name:
                mapping.setdefault(name, []).append(c)
                heading_cur = heading_cur or cur
        money = any(k in mapping for k in ("amount", "debit", "credit", "balance"))
        keyed = any(k in mapping for k in ("number", "date"))
        if money and keyed and len(mapping) >= 2 and len(mapping) > len(best_map):
            best, best_map, best_cur = i, mapping, heading_cur
    return best, best_map, best_cur


def _cell(row: list[str], idx: list[int] | None, which: int = 0) -> str:
    if not idx or which >= len(idx) or idx[which] >= len(row):
        return ""
    return (row[idx[which]] or "").strip()


def read_sheet_rows(loaded: list[sheets.Sheet]) -> ParsedStatement:
    p = ParsedStatement()
    skipped: list[str] = []
    for sheet in loaded:
        header, cols, heading_cur = find_columns(sheet.rows)
        if header is None:
            continue
        _labels_around(sheet.rows[:header], p)
        if heading_cur and not p.currency:
            p.currency = heading_cur
        for row in sheet.rows[header + 1:]:
            cells = [c for c in row if c]
            if not cells:
                continue
            first = cells[0]
            number = _cell(row, cols.get("number"))
            raw_type = _cell(row, cols.get("type"))
            refs = " ".join(x for x in (_cell(row, cols.get("reference"), i) for i in range(len(cols.get("reference", []))))
                            if x)
            words = " ".join(x for x in (raw_type, refs) if x)
            if not number and not _cell(row, cols.get("date")) and _CLOSING.match(first) and not _OPENING.search(first):
                value = _last_money(row)
                if value is not None:
                    p.closing_balance = value
                continue
            amount = amount_of(_cell(row, cols.get("amount"))) if "amount" in cols else None
            if amount is None and ("debit" in cols or "credit" in cols):
                debit = amount_of(_cell(row, cols.get("debit")))
                credit = amount_of(_cell(row, cols.get("credit")))
                if debit is not None or credit is not None:
                    amount = (abs(debit) if debit else Decimal("0.00")) - (abs(credit) if credit else Decimal("0.00"))
            balance = amount_of(_cell(row, cols.get("balance"))) if "balance" in cols else None
            if _OPENING.search(" ".join(cells)):
                value = balance if balance is not None else amount
                if value is not None:
                    p.lines.append(ParsedLine(OPENING, value, "", date_of(_cell(row, cols.get("date"))),
                                              "Opening balance", balance, " | ".join(cells)[:500]))
                continue
            if amount is None and (number or _cell(row, cols.get("date"))):
                skipped.append(" | ".join(cells)[:70])   # looks like a transaction but no amount could be read
            if amount is None or (amount == 0 and not number):
                continue
            kind = kind_from(words, number, amount) or (INVOICE if amount >= 0 else CREDIT)
            if kind == OPENING:
                kind = INVOICE if amount >= 0 else CREDIT
            if not p.currency and "currency" in cols:
                m = _CUR.search(_cell(row, cols.get("currency")).upper())
                p.currency = m.group(1) if m else ""
            p.lines.append(ParsedLine(kind, signed_for(kind, amount), number[:80], date_of(_cell(row, cols.get("date"))),
                                      refs[:200], balance, " | ".join(cells)[:500]))
        # Label/value cells after the table (closing balance, totals) when no total row was read.
        if p.closing_balance is None:
            _labels_around([r for r in sheet.rows[header + 1:] if r and not any(_cell(r, cols.get(k)) for k in
                                                                                 ("number", "date"))], p,
                           only=("closing_balance",))
        if p.lines:
            break
    if skipped and p.lines:
        shown = "; ".join(skipped[:3]) + ("; …" if len(skipped) > 3 else "")
        p.notes.append(f"{len(skipped)} row{'s' if len(skipped) != 1 else ''} had a number or date but no amount "
                       f"that could be read, so {'they were' if len(skipped) != 1 else 'it was'} left out: {shown}. "
                       "Check them against the file.")
    if not p.vendor_name:
        p.vendor_name = _company_from_rows(loaded[0].rows[:6]) if loaded else ""
    return p


def _last_money(row: list[str]) -> Decimal | None:
    for cell in reversed(row):
        value = amount_of(cell)
        if value is not None and not date_of(cell):
            return value
    return None


def _labels_around(rows: list[list[str]], p: ParsedStatement, only: tuple = ()) -> None:
    for row in rows:
        cells = [c for c in row if c]
        i = 0
        while i < len(cells):
            cell = cells[i]
            label, value = None, None
            if ":" in cell and not re.match(r"^\d{1,2}:\d{2}", cell):
                left, right = cell.split(":", 1)
                if right.strip() and _HEADER_LOOKUP.get(sheets.norm_label(left)[0].rstrip(" :")):
                    label, value = left, right.strip()
            if label is None and _HEADER_LOOKUP.get(sheets.norm_label(cell)[0].rstrip(" :")) and i + 1 < len(cells):
                label, value = cell, cells[i + 1]
                i += 1
            i += 1
            if label is None:
                continue
            norm, heading_cur = sheets.norm_label(label)
            name = _HEADER_LOOKUP.get(norm.rstrip(" :"))
            if only and name not in only:
                continue
            _put_header(p, name, value, heading_cur)


def _put_header(p: ParsedStatement, name: str | None, value: str, heading_cur: str | None = None) -> None:
    if name == "statement_date" and p.statement_date is None:
        p.statement_date = date_of(value)
    elif name == "vendor_name" and not p.vendor_name:
        p.vendor_name = value.strip()[:200]
    elif name == "currency" and not p.currency:
        m = _CUR.search(value.upper())
        p.currency = m.group(1) if m else ""
    elif name in ("closing_balance", "opening_balance") and getattr(p, name) is None:
        amount = amount_of(value)
        if amount is not None:
            setattr(p, name, amount)
            if heading_cur and not p.currency:
                p.currency = heading_cur
            m = _CUR.search(value.upper())
            if m and not p.currency:
                p.currency = m.group(1)


def _company_from_rows(rows: list[list[str]]) -> str:
    candidates = [c.strip() for r in rows for c in r if c and c.strip()]
    for c in candidates:
        if COMPANY_SUFFIX.search(c) and not _TITLE_WORDS.fullmatch(c.strip()) and ":" not in c and len(c) <= 120:
            return c[:200]
    return ""


# --------------------------------------------------------------------------- PDFs


def read_pdf_text(text: str) -> ParsedStatement:
    p = ParsedStatement()
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    has_balance_column = False
    debit_credit = False
    in_table = False
    for line in lines:
        low = line.lower()
        if not in_table and re.search(r"\b(amount|debit|credit)\b", low) and re.search(r"\b(date|invoice|number|no\.?|"
                                                                                         r"reference|type)\b", low) \
                and not _MONEY.search(line):
            in_table = True
            has_balance_column = "balance" in low
            debit_credit = "debit" in low and "credit" in low
            continue
        label_value = re.match(r"^(?P<label>[A-Za-z][A-Za-z /]{1,40}?)\s*:\s*(?P<value>.+)$", line)
        if label_value and not in_table:
            norm = sheets.norm_label(label_value.group("label"))[0]
            name = _HEADER_LOOKUP.get(norm)
            if name:
                _put_header(p, name, label_value.group("value"))
                continue
        monies = [m for m in _MONEY.finditer(line) if not _inside_date(line, m)]
        if not monies:
            continue
        found_date = _DATE_SHAPES.search(line)
        if _OPENING.search(line):
            value = amount_of(monies[-1].group(0))
            p.lines.append(ParsedLine(OPENING, value, "", date_of(found_date.group(1)) if found_date else None,
                                      "Opening balance", value, line[:500]))
            continue
        if _CLOSING.match(line) and not found_date:
            p.closing_balance = amount_of(monies[-1].group(0))
            continue
        if not found_date or not in_table:
            label = re.match(r"^(?P<label>[A-Za-z][A-Za-z /]{1,40}?)\s*:?\s*[\(\-$A-Z]{0,4}\d", line)
            if label and not in_table:
                name = _HEADER_LOOKUP.get(sheets.norm_label(label.group("label"))[0])
                if name in ("closing_balance", "opening_balance"):
                    _put_header(p, name, monies[-1].group(0))
            continue
        amounts = [amount_of(m.group(0)) for m in monies]
        if has_balance_column and len(amounts) >= 2:
            amount, balance = amounts[-2], amounts[-1]
            first_money = monies[-2].start()
        else:
            amount, balance = amounts[-1], None
            first_money = monies[-1].start()
        middle = line[found_date.end():first_money] if found_date.end() <= first_money else line[:first_money]
        before = line[:found_date.start()]
        rest = f"{before} {middle}".strip()
        tokens = rest.split()
        number = next((t for t in tokens if _NUMBER_TOKEN.match(t) and not date_of(t)), "")
        words = " ".join(t for t in tokens if t != number)
        kind = kind_from(words, number, amount) or (INVOICE if amount >= 0 else CREDIT)
        if kind == OPENING:
            kind = INVOICE if amount >= 0 else CREDIT
        if debit_credit and amount is not None and amount > 0 and kind in (CREDIT, PAYMENT):
            amount = -amount
        reference = _strip_type_words(words)
        p.lines.append(ParsedLine(kind, signed_for(kind, amount), number[:80], date_of(found_date.group(1)),
                                  reference[:200], balance, line[:500]))
    if not p.vendor_name:
        p.vendor_name = _company_from_lines(lines[:6])
    if not p.currency:
        head = "\n".join(lines[:25])
        m = re.search(r"\bcurrency\s*:?\s*([A-Z]{3})\b", head, re.I) or re.search(r"\(([A-Z]{3})\)", head)
        if m and m.group(1).upper() in sheets.CURRENCIES:
            p.currency = m.group(1).upper()
    return p


def _inside_date(line: str, m: re.Match) -> bool:
    for d in _DATE_SHAPES.finditer(line):
        if d.start() <= m.start() < d.end():
            return True
    return False


_TYPE_WORDS = re.compile(r"^\s*(invoice|inv|credit\s*note|credit\s*memo|credit|payment(\s+received)?|pmt|"
                         r"receipt|debit\s*note)\b[\s:-]*", re.I)


def _strip_type_words(words: str) -> str:
    return _TYPE_WORDS.sub("", words or "").strip(" |-")


def _company_from_lines(lines: list[str]) -> str:
    for line in lines:
        candidate = _TITLE_WORDS.sub("", line).strip(" -:|")
        if len(candidate) < 3 or re.match(r"^\d", candidate) or ":" in candidate:
            continue
        if COMPANY_SUFFIX.search(candidate):
            return candidate[:200]
    return ""
