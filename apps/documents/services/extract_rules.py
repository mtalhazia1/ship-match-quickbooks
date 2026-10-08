"""Offline rule-based extractor.

Reads 'Label: value' lines with regular expressions. It needs no API key, so it powers
local development, tests and demos, and serves as a fallback when the LLM is down.
Each value carries a confidence that reflects how it was found.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from apps.shipments.services.containers import find_containers

from .normalize import parse_date, parse_money

LABEL_CONF = 0.92     # found next to an explicit label
PATTERN_CONF = 0.80   # found by shape only (e.g. a PO-like token anywhere)
GUESS_CONF = 0.70     # positional guess (e.g. first line = company name)

TITLE_WORDS = re.compile(
    r"\b(commercial invoice|freight invoice|ocean bill of lading|combined transport bill of lading|bill of lading|"
    r"invoice\s*-\s*freight\s*&\s*logistics\s*charges|tax invoice|invoice)\b",
    re.I,
)
COMPANY_SUFFIX = re.compile(
    r"(?<![A-Za-z])(co|ltd|llc|inc|corp|gmbh|a\.s|s\.a|plc|line|lines|shipping|logistics|company|limited)(?![A-Za-z])", re.I
)
AMOUNT = r"([\d,]+\.\d{2})"


@dataclass
class RuleResult:
    values: dict = field(default_factory=dict)
    confidence: dict = field(default_factory=dict)

    def put(self, name, value, conf):
        if value in (None, "", []):
            return
        self.values[name] = value
        self.confidence[name] = conf


# Readers for document types with a layout of their own, added by other apps from AppConfig.ready() with
# register_rules_reader(doc_type, fn); fn(text) returns a RuleResult (see apps/customs/services/readers.py).
RULE_READERS: dict = {}


def register_rules_reader(doc_type: str, reader) -> None:
    RULE_READERS[doc_type] = reader


def _label(lines: list[str], pattern: str, flags=re.I) -> str | None:
    rx = re.compile(pattern, flags)
    for line in lines:
        m = rx.search(line)
        if m:
            return m.group("v").strip()
    return None


def _company(lines: list[str]) -> tuple[str | None, float]:
    for line in lines[:4]:
        candidate = TITLE_WORDS.sub("", line).strip(" -:|")
        if len(candidate) < 3 or re.match(r"^\d", candidate) or ":" in candidate:
            continue
        return candidate, (LABEL_CONF if COMPANY_SUFFIX.search(candidate) else GUESS_CONF)
    return None, 0.0


def _po_numbers(lines: list[str], text: str) -> tuple[list[str], float]:
    raw = _label(lines, r"(?:p\.?o\.?\s*(?:no\.?|number|#)|purchase order|customer ref\s*/\s*po|shipper'?s ref\s*/\s*po)\s*[:#]?\s*(?P<v>.+)$")
    if raw:
        return [p.strip() for p in re.split(r"[,;]", raw) if p.strip()], LABEL_CONF
    found = re.findall(r"\bPO[-\s]?\d[\w-]*", text)
    return list(dict.fromkeys(found)), PATTERN_CONF


def _line_items(lines: list[str], goods_details: bool = False) -> list[dict]:
    """goods_details: commercial invoices; read SKU, HS code, weight and volume printed on a product line
    or on the detail line under it ("SKU BRK-100 | HS 7326.90 | 410.0 kg | 1.20 cbm")."""
    items, inside, last_goods = [], False, None
    goods = re.compile(rf"^(?P<d>.+?)\s+(?P<q>\d[\d,]*)\s+(?P<u>[\d,]+\.\d{{2,4}})\s+(?P<a>{AMOUNT[1:-1]})$")
    charge = re.compile(rf"^(?P<d>[A-Za-z(][^:]*?)\s+(?P<a>{AMOUNT[1:-1]})$")
    for line in lines:
        low = line.lower()
        if not inside:
            inside = "description" in low and ("amount" in low or "qty" in low)
            continue
        if re.search(r"\b(total|amount due|subtotal)\b", low):
            break
        m = goods.match(line.strip())
        if m:
            items.append({"description": m["d"].strip(), "quantity": m["q"].replace(",", ""),
                          "unit_price": str(parse_money(m["u"])), "amount": str(parse_money(m["a"]))})
            last_goods = items[-1]
            if goods_details:  # weights in a product name ("Cement 50 kg bag") are per unit, so only codes here
                _add_goods_details(last_goods, m["d"], keys=("sku", "hs_code"))
            continue
        m = charge.match(line.strip())
        if m:
            items.append({"description": m["d"].strip(), "amount": str(parse_money(m["a"]))})
            last_goods = None
            continue
        if goods_details and last_goods is not None and _add_goods_details(last_goods, line):
            last_goods = None  # one detail line per product
    return items


_SKU = re.compile(r"\b(?:sku|item\s*(?:no\.?|code|#)|part\s*(?:no\.?|#)|art(?:icle)?\.?\s*(?:no\.?|#))\s*[:#]?\s*"
                  r"(?P<v>[A-Z0-9][A-Z0-9./-]{1,30})", re.I)
_HS = re.compile(r"\b(?:hs|hts)(?:\s*code)?\s*[:#]?\s*(?P<v>\d{4}(?:[.\s]?\d{2}){0,3})\b", re.I)
_KG = re.compile(r"(?P<v>\d[\d,]*(?:\.\d+)?)\s*(?:kgs?|kilograms?)\b", re.I)
_CBM = re.compile(r"(?P<v>\d[\d,]*(?:\.\d+)?)\s*(?:cbm|m3|m³|cubic met(?:er|re)s?)(?![a-z])", re.I)
_DETAILS = (("sku", _SKU, str), ("hs_code", _HS, lambda v: re.sub(r"\s", "", v)),
            ("weight_kg", _KG, lambda v: v.replace(",", "")), ("volume_cbm", _CBM, lambda v: v.replace(",", "")))


def _add_goods_details(item: dict, text: str, keys=("sku", "hs_code", "weight_kg", "volume_cbm")) -> bool:
    """Add the SKU, HS code, weight and volume found in `text` to a product line. True if any was found."""
    found = False
    for key, rx, clean in _DETAILS:
        m = rx.search(text or "") if key in keys else None
        if m and key not in item:
            item[key] = clean(m["v"]).strip()
            found = True
    return found


def _total(lines: list[str]) -> str | None:
    rx = re.compile(rf"\b(total due|amount due|grand total|total amount|total)\b[^:\n]*:\s*(?:[A-Z]{{3}}\s*)?{AMOUNT}", re.I)
    found = None
    for line in lines:
        m = rx.search(line)
        if m and "subtotal" not in line.lower():
            found = m.group(2)
    return str(parse_money(found)) if found else None


def _currency(lines: list[str], text: str) -> str | None:
    cur = _label(lines, r"^currency\s*:\s*(?P<v>[A-Z]{3})\b")
    if cur:
        return cur
    m = re.search(r"\((USD|EUR|GBP|CNY|AED|SAR|PKR|INR|JPY|CAD|AUD|TRY|VND)\)", text)
    return m.group(1) if m else None


def _date(lines: list[str], pattern: str) -> str | None:
    raw = _label(lines, pattern)
    d = parse_date(raw) if raw else None
    return d.isoformat() if d else None


def extract_rules(doc_type: str, text: str) -> RuleResult:
    if doc_type in RULE_READERS:
        return RULE_READERS[doc_type](text)
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    r = RuleResult()
    containers = find_containers(text)
    r.put("container_numbers", containers, LABEL_CONF if re.search(r"container", text, re.I) else PATTERN_CONF)
    pos, po_conf = _po_numbers(lines, text)
    r.put("po_numbers", pos, po_conf)
    bl = _label(lines, r"(?:b/l\s*(?:no\.?|number)|bill of lading\s*(?:no\.?|number)|\bmbl|\bhbl)\s*[:#]?\s*(?P<v>[A-Z0-9][A-Z0-9-]{5,})")
    r.put("bl_number", bl, LABEL_CONF)

    if doc_type == "bill_of_lading":
        name, conf = _company(lines)
        r.put("carrier_name", name, conf)
        r.put("issue_date", _date(lines, r"^(?:date of issue|issue date|place and date of issue)\s*:\s*(?P<v>.+)$"), LABEL_CONF)
        r.put("shipper", _label(lines, r"^shipper\s*:\s*(?P<v>.+)$"), LABEL_CONF)
        r.put("consignee", _label(lines, r"^consignee\s*:\s*(?P<v>.+)$"), LABEL_CONF)
        r.put("port_of_loading", _label(lines, r"^port of loading\s*:\s*(?P<v>.+)$"), LABEL_CONF)
        r.put("port_of_discharge", _label(lines, r"^port of discharge\s*:\s*(?P<v>.+)$"), LABEL_CONF)
        r.put("vessel_voyage", _label(lines, r"^vessel\s*/\s*voyage\s*:\s*(?P<v>.+)$"), LABEL_CONF)
        return r

    name, conf = _company(lines)
    r.put("vendor_name", name, conf)
    r.put("invoice_number", _label(lines, r"invoice\s*(?:no\.?|number|#)\s*[:#]?\s*(?P<v>[A-Z0-9][A-Z0-9/-]+)"), LABEL_CONF)
    r.put("invoice_date", _date(lines, r"^(?:invoice date|date|dated|issued)\s*:\s*(?P<v>.+)$"), LABEL_CONF)
    r.put("currency", _currency(lines, text), LABEL_CONF)
    r.put("line_items", _line_items(lines, goods_details=doc_type == "commercial_invoice"), LABEL_CONF)
    r.put("total_amount", _total(lines), LABEL_CONF)
    if doc_type == "freight_invoice":
        r.put("due_date", _date(lines, r"^(?:due date|payment due|due)\s*:\s*(?P<v>.+)$"), LABEL_CONF)
    return r
