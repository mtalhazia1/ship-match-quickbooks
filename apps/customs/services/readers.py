"""Rule-based readers for customs entries and arrival notices (no AI), registered with
apps.documents.services.extract_rules.register_rules_reader.

Both read "Label: value" pairs. A form prints several numbered boxes on one line ("1. Filer Code/Entry No.:
ABC-1234567-8 2. Entry Type: 01 ABI/A 3. Summary Date: 04/02/2026"), so each line is cut into segments at the
box numbers and at known labels before the value after each label is read. Tables (tariff lines, containers,
charges) are read row by row under their header.
"""
from __future__ import annotations

import re
from datetime import date

from apps.documents.services.extract_rules import (
    LABEL_CONF,
    PATTERN_CONF,
    RuleResult,
    _company,
    _currency,
    _line_items,
    _total,
)
from apps.documents.services.normalize import parse_date, parse_money
from apps.shipments.services.containers import find_containers

from .entry import US_ENTRY

# --------------------------------------------------------------------------- shared helpers

BOX = re.compile(r"(?:(?<=\s)|^)(?:\d{1,2}[A-Z]?|[A-Z])\.\s+(?=[A-Za-z])")
DATE_TOKEN = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{2,4}"
    r"|[A-Za-z]{3,9} \d{1,2}, \d{4}")
MONEY = r"-?[\d,]+\.\d{2}"


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _segments(line: str, labels: re.Pattern) -> list[tuple[str, str]]:
    """(label, value) pairs on one line, split at box numbers and at known labels followed by a colon."""
    cuts = sorted({m.start() for m in BOX.finditer(line)} | {m.start() for m in labels.finditer(line)} | {0})
    parts = [line[a:b].strip() for a, b in zip(cuts, cuts[1:] + [len(line)])]
    out = []
    for part in parts:
        part = BOX.sub("", part, count=1).strip() if BOX.match(part) else part
        if ":" not in part:
            continue
        label, value = part.split(":", 1)
        out.append((label.strip().lower(), value.strip()))
    return out


def _first_date(raw: str | None) -> str | None:
    if not raw:
        return None
    m = DATE_TOKEN.search(raw)
    d = parse_date(m.group(0)) if m else parse_date(raw)
    return d.isoformat() if d else None


def _amount(raw: str | None) -> str | None:
    if not raw:
        return None
    m = re.search(MONEY, raw.replace(" ", ""))
    value = parse_money(m.group(0)) if m else None
    return str(value) if value is not None else None


def _name(value: str | None) -> str | None:
    """A company name without the address printed after it ('Harborlink LLC, 500 Ocean Gate ...')."""
    return re.split(r",\s*(?=\d)", value)[0].strip() if value else None


def _pairs(lines: list[str], labels: re.Pattern) -> list[tuple[str, str]]:
    return [pair for ln in lines for pair in _segments(ln, labels)]


def _find(pairs: list[tuple[str, str]], pattern: str, *, skip: str | None = None) -> str | None:
    rx, no = re.compile(pattern, re.I), re.compile(skip, re.I) if skip else None
    for label, value in pairs:
        if rx.search(label) and value and not (no and no.search(label)):
            return value
    return None


# --------------------------------------------------------------------------- customs entry

ENTRY_LABELS = re.compile(
    r"(?:filer code\s*/\s*)?entry (?:no|number)\.?\s*:|entry type\s*:|summary date\s*:|entry date\s*:|import date\s*:|"
    r"port code\s*:|port of entry\s*:|b/l(?: or awb)? (?:no|number)\.?\s*:|country of origin\s*:|exchange rate\s*:|"
    r"invoice currency\s*:|currency\s*:|declaration (?:no|number|date)\.?\s*:|customs office\s*:|importer\s*:|"
    r"declarant\s*:|broker\s*:|transport document[^:]*:", re.I)
HTS = r"\d{4}\.?\d{2}(?:\.?\d{2,4})?"
RATE = r"free|\d+(?:\.\d+)?\s*%|[\d.]+\s*¢\s*/\s*[a-z]+(?:\s*\+\s*\d+(?:\.\d+)?\s*%)?"
ENTRY_ROW = re.compile(
    rf"^(?:(?P<no>\d{{1,3}})\s+)?(?P<hts>{HTS})\s+(?P<desc>.*?)\s*(?P<value>{MONEY})?\s+(?P<rate>{RATE})\s+"
    rf"(?P<duty>{MONEY})$", re.I)
FEE_ROW = re.compile(rf"^(?P<code>\d{{3}})\s+(?P<name>[A-Za-z][A-Za-z .&/-]+?)\s*:?\s*(?P<amount>{MONEY})$")


