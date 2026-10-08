"""Reading an amount a person typed, so it can always be stored and read back.

An amount outside what the database column holds (more than 14 digits before the decimal point) used to be
accepted, and every page that later read it failed. Everything that parses a typed amount by hand uses this."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_AMOUNT = Decimal("99999999999999.99")   # fits DecimalField(max_digits=16, decimal_places=2)


class AmountError(ValueError):
    """kind is "number" (not a number at all) or "range" (a number too large to store)."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def parse_amount(raw, *, allow_negative: bool = False, limit: Decimal = MAX_AMOUNT) -> Decimal:
    """The typed text as an amount rounded to cents. Thousands commas and spaces are ignored; NaN, infinity,
    exponents and text are refused, and so are negatives unless allowed. `limit` is the largest amount the
    column behind it can hold."""
    text = str(raw if raw is not None else "").replace(",", "").replace(" ", "").strip()
    if not text or "e" in text.lower():
        raise AmountError("number")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise AmountError("number") from None
    if not value.is_finite() or (value < 0 and not allow_negative):
        raise AmountError("number")
    if abs(value) > limit:
        raise AmountError("range")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
