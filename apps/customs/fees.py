"""US customs user fees on formal entries: the Merchandise Processing Fee (MPF) and the Harbor Maintenance Fee (HMF).

MPF (class code 499) is 0.3464% of the entered value, but never less than a minimum or more than a maximum
(19 U.S.C. 58c(a)(9)(B), 19 CFR 24.23(b)(1)). Since 2018 the minimum and maximum change every US fiscal year on
1 October for inflation (FAST Act, 19 U.S.C. 58c(l)); CBP publishes the new amounts in the Federal Register each
summer. The table below lists every amount in force since 2018 with its source, checked on 3 October 2026.

When CBP publishes the next fiscal year, add a row here. Until a release ships with it, set CUSTOMS_MPF_TABLE
(JSON, see .env.example): rows with the same start date replace a row below, others are added. An invalid
CUSTOMS_MPF_TABLE is reported by `manage.py check` (apps/customs/checks.py) and is never ignored silently.

HMF (class code 501) is 0.125% of the value of commercial cargo unloaded at a US port, with no minimum or maximum
(26 U.S.C. 4461(c), 19 CFR 24.24). Override with CUSTOMS_HMF_RATE (percent).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings

CENT = Decimal("0.01")


@dataclass(frozen=True)
class MpfRate:
    start: date          # first entry date the amounts apply to
    percent: Decimal     # ad valorem rate, percent of entered value
    minimum: Decimal     # USD
    maximum: Decimal     # USD
    source: str


MPF_RATES: list[MpfRate] = [
    MpfRate(date(1, 1, 1), Decimal("0.3464"), Decimal("25.00"), Decimal("485.00"),
            "19 CFR 24.23(b)(1) before the FAST Act adjustments (until 31 Dec 2017)"),
    MpfRate(date(2018, 1, 1), Decimal("0.3464"), Decimal("25.67"), Decimal("497.99"),
            "CBP CSMS 17-000734, effective 1 Jan 2018"),
    MpfRate(date(2018, 10, 1), Decimal("0.3464"), Decimal("26.22"), Decimal("508.70"),
            "CBP notice for fiscal year 2019, effective 1 Oct 2018"),
    MpfRate(date(2019, 10, 1), Decimal("0.3464"), Decimal("26.79"), Decimal("519.76"),
            "CBP notice for fiscal year 2020, effective 1 Oct 2019"),
    MpfRate(date(2020, 10, 1), Decimal("0.3464"), Decimal("27.23"), Decimal("528.33"),
            "Federal Register 29 July 2020 (fiscal year 2021), effective 1 Oct 2020"),
    MpfRate(date(2021, 10, 1), Decimal("0.3464"), Decimal("27.75"), Decimal("538.40"),
            "86 FR 40576 (29 July 2021), fiscal year 2022, effective 1 Oct 2021"),
    MpfRate(date(2022, 10, 1), Decimal("0.3464"), Decimal("29.66"), Decimal("575.35"),
            "CBP notice for fiscal year 2023, effective 1 Oct 2022"),
    MpfRate(date(2023, 10, 1), Decimal("0.3464"), Decimal("31.67"), Decimal("614.35"),
            "88 FR 48900 (28 July 2023), fiscal year 2024, effective 1 Oct 2023"),
    MpfRate(date(2024, 10, 1), Decimal("0.3464"), Decimal("32.71"), Decimal("634.62"),
            "CBP notice for fiscal year 2025, effective 1 Oct 2024"),
    MpfRate(date(2025, 10, 1), Decimal("0.3464"), Decimal("33.58"), Decimal("651.50"),
            "90 FR 34665 (23 July 2025) and CSMS 65741993, fiscal year 2026, effective 1 Oct 2025"),
    MpfRate(date(2026, 10, 1), Decimal("0.3464"), Decimal("34.58"), Decimal("670.86"),
            "91 FR 46530 (31 July 2026), fiscal year 2027, effective 1 Oct 2026"),
]

HMF_PERCENT = Decimal("0.125")  # 26 U.S.C. 4461(c)(1)


class FeeTableError(ValueError):
    pass


def _dec(raw, what: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise FeeTableError(f"{what} {raw!r} is not a number") from None
    if value < 0:
        raise FeeTableError(f"{what} {raw!r} is negative")
    return value


def parse_override(raw: str) -> list[MpfRate]:
    """Rows from CUSTOMS_MPF_TABLE: a JSON list of {"from": "YYYY-MM-DD", "min": "...", "max": "..."} with an
    optional "rate" (percent, default 0.3464) and "source". Raises FeeTableError with what is wrong."""
    if not (raw or "").strip():
        return []
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FeeTableError(f"CUSTOMS_MPF_TABLE is not valid JSON ({e.msg} at character {e.pos})") from None
    if not isinstance(rows, list):
        raise FeeTableError("CUSTOMS_MPF_TABLE must be a JSON list of rows")
    out = []
    for n, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise FeeTableError(f"CUSTOMS_MPF_TABLE row {n} must be an object")
        try:
            start = date.fromisoformat(str(row.get("from", "")))
        except ValueError:
            raise FeeTableError(f"CUSTOMS_MPF_TABLE row {n}: \"from\" must be a date like 2027-10-01") from None
        minimum = _dec(row.get("min"), f"CUSTOMS_MPF_TABLE row {n}: min")
        maximum = _dec(row.get("max"), f"CUSTOMS_MPF_TABLE row {n}: max")
        percent = _dec(row.get("rate", "0.3464"), f"CUSTOMS_MPF_TABLE row {n}: rate")
        if minimum > maximum:
            raise FeeTableError(f"CUSTOMS_MPF_TABLE row {n}: min is larger than max")
        out.append(MpfRate(start, percent, minimum, maximum,
                           str(row.get("source") or "CUSTOMS_MPF_TABLE setting")))
    return out


def mpf_table() -> list[MpfRate]:
    """The built-in table with the CUSTOMS_MPF_TABLE rows applied, oldest first."""
    rows = {r.start: r for r in MPF_RATES}
    for r in parse_override(getattr(settings, "CUSTOMS_MPF_TABLE", "")):
        rows[r.start] = r
    return [rows[k] for k in sorted(rows)]


def mpf_rate_on(day: date) -> MpfRate:
    """The MPF rate, minimum and maximum in force on an entry date."""
    current = None
    for r in mpf_table():
        if r.start <= day:
            current = r
    return current or MPF_RATES[0]


def expected_mpf(entered_value: Decimal, day: date) -> tuple[Decimal, MpfRate]:
    """MPF for a formal entry: the ad valorem amount kept between the minimum and maximum of that date."""
    r = mpf_rate_on(day)
    raw = (entered_value * r.percent / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
    return min(max(raw, r.minimum), r.maximum), r


def hmf_percent() -> Decimal:
    raw = getattr(settings, "CUSTOMS_HMF_RATE", "") or ""
    return _dec(raw, "CUSTOMS_HMF_RATE") if str(raw).strip() else HMF_PERCENT


def expected_hmf(entered_value: Decimal) -> Decimal:
    return (entered_value * hmf_percent() / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
