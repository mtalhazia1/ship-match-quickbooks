"""Day-first or month-first: how a vendor prints numeric dates, learned from corrected dates.

The general parser (apps.documents.services.normalize.parse_date) reads 03/08/2026 as 8 March
(month first). A vendor that prints 3 August that way is corrected once, and from then on its
ambiguous dates are read day first.
"""
from __future__ import annotations

import re
from datetime import date, datetime

DMY, MDY = "dmy", "mdy"
NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})([/.-])(\d{1,2})\2(\d{4}|\d{2})(?!\d)")

_COMMON = ["%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d-%b-%y", "%Y/%m/%d"]
_FORMATS = {
    DMY: ["%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%y", "%d.%m.%y", "%m/%d/%Y"] + _COMMON,
    MDY: ["%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%d/%m/%Y", "%d.%m.%Y"] + _COMMON,
}


def parse_with(raw, fmt: str) -> date | None:
    """Parse a printed date, trying the vendor's order first for numeric dates."""
    if raw is None:
        return None
    s = str(raw).strip().rstrip(".,")
    for f in _FORMATS.get(fmt, _FORMATS[MDY]):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def _year(y: str) -> int:
    n = int(y)
    return n + 2000 if len(y) == 2 else n


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def vote(text: str, corrected: date) -> str:
    """Which order explains how `corrected` is printed in the text? '' when no printed date tells."""
    for m in NUMERIC_DATE.finditer(text or ""):
        a, b, y = int(m.group(1)), int(m.group(3)), _year(m.group(4))
        if a == b:
            continue
        if _safe(y, b, a) == corrected:
            return DMY
        if _safe(y, a, b) == corrected:
            return MDY
    return ""


def swap_if_day_first(text: str, value: str, fmt: str) -> str | None:
    """A month-first reading of an ambiguous printed date, re-read day first. None when nothing changes."""
    if fmt != DMY:
        return None
    try:
        current = date.fromisoformat(str(value))
    except ValueError:
        return None
    for m in NUMERIC_DATE.finditer(text or ""):
        a, b, y = int(m.group(1)), int(m.group(3)), _year(m.group(4))
        if a == b or a > 12 or b > 12:
            continue
        if _safe(y, a, b) == current:  # printed a/b was read as month a, day b
            swapped = _safe(y, b, a)
            return swapped.isoformat() if swapped else None
    return None
