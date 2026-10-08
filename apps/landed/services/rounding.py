"""Money splits that add up exactly (largest-remainder method), in whole cents.

`split(total, weights)` gives each weight its proportional part, rounded down, then hands the cents
left over to the parts that lost the most in rounding. The parts always add up to `total`, and each
part is at most one cent away from its exact share. Ties go to the larger weight, then to the earlier
position, so the same input always gives the same result.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

CENT = Decimal("0.01")


def to_cents(amount) -> int:
    return int((Decimal(str(amount)).quantize(CENT, rounding=ROUND_HALF_UP) * 100).to_integral_value())


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENT)


def split(total: int, weights) -> list[int]:
    """Split `total` cents in proportion to `weights` (each >= 0, at least one > 0)."""
    ws = [Fraction(str(w)) for w in weights]
    if not ws:
        raise ValueError("Nothing to split over")
    if any(w < 0 for w in ws):
        raise ValueError("Weights can't be negative")
    whole = sum(ws)
    if whole == 0:
        raise ValueError("All weights are zero")
    sign = -1 if total < 0 else 1
    t = abs(int(total))
    quotas = [t * w / whole for w in ws]
    parts = [q.numerator // q.denominator for q in quotas]
    left = t - sum(parts)
    order = sorted(range(len(ws)), key=lambda i: (-(quotas[i] - parts[i]), -ws[i], i))
    for i in order[:left]:
        parts[i] += 1
    return [sign * p for p in parts]


def split_amount(total: Decimal, weights) -> list[Decimal]:
    return [from_cents(c) for c in split(to_cents(total), weights)]


def split_table(columns: list[int], targets: list[int]) -> list[list[int]]:
    """Cents table parts[row][col] whose columns add up to `columns` and rows to `targets` exactly.

    Used for a manual split of a shared invoice: every invoice line (column) is split over the shipments
    (rows) in proportion to their shares, then single cents are moved inside the table until each
    shipment's lines add up to its share. Moving a cent within one column keeps that column's total.
    """
    if sum(columns) != sum(targets):
        raise ValueError("Rows and columns must have the same total")
    rows = len(targets)
    parts = [[0] * len(columns) for _ in range(rows)]
    if not any(targets):
        if any(columns):
            raise ValueError("Nothing to split over")
        return parts
    weights = [abs(t) for t in targets]
    for c, amount in enumerate(columns):
        for r, part in enumerate(split(amount, weights)):
            parts[r][c] = part
    deficit = [targets[r] - sum(parts[r]) for r in range(rows)]
    guard = sum(abs(d) for d in deficit) + 1
    while any(deficit) and guard > 0:
        guard -= 1
        give = next(r for r in range(rows) if deficit[r] < 0)   # has a cent too many
        take = next(r for r in range(rows) if deficit[r] > 0)   # is a cent short
        col = max(range(len(columns)), key=lambda c: (parts[give][c], -c))
        parts[give][col] -= 1
        parts[take][col] += 1
        deficit[give] += 1
        deficit[take] -= 1
    return parts