def customs_entry_rules(text: str) -> RuleResult:
    lines = _lines(text)
    pairs = _pairs(lines, ENTRY_LABELS)
    r = RuleResult()

    number = _find(pairs, r"entry (?:no|number)|declaration (?:no|number)|\bmrn\b")
    number = (number or "").split()[0] if number else None
    if not number:
        m = US_ENTRY.search(text.upper())
        number = m.group(0) if m else None
        r.put("entry_number", number, PATTERN_CONF)
    else:
        r.put("entry_number", number, LABEL_CONF)
    r.put("entry_type", _find(pairs, r"entry type|declaration type"), LABEL_CONF)
    r.put("entry_date", _first_date(_find(pairs, r"^(?:entry date|date of entry|declaration date|date of acceptance|"
                                                  r"acceptance date)")), LABEL_CONF)
    r.put("import_date", _first_date(_find(pairs, r"^(?:import date|date of import|arrival date)")), LABEL_CONF)
    r.put("port_of_entry", _find(pairs, r"^(?:port of entry|port code|customs office|office of entry)"), LABEL_CONF)
    r.put("importer_name", _name(_find(pairs, r"importer(?: of record)?(?: name)?$|^importer")), LABEL_CONF)
    r.put("broker_name", _name(_find(pairs, r"broker|declarant|filer name")), LABEL_CONF)
    bl = _find(pairs, r"b/l|bill of lading|transport document")
    r.put("bl_number", bl.split()[0] if bl else None, LABEL_CONF)
    r.put("container_numbers", find_containers(text), LABEL_CONF if re.search(r"container", text, re.I) else PATTERN_CONF)
    r.put("country_of_origin", _find(pairs, r"country of origin|origin country"), LABEL_CONF)
    cur = _find(pairs, r"^currency")
    r.put("currency", cur[:3].upper() if cur and re.match(r"[A-Za-z]{3}\b", cur) else _currency(lines, text), LABEL_CONF)
    inv_cur = _find(pairs, r"invoice currency")
    r.put("invoice_currency", inv_cur[:3].upper() if inv_cur and re.match(r"[A-Za-z]{3}\b", inv_cur) else None,
          LABEL_CONF)
    rate = _find(pairs, r"exchange rate|rate of exchange")
    m = re.search(r"\d+(?:\.\d+)?", rate or "")
    r.put("exchange_rate", m.group(0) if m else None, LABEL_CONF)

    rows = _entry_lines(lines)
    r.put("entry_lines", rows, LABEL_CONF)
    totals = _entry_totals(lines)
    for name, value in totals.items():
        r.put(name, value, LABEL_CONF)
    return r


def _entry_lines(lines: list[str]) -> list[dict]:
    rows, inside = [], False
    for line in lines:
        low = line.lower()
        if not inside:
            inside = ("hts" in low or "tariff" in low or "commodity code" in low) and "duty" in low
            continue
        if re.match(r"^(?:\d{1,2}\.\s*)?(?:total|other fee|duty summary|block)", low):
            break
        m = ENTRY_ROW.match(line)
        if not m:
            continue
        rate = re.sub(r"\s+", "", m["rate"]) if m["rate"].lower() != "free" else "Free"
        rows.append({k: v for k, v in {
            "line_number": m["no"], "hts_code": m["hts"], "description": m["desc"].strip() or None,
            "entered_value": str(parse_money(m["value"])) if m["value"] else None, "duty_rate": rate,
            "duty_amount": str(parse_money(m["duty"])),
        }.items() if v not in (None, "")})
    return rows


TOTAL_LABELS = [
    ("total_entered_value", r"total entered value|entered value total|total customs value|customs value total"),
    ("total_duty", r"^(?:\d{1,2}\.\s*)?(?:total duty|duty total|total import duty|duty)\s*:"),
    ("merchandise_processing_fee", r"merchandise processing fee|\bmpf\b"),
    ("harbor_maintenance_fee", r"harbou?r maintenance fee|\bhmf\b"),
    ("total_duty_and_fees", r"^(?:\d{1,2}\.\s*)?(?:total duty(?:, taxes)? and fees|total duties and fees|"
                            r"total amount due|total payable|total)\s*:"),
]


def _entry_totals(lines: list[str]) -> dict:
    out: dict[str, str] = {}
    other = None
    for line in lines:
        for seg in re.split(r"(?<=\d)\s+(?=\d{1,2}\.\s+[A-Za-z])", line):  # '37. Duty: 1.00 38. Tax: 0.00'
            seg = seg.strip()
            low = seg.lower()
            for name, pattern in TOTAL_LABELS:
                if name not in out and re.search(pattern, low) and ":" in seg:
                    value = _amount(seg.split(":", 1)[1])
                    if value is not None:
                        out[name] = value
                    break
            else:
                if re.match(r"^(?:\d{1,2}\.\s*)?(?:tax|ir tax|other fees?)\s*:", low):
                    value = _amount(seg.split(":", 1)[1])
                    if value is not None:
                        other = (other or 0) + float(value)
                    continue
                m = FEE_ROW.match(seg)  # 'Other fee summary' rows with a class code: 056 Cotton Fee 1.20
                if m and m["code"] not in {"499", "501"}:
                    other = (other or 0) + float(parse_money(m["amount"]))
    if other is not None:
        out["other_fees"] = f"{other:.2f}"
    return out


# --------------------------------------------------------------------------- arrival notice

