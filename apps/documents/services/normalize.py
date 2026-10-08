"""Parsing helpers for dates, amounts and reference numbers found in documents."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache

# Order matters for ambiguous numeric dates: US style (month/day) is tried first.
DATE_FORMATS = [
    "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d/%m/%Y",
    "%d.%m.%Y", "%d-%b-%Y", "%d-%b-%y", "%Y/%m/%d",
]


def parse_date(raw) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    return _parse_date_text(str(raw).strip().rstrip(".,"))


@lru_cache(maxsize=8192)
def _parse_date_text(s: str) -> date | None:
    """Pure function of the text, and the same few thousand strings recur in a big report: parse each once."""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def date_variants(d: date) -> list[str]:
    """Ways a date may be printed; used to check an extracted date against the document text."""
    out = set()
    for fmt in DATE_FORMATS:
        try:
            out.add(d.strftime(fmt))
        except ValueError:
            pass
    out.add(f"{d.day} {d.strftime('%b %Y')}")  # 8 Mar 2026 (no leading zero)
    out.add(f"{d.month}/{d.day}/{d.year}")
    return sorted(out)


_MONEY_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")


def parse_money(raw) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        return Decimal(str(raw)).quantize(Decimal("0.01"))
    m = _MONEY_RE.search(str(raw).replace(" ", ""))
    if not m:
        return None
    try:
        return Decimal(m.group(0).replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def numbers_in_text(text: str) -> set[Decimal]:
    """Every number printed in the text, normalized (1,234.50 -> 1234.50)."""
    out = set()
    for m in _MONEY_RE.finditer(text or ""):
        try:
            out.add(Decimal(m.group(0).replace(",", "")).quantize(Decimal("0.01")))
        except InvalidOperation:
            continue
    return out


def norm_ref(raw) -> str:
    """Normalize a reference number for comparison: 'oslu 123456-7' -> 'OSLU1234567'."""
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())


def norm_text(raw) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())
