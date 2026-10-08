"""Find the label a vendor prints before a value, and read a value after a known label.

Learning: when a reviewer types the correct value and that value is printed in the document,
the words just before it on the same line ("Ref No.:") are the vendor's label for the field.
Applying: on the vendor's next document, the value after that label is read for the field.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from apps.documents.services.normalize import date_variants, parse_money
from apps.shipments.services.containers import find_containers

from . import dates

MAX_LABEL_CHARS = 40
MAX_LABEL_WORDS = 5

DATE_FIELDS = {"invoice_date", "issue_date", "due_date"}
AMOUNT_FIELDS = {"total_amount"}
REF_FIELDS = {"invoice_number", "bl_number"}
LIST_FIELDS = {"po_numbers", "container_numbers"}
CODE_FIELDS = {"currency"}
NAME_FIELDS = {"vendor_name", "carrier_name", "shipper", "consignee", "port_of_loading", "port_of_discharge",
               "vessel_voyage"}
LEARNABLE = DATE_FIELDS | AMOUNT_FIELDS | REF_FIELDS | LIST_FIELDS | CODE_FIELDS | NAME_FIELDS

_LABEL_WORD = re.compile(r"^[A-Za-z][A-Za-z./#&()'-]*$|^[A-Za-z./#&()'-]*[A-Za-z][A-Za-z./#&()'-]*$")
_NEXT_LABEL = re.compile(r"\s{2,}|\s+(?=[A-Z][A-Za-z./# ]{1,24}:\s)")


# --------------------------------------------------------------------------- learning a label


def _printed_forms(name: str, value) -> list[str]:
    """How a corrected value may be printed in the document."""
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        value = value[0]
    s = str(value).strip()
    if name in DATE_FIELDS:
        try:
            d = date.fromisoformat(s)
        except ValueError:
            return [s]
        forms = set(date_variants(d))
        forms |= {d.strftime("%d/%m/%Y"), d.strftime("%d.%m.%Y"), d.strftime("%d-%m-%Y"), d.strftime("%m-%d-%Y"),
                  f"{d.day}/{d.month}/{d.year}", f"{d.day}.{d.month}.{d.year}"}
        return sorted(forms, key=len, reverse=True)
    if name in AMOUNT_FIELDS:
        try:
            amount = Decimal(s.replace(",", ""))
        except InvalidOperation:
            return [s]
        return [f"{amount:,.2f}", f"{amount:.2f}"]
    return [s]


def _label_from_prefix(prefix: str) -> str:
    """'Date: 03/08/2026  Ref No.:' -> 'Ref No.'"""
    p = prefix.rstrip()
    explicit = p.endswith((":", "#"))
    p = p.rstrip(" :#-")
    if ":" in p:
        p = p.rsplit(":", 1)[1]
    words = []
    for token in reversed(p.split()):
        if len(words) >= MAX_LABEL_WORDS or not _LABEL_WORD.match(token):
            break
        words.insert(0, token)
    label = " ".join(words).strip(" -")
    if sum(ch.isalpha() for ch in label) < 2 or len(label) > MAX_LABEL_CHARS:
        return ""
    if not explicit and len(words) > 3:  # a long run of words without ':' is a sentence, not a label
        return ""
    return label


def _find_value(line: str, form: str) -> int:
    """Start of `form` in `line` as a whole token (case-insensitive), or -1."""
    rx = re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(re.escape(part) for part in form.split()) + r"(?![A-Za-z0-9])",
                    re.I)
    m = rx.search(line)
    return m.start() if m else -1


def label_before(text: str, name: str, value) -> str:
    """The label printed before `value` in the document, or '' if the value isn't printed with a label."""
    if name not in LEARNABLE:
        return ""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    best, best_score = "", -1
    for form in _printed_forms(name, value):
        if len(form) < 2:
            continue
        for i, line in enumerate(lines):
            pos = _find_value(line, form)
            if pos < 0:
                continue
            prefix = line[:pos]
            label = _label_from_prefix(prefix)
            score = 2 if prefix.rstrip().endswith((":", "#")) else 1
            if not prefix.strip() and i > 0:  # label on the line above, value below it
                above = lines[i - 1]
                label = _label_from_prefix(above) if above.rstrip().endswith((":", "#")) else ""
                score = 1
            if label and score > best_score:
                best, best_score = label, score
                if score == 2:
                    return best
    return best


# --------------------------------------------------------------------------- reading after a label


def _label_regex(label: str) -> re.Pattern:
    words = [re.escape(w) for w in label.split()]
    return re.compile(r"(?<![A-Za-z0-9])" + r"\s*".join(words) + r"\s*[:#]?\s*", re.I)


def _inside_longer_label(before: str) -> bool:
    """'Due Date' contains 'Date': a learned label never starts right after another label word."""
    tokens = before.split()
    return bool(tokens) and not tokens[-1].endswith((":", "#")) and bool(_LABEL_WORD.match(tokens[-1]))


def _cut_next_label(rest: str) -> str:
    return _NEXT_LABEL.split(rest.strip(), maxsplit=1)[0].strip()


def _parse(name: str, raw: str, date_format: str):
    raw = raw.strip()
    if not raw:
        return None
    if name in DATE_FIELDS:
        tokens = raw.split()
        for n in range(min(4, len(tokens)), 0, -1):
            d = dates.parse_with(" ".join(tokens[:n]), date_format or dates.MDY)
            if d:
                return d.isoformat()
        return None
    if name in AMOUNT_FIELDS:
        m = re.match(r"^(?:[A-Z]{3}\s*)?[$€£]?\s*([\d,]+\.\d{2})\b", raw)
        return str(parse_money(m.group(1))) if m else None
    if name in REF_FIELDS:
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9/._-]{2,})", raw)
        return m.group(1).rstrip(".") if m else None
    if name in CODE_FIELDS:
        m = re.match(r"^([A-Z]{3})\b", raw)
        return m.group(1) if m else None
    if name == "container_numbers":
        return find_containers(raw) or None
    if name in LIST_FIELDS:
        parts = [p.strip() for p in re.split(r"[,;]", _cut_next_label(raw)) if p.strip()]
        parts = [p.split()[0] for p in parts if p.split()]
        return parts or None
    value = _cut_next_label(raw)
    return value[:200] if len(value) >= 2 else None


def value_after(text: str, label: str, name: str, date_format: str = ""):
    """Read the field's value printed after the vendor's label (same line, or the line below)."""
    if not label or name not in LEARNABLE:
        return None
    rx = _label_regex(label)
    lines = [ln.strip() for ln in (text or "").splitlines()]
    hits = [(m.start() > 0, i, m) for i, line in enumerate(lines) for m in rx.finditer(line)
            if not _inside_longer_label(line[:m.start()])]
    # A label that starts its line ("Date:") beats the same words inside a longer label ("Due Date:").
    for _, i, m in sorted(hits, key=lambda h: (h[0], h[1], h[2].start())):
        rest = lines[i][m.end():]
        value = _parse(name, rest, date_format)
        if value in (None, "", []) and not rest.strip() and i + 1 < len(lines):
            value = _parse(name, lines[i + 1], date_format)
        if value not in (None, "", []):
            return value
    return None