NOTICE_LABELS = re.compile(
    r"notice date\s*:|date of notice\s*:|b/l (?:no|number)\.?\s*:|bill of lading (?:no|number)\.?\s*:|"
    r"vessel\s*/\s*voyage\s*:|vessel\s*:|voyage\s*:|port of discharge\s*:|terminal\s*:|\beta\s*:|\bata\s*:|"
    r"estimated (?:time of )?arrival\s*:|actual arrival\s*:|discharge date\s*:|discharged\s*:|"
    r"(?:demurrage|detention|terminal|port|storage|per diem|equipment)\s+free (?:days|time)\s*:|"
    r"(?:demurrage\s+)?last free day\s*:|empty return by\s*:|return empty by\s*:|"
    r"detention last free day\s*:|currency\s*:", re.I)
CALENDAR = re.compile(r"calendar days|including (?:saturdays|weekends)|incl\.? weekends|7 days a week", re.I)
WORKING = re.compile(r"working days|business days|excluding (?:saturdays|weekends)|excl\.? weekends|"
                     r"weekends and (?:public )?holidays (?:are )?(?:not counted|excluded)|monday (?:to|-) friday", re.I)


def arrival_notice_rules(text: str) -> RuleResult:
    lines = _lines(text)
    pairs = _pairs(lines, NOTICE_LABELS)
    r = RuleResult()
    cleaned = [re.sub(r"\b(?:arrival notice|notice of arrival|delivery order|cargo arrival notice)\b", "", ln,
                      flags=re.I).strip(" -:|") or ln for ln in lines]
    name, conf = _company(cleaned)
    r.put("carrier_name", name, conf)
    r.put("notice_date", _first_date(_find(pairs, r"^(?:notice date|date of notice|date)$")), LABEL_CONF)
    bl = _find(pairs, r"b/l|bill of lading")
    r.put("bl_number", bl.split()[0] if bl else None, LABEL_CONF)
    vv = _find(pairs, r"^vessel\s*/\s*voyage$") or " / ".join(
        v for v in (_find(pairs, r"^vessel$"), _find(pairs, r"^voyage$")) if v) or None
    r.put("vessel_voyage", vv, LABEL_CONF)
    r.put("port_of_discharge", _find(pairs, r"port of discharge"), LABEL_CONF)
    r.put("terminal", _find(pairs, r"^terminal$"), LABEL_CONF)
    r.put("estimated_arrival_date", _first_date(_find(pairs, r"^eta$|estimated (?:time of )?arrival")), LABEL_CONF)
    r.put("actual_arrival_date", _first_date(_find(pairs, r"^ata$|actual arrival")), LABEL_CONF)
    r.put("discharge_date", _first_date(_find(pairs, r"discharge date|^discharged$")), LABEL_CONF)
    for name, pattern in (("demurrage_free_days", r"(?:demurrage|terminal|port|storage) free"),
                          ("detention_free_days", r"(?:detention|per diem|equipment) free")):
        m = re.search(r"\d+", _find(pairs, pattern) or "")
        r.put(name, int(m.group(0)) if m else None, LABEL_CONF)
    basis = CALENDAR.search(text) or WORKING.search(text)
    r.put("free_time_basis", basis.group(0) if basis else None, LABEL_CONF)
    r.put("demurrage_last_free_day", _first_date(_find(pairs, r"last free day", skip=r"detention")), LABEL_CONF)
    r.put("detention_last_free_day", _first_date(_find(pairs, r"empty return by|return empty by|detention last free")),
          LABEL_CONF)
    r.put("container_numbers", find_containers(text), LABEL_CONF if re.search(r"container", text, re.I) else PATTERN_CONF)
    r.put("container_dates", _container_dates(lines), LABEL_CONF)
    cur = _find(pairs, r"^currency$")
    r.put("currency", cur[:3].upper() if cur and re.match(r"[A-Za-z]{3}\b", cur) else _currency(lines, text), LABEL_CONF)
    r.put("line_items", _line_items(lines), LABEL_CONF)
    r.put("total_amount", _total(lines), LABEL_CONF)
    return r


COLUMN_WORDS = [
    ("discharge_date", r"discharg"),
    ("demurrage_last_free_day", r"last free day|\blfd\b|pick ?up by|free until"),
    ("detention_last_free_day", r"empty return|return by|return empty|detention"),
]


def _container_dates(lines: list[str]) -> list[dict]:
    """Rows of the container table: the container number and the dates in the order of the header's columns."""
    rows, columns = [], None
    for line in lines:
        low = line.lower()
        if columns is None:
            if "container" in low and any(re.search(p, low) for _, p in COLUMN_WORDS):
                found = []
                for name, pattern in COLUMN_WORDS:
                    m = re.search(pattern, low)
                    if m:
                        found.append((m.start(), name))
                columns = [name for _, name in sorted(found)]
            continue
        boxes = find_containers(line)
        if not boxes:
            if rows and not re.search(r"\d", line):
                break
            continue
        dates = [parse_date(m.group(0)) for m in DATE_TOKEN.finditer(line)]
        dates = [d for d in dates if isinstance(d, date)]
        row = {"container_number": boxes[0]}
        for name, d in zip(columns, dates):
            row[name] = d.isoformat()
        rows.append(row)
    return rows
