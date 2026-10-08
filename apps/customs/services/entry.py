"""Reading the numbers of a customs entry: duty rates, tariff codes, entry numbers, and the duty each line
should carry. Plain code shared by the checks, the review screen and the landed cost charges."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings

from apps.documents.services.normalize import norm_ref

CENT = Decimal("0.01")
# A US entry number: 3-character filer code, 7-digit serial, 1 check digit (ABC-1234567-8).
US_ENTRY = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{3})[\s-]?(\d{7})[\s-]?(\d)(?![A-Z0-9])")
_PERCENT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%?\s*$")


def dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").strip()).quantize(CENT)
    except (InvalidOperation, ValueError):
        return None


def day_text(d) -> str:
    """4 Apr 2026 (no leading zero, on every platform)."""
    return f"{d.day} {d:%b %Y}" if d else "–"


def money(amount: Decimal | None, currency: str = "") -> str:
    if amount is None:
        return "–"
    return f"{currency} {amount:,.2f}".strip()


def duty_tolerance() -> Decimal:
    return Decimal(str(getattr(settings, "CUSTOMS_DUTY_TOLERANCE", "1.00")))


def value_tolerance_percent() -> Decimal:
    return Decimal(str(getattr(settings, "CUSTOMS_VALUE_TOLERANCE_PERCENT", "1.0")))


def parse_rate(raw) -> Decimal | None:
    """An ad valorem rate in percent: '4.9%' -> 4.9, 'Free' -> 0. None for a specific or compound rate
    ('2.4¢/kg', '4.9% + 1.2¢/kg'), which can't be checked from the entered value alone."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in {"free", "0", "0%", "0.0%", "nil", "exempt"}:
        return Decimal("0")
    m = _PERCENT.match(text)
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return None


def hts_digits(raw) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def is_chapter_99(raw) -> bool:
    return hts_digits(raw).startswith("99")


def normalize_entry_number(raw) -> str:
    return norm_ref(raw)


def us_entry_numbers(text: str) -> list[str]:
    """US entry numbers printed in a text (normalized, in order)."""
    out = []
    for m in US_ENTRY.finditer((text or "").upper()):
        if not re.search(r"[A-Z]", m.group(1)) and not m.group(0).count("-"):
            continue  # eleven bare digits are more likely a phone or account number than an entry
        number = "".join(m.groups())
        if number not in out:
            out.append(number)
    return out


def is_us_entry(data: dict) -> bool:
    """A US entry summary (CBP 7501): a US-format entry number, or US user fees on it."""
    number = str(data.get("entry_number") or "")
    if US_ENTRY.fullmatch(number.strip().upper()):
        return True
    return dec(data.get("merchandise_processing_fee")) is not None or dec(data.get("harbor_maintenance_fee")) is not None


@dataclass
class LineCheck:
    index: int
    number: str
    hts: str
    description: str
    value: Decimal | None          # entered value used for the duty (a Chapter 99 line uses its main line's value)
    own_value: bool
    rate_text: str
    rate: Decimal | None           # percent; None = specific or compound rate, or not printed
    stated: Decimal | None
    expected: Decimal | None

    @property
    def difference(self) -> Decimal | None:
        if self.stated is None or self.expected is None:
            return None
        return self.stated - self.expected

    @property
    def checkable(self) -> bool:
        return self.expected is not None and self.stated is not None

    @property
    def mismatch(self) -> bool:
        return self.checkable and abs(self.difference) > duty_tolerance()

    @property
    def label(self) -> str:
        return f"line {self.number}" if self.number else f"line {self.index + 1}"


def line_checks(data: dict) -> list[LineCheck]:
    """Each line's duty as printed and as rate x entered value. A Chapter 99 line (Section 301 and other
    additional duties) printed without its own value uses the value of the line above it."""
    out, last_value = [], None
    for i, row in enumerate(data.get("entry_lines") or []):
        if not isinstance(row, dict):
            continue
        hts = str(row.get("hts_code") or "").strip()
        own = dec(row.get("entered_value"))
        value = own
        if value is None and is_chapter_99(hts):
            value = last_value
        if own is not None and not is_chapter_99(hts):
            last_value = own
        rate = parse_rate(row.get("duty_rate"))
        expected = None
        if rate is not None and value is not None:
            expected = (value * rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
        out.append(LineCheck(i, str(row.get("line_number") or "").strip(), hts, str(row.get("description") or ""),
                             value, own is not None, str(row.get("duty_rate") or ""), rate,
                             dec(row.get("duty_amount")), expected))
    return out


def entered_value(data: dict) -> Decimal | None:
    """Total entered value: as printed, else the sum of the lines' own values."""
    total = dec(data.get("total_entered_value"))
    if total is not None:
        return total
    values = [c.value for c in line_checks(data) if c.own_value and c.value is not None]
    return sum(values, Decimal("0.00")) if values else None


def stated_fees(data: dict) -> Decimal:
    return sum((dec(data.get(k)) or Decimal("0.00") for k in
                ("merchandise_processing_fee", "harbor_maintenance_fee", "other_fees")), Decimal("0.00"))


def total_duty_and_fees(data: dict) -> Decimal | None:
    """What the entry costs: the printed total, else duty plus fees."""
    total = dec(data.get("total_duty_and_fees"))
    if total is not None:
        return total
    duty = dec(data.get("total_duty"))
    if duty is None:
        lines = [c.stated for c in line_checks(data) if c.stated is not None]
        duty = sum(lines, Decimal("0.00")) if lines else None
    if duty is None:
        return None
    return duty + stated_fees(data)


def entry_currency(data: dict) -> str:
    cur = str(data.get("currency") or "").strip().upper()[:3]
    if cur:
        return cur
    return "USD" if is_us_entry(data) else ""
