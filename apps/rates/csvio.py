"""Quotes as CSV: the import template, import with row-level errors, and export.

One row per charge line. Rows with the same vendor, quote reference, lane, equipment and start
date form one quote. Importing a quote that already exists (same vendor, reference, lane,
equipment and start date) replaces its charge lines; anything else creates a new quote. The whole
file is checked first and nothing is saved if any row has a problem.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.accounting.models import vendor_key
from apps.core import csvsafe
from apps.core.utils import audit
from apps.documents.services.normalize import parse_date

from . import charges, lanes
from .models import Quote, QuoteCharge
from .services import snapshot

COLUMNS = ["vendor", "quote_reference", "origin", "destination", "equipment", "valid_from", "valid_to", "currency",
           "all_in", "charge_code", "charge_description", "amount", "basis", "notes"]
REQUIRED = ["vendor", "valid_from", "currency", "charge_code", "amount", "basis"]
HEADER_ALIASES = {
    "vendor_name": "vendor", "supplier": "vendor", "carrier": "vendor", "forwarder": "vendor",
    "reference": "quote_reference", "quote_ref": "quote_reference", "quote": "quote_reference",
    "quote_number": "quote_reference", "contract": "quote_reference",
    "pol": "origin", "port_of_loading": "origin", "from": "origin",
    "pod": "destination", "port_of_discharge": "destination", "to": "destination",
    "container_type": "equipment", "container": "equipment",
    "valid_until": "valid_to", "expires": "valid_to", "start": "valid_from", "end": "valid_to",
    "charge": "charge_code", "code": "charge_code", "description": "charge_description",
    "rate": "amount", "price": "amount", "unit": "basis", "per": "basis", "all_in_rate": "all_in",
}
BASIS_ALIASES = {
    "container": "container", "per container": "container", "box": "container", "per box": "container",
    "cntr": "container", "teu": "container", "unit": "container",
    "shipment": "shipment", "per shipment": "shipment", "file": "shipment", "lump sum": "shipment",
    "bl": "bl", "b/l": "bl", "per bl": "bl", "per b/l": "bl", "bill of lading": "bl", "document": "bl",
    "kg": "kg", "per kg": "kg", "kgs": "kg", "cbm": "cbm", "per cbm": "cbm", "m3": "cbm", "w/m": "cbm",
    "day": "day", "per day": "day", "hour": "hour", "per hour": "hour", "hr": "hour",
}
YES, NO = {"yes", "y", "true", "1", "x", "all-in", "all in"}, {"", "no", "n", "false", "0"}
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000

EXAMPLE_ROWS = [
    ["Example Forwarding LLC", "Q-2026-014", "Ningbo", "Long Beach, CA", "40HC", "2026-01-01", "2026-12-31", "USD", "no",
     "ocean_freight", "Ocean freight", "2400.00", "container", "Annual contract"],
    ["Example Forwarding LLC", "Q-2026-014", "Ningbo", "Long Beach, CA", "40HC", "2026-01-01", "2026-12-31", "USD", "no",
     "thc_destination", "Terminal handling (destination)", "350.00", "container", ""],
    ["Example Forwarding LLC", "Q-2026-014", "Ningbo", "Long Beach, CA", "40HC", "2026-01-01", "2026-12-31", "USD", "no",
     "documentation", "Documentation fee", "75.00", "bl", ""],
    ["Example Drayage Co", "D-77", "", "Long Beach, CA", "", "2026-01-01", "", "USD", "no",
     "trucking", "Drayage port to warehouse", "625.00", "container", "Origin left empty = any"],
]


@dataclass
class RowError:
    row: int
    column: str
    value: str
    message: str

    def as_list(self) -> list:
        return [self.row, self.column, self.value, self.message]


@dataclass
class ImportResult:
    created: list[Quote] = field(default_factory=list)
    updated: list[Quote] = field(default_factory=list)
    rows: int = 0
    errors: list[RowError] = field(default_factory=list)
    file_error: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors and not self.file_error


def _write(rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    for r in rows:
        w.writerow(csvsafe.row(r))
    return buf.getvalue()


def template_csv() -> str:
    return _write([COLUMNS, *EXAMPLE_ROWS])


def errors_csv(errors: list[list]) -> str:
    return _write([["row", "column", "value", "problem"], *errors])


def export_rows(quotes) -> list[list]:
    rows = [COLUMNS]
    for q in quotes:
        head = [q.vendor_name, q.reference, q.origin, q.destination, q.equipment, q.valid_from.isoformat(),
                q.valid_to.isoformat() if q.valid_to else "", q.currency, "yes" if q.all_in else "no"]
        lines = list(q.charges.all())
        if not lines:
            rows.append(head + ["", "", "", "", q.notes])
        for n, c in enumerate(lines):
            rows.append(head + [c.code, c.description, f"{c.amount:.2f}", c.basis, q.notes if n == 0 else ""])
    return rows


def export_csv(quotes) -> str:
    return _write(export_rows(quotes))


# --------------------------------------------------------------------------- import


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _header(name: str) -> str:
    key = (name or "").strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_")
    return HEADER_ALIASES.get(key, key)


def _amount(raw: str) -> Decimal | None:
    s = (raw or "").strip().replace(",", "").replace(" ", "")
    for sym in ("$", "€", "£", "¥"):
        s = s.replace(sym, "")
    try:
        v = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return v.quantize(Decimal("0.01")) if v.is_finite() else None


def parse(data: bytes) -> tuple[list[dict], ImportResult]:
    """Read and check every row. Returns quote groups (only meaningful when result.ok)."""
    result = ImportResult()
    if len(data) > MAX_BYTES:
        result.file_error = "The file is larger than 2 MB. Split it into smaller files."
        return [], result
    text = _decode(data)
    if not text.strip():
        result.file_error = "The file is empty. Start from the template."
        return [], result
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = [_header(h) for h in next(reader)]
    except (StopIteration, csv.Error):
        result.file_error = "The file has no header row. Start from the template."
        return [], result
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        result.file_error = (f"Missing column{'s' if len(missing) > 1 else ''}: {', '.join(missing)}. "
                             "The first row must hold the column names from the template.")
        return [], result
    groups: dict[tuple, dict] = {}
    try:
        rows = [[csvsafe.unquote(c) for c in r] for r in reader]   # undo the export's formula guard
    except csv.Error as e:
        result.file_error = f"The file isn't valid CSV ({e}). Save it as CSV (comma separated) and try again."
        return [], result
    if len(rows) > MAX_ROWS:
        result.file_error = f"The file has {len(rows):,} rows; the most per import is {MAX_ROWS:,}."
        return [], result
    for n, values in enumerate(rows, start=2):
        if not any((v or "").strip() for v in values):
            continue
        result.rows += 1
        row = {h: (values[i].strip() if i < len(values) else "") for i, h in enumerate(header)}
        errs: list[RowError] = []

        def err(col, msg, row=row, errs=errs, n=n):
            errs.append(RowError(n, col, row.get(col, ""), msg))

        vendor = row.get("vendor", "")
        if not vendor_key(vendor):
            err("vendor", "Vendor is missing.")
        start = parse_date(row.get("valid_from")) if row.get("valid_from") else None
        if start is None:
            err("valid_from", "Use a date like 2026-01-31." if row.get("valid_from") else "Start date is missing.")
        end = None
        if row.get("valid_to"):
            end = parse_date(row["valid_to"])
            if end is None:
                err("valid_to", "Use a date like 2026-12-31, or leave it empty for no end date.")
            elif start and end < start:
                err("valid_to", "The end date is before the start date.")
        cur = row.get("currency", "").upper()
        if len(cur) != 3 or not cur.isalpha():
            err("currency", "Use a three-letter currency code such as USD.")
        equipment = ""
        if row.get("equipment"):
            equipment = lanes.normalize_equipment(row["equipment"]) or ""
            if not equipment:
                err("equipment", f"Unknown equipment. Use one of {', '.join(sorted(lanes.EQUIPMENT_CODES))}, "
                                 "or leave it empty for any.")
        all_in_raw = row.get("all_in", "").lower()
        if all_in_raw not in YES | NO:
            err("all_in", "Use yes or no.")
        code = charges.code_for(row.get("charge_code", ""))
        if code is None:
            err("charge_code", "Unknown charge. Use a code from the list on the import page (e.g. ocean_freight)."
                if row.get("charge_code") else "Charge code is missing.")
        amount = _amount(row.get("amount", ""))
        if amount is None:
            err("amount", "Use a number like 2400.00." if row.get("amount") else "Amount is missing.")
        elif amount < 0:
            err("amount", "Use a positive amount.")
        basis = BASIS_ALIASES.get(row.get("basis", "").lower())
        if basis is None:
            err("basis", "Use container, shipment, bl, kg, cbm, day or hour." if row.get("basis")
                else "Basis is missing (container, shipment, bl, kg, cbm, day or hour).")
        for col, limit in (("vendor", 200), ("quote_reference", 60), ("origin", 120), ("destination", 120),
                           ("charge_description", 200)):
            if len(row.get(col, "")) > limit:
                err(col, f"Too long (at most {limit} characters).")
        if errs:
            result.errors.extend(errs)
            continue
        key = (vendor_key(vendor), row.get("quote_reference", ""), lanes.place_key(row.get("origin", "")),
               lanes.place_key(row.get("destination", "")), equipment, start)
        quote_fields = {"vendor_name": vendor, "reference": row.get("quote_reference", ""),
                        "origin": row.get("origin", ""), "destination": row.get("destination", ""),
                        "equipment": equipment, "valid_from": start, "valid_to": end, "currency": cur,
                        "all_in": all_in_raw in YES}
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"fields": quote_fields, "charges": [], "notes": row.get("notes", ""), "first_row": n}
        else:
            for f in ("valid_to", "currency", "all_in"):
                if g["fields"][f] != quote_fields[f]:
                    result.errors.append(RowError(n, f, row.get(f, ""),
                                                  f"Differs from row {g['first_row']} of the same quote. "
                                                  "Every line of one quote needs the same value."))
            if row.get("notes") and not g["notes"]:
                g["notes"] = row["notes"]
        g["charges"].append({"code": code, "description": row.get("charge_description", ""), "amount": amount,
                             "basis": basis})
    if not result.rows and not result.errors:
        result.file_error = "The file has a header but no rows."
    return list(groups.values()), result


def import_quotes(org, data: bytes, actor=None, filename: str = "") -> ImportResult:
    groups, result = parse(data)
    if not result.ok:
        return result
    with transaction.atomic():
        for g in groups:
            f = g["fields"]
            existing = (Quote.objects.filter(organization=org, vendor_key=vendor_key(f["vendor_name"]),
                                             reference=f["reference"], equipment=f["equipment"],
                                             valid_from=f["valid_from"])
                        .prefetch_related("charges"))
            existing = [q for q in existing if q.origin_key == lanes.place_key(f["origin"])
                        and q.destination_key == lanes.place_key(f["destination"])]
            if existing:
                q = existing[0]
                before = snapshot(q)
                for k, v in f.items():
                    setattr(q, k, v)
                q.notes = g["notes"] or q.notes
                q.archived = False
                q.updated_by = actor if getattr(actor, "is_authenticated", False) else None
                q.save()
                q.charges.all().delete()
                result.updated.append(q)
            else:
                q = Quote(organization=org, notes=g["notes"], **f)
                q.created_by = q.updated_by = actor if getattr(actor, "is_authenticated", False) else None
                q.save()
                before = None
                result.created.append(q)
            QuoteCharge.objects.bulk_create([QuoteCharge(quote=q, **c) for c in g["charges"]])
            after = snapshot(q)
            if before is None:
                audit(org, "quote.created", q, actor=actor, vendor=q.vendor_name, name=q.audit_name, reference=q.reference,
                      lane=q.lane, source="CSV import", file=filename, quote=after)
            else:
                audit(org, "quote.updated", q, actor=actor, vendor=q.vendor_name, name=q.audit_name, reference=q.reference,
                      lane=q.lane, source="CSV import", file=filename, before=before, after=after)
        audit(org, "quote.imported", org, actor=actor, file=filename, rows=result.rows,
              created=len(result.created), updated=len(result.updated))
    return result

