"""ROI calculator math, shared by the /roi/ page (server side) and mirrored in static/js/roi.js.

Every number comes from the inputs; nothing here is an industry statistic. The defaults are
example values for a first look and are labelled as such on the page.

    hours saved per month     = invoices per month x (minutes per invoice today
                                - minutes per invoice with ShipMatch) / 60
    labor saved per year      = hours saved per month x 12 x loaded hourly cost
    overcharges caught / year = invoices per month x 12 x share with errors x average overcharge
    ShipMatch cost per year   = monthly price x 12
    net annual benefit        = labor saved + overcharges caught - ShipMatch cost
    payback in months         = ShipMatch cost per year / (monthly labor saved + monthly overcharges caught)
                                (how many months of savings pay for a year of ShipMatch)

Assumes every overcharge caught is not paid (the vendor corrects or credits it).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    default: Decimal
    lo: Decimal
    hi: Decimal
    step: str = "1"
    unit: str = ""
    hint: str = ""
    integer: bool = False


FIELDS = [
    Field("invoices", "Freight invoices per month", Decimal("400"), Decimal("0"), Decimal("1000000"),
          integer=True, hint="Invoices from forwarders, carriers, truckers and brokers."),
    Field("minutes_today", "Minutes per invoice today", Decimal("12"), Decimal("0"), Decimal("600"), step="0.5",
          unit="min", hint="Opening, keying, matching to the shipment and checking against the quote."),
    Field("minutes_with", "Minutes per invoice with ShipMatch", Decimal("3"), Decimal("0"), Decimal("600"),
          step="0.5", unit="min", hint="Your estimate of the time left for review and approval."),
    Field("hourly_cost", "Loaded hourly cost of the team", Decimal("45"), Decimal("0"), Decimal("10000"),
          step="0.01", unit="money", hint="Salary plus benefits and overhead, per hour."),
    Field("error_share", "Share of invoices with an overcharge", Decimal("5"), Decimal("0"), Decimal("100"),
          step="0.1", unit="%", hint="From your own audits, or a sample of last month's invoices."),
    Field("avg_overcharge", "Average overcharge on those invoices", Decimal("150"), Decimal("0"),
          Decimal("1000000"), step="0.01", unit="money", hint="Charged above the quote, unapproved extras, duplicates."),
    Field("price", "ShipMatch price per month", Decimal("1000"), Decimal("0"), Decimal("1000000"), step="0.01",
          unit="money", hint="From your proposal."),
]
FIELD_BY_NAME = {f.name: f for f in FIELDS}
CURRENCIES = ["USD", "EUR", "GBP", "CAD", "AUD", "AED", "SAR", "PKR", "INR", "CNY", "SGD"]


@dataclass
class Result:
    hours_saved_month: Decimal
    labor_saved_year: Decimal
    overcharges_year: Decimal
    cost_year: Decimal
    net_year: Decimal
    payback_months: Decimal | None   # None = savings never cover the cost

    @property
    def gross_year(self) -> Decimal:
        return self.labor_saved_year + self.overcharges_year


@dataclass
class Calculation:
    values: dict[str, Decimal] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    currency: str = "USD"
    result: Result | None = None


def _round(x: Decimal, places: str = "1") -> Decimal:
    return x.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def compute(v: dict[str, Decimal]) -> Result:
    invoices = v["invoices"]
    hours = invoices * (v["minutes_today"] - v["minutes_with"]) / Decimal("60")
    labor = hours * 12 * v["hourly_cost"]
    over = invoices * 12 * v["error_share"] / Decimal("100") * v["avg_overcharge"]
    cost = v["price"] * 12
    gross_month = (labor + over) / 12
    payback = _round(cost / gross_month, "0.1") if gross_month > 0 else None
    return Result(hours_saved_month=_round(hours, "0.1"), labor_saved_year=_round(labor), overcharges_year=_round(over),
                  cost_year=_round(cost), net_year=_round(labor + over - cost), payback_months=payback)


def calculate(params) -> Calculation:
    """Read inputs from a GET query (blank = example value), validate, compute."""
    calc = Calculation()
    cur = str(params.get("currency", "") or "USD").upper()
    calc.currency = cur if cur in CURRENCIES else "USD"
    for f in FIELDS:
        raw = str(params.get(f.name, "") or "").strip().replace(",", "")
        calc.raw[f.name] = raw
        if raw == "":
            calc.values[f.name] = f.default
            continue
        try:
            if len(raw) > 30:
                raise InvalidOperation
            value = Decimal(raw)
            # Plain numbers only: an exponent like 1E-999990 is a valid Decimal but breaks rounding.
            if not value.is_finite() or value.as_tuple().exponent < -6:
                raise InvalidOperation
            value = value.quantize(Decimal("0.000001"))
        except (InvalidOperation, ValueError):
            calc.errors[f.name] = f"Enter a number for “{f.label.lower()}”."
            continue
        if f.integer and value != value.to_integral_value():
            calc.errors[f.name] = "Enter a whole number of invoices."
            continue
        if not f.lo <= value <= f.hi:
            calc.errors[f.name] = f"Enter a value from {f.lo:,} to {f.hi:,}."
            continue
        calc.values[f.name] = value
    if "minutes_today" in calc.values and "minutes_with" in calc.values and not calc.errors.get("minutes_with"):
        if calc.values["minutes_with"] > calc.values["minutes_today"]:
            calc.errors["minutes_with"] = "Minutes with ShipMatch can't be more than minutes today."
    if not calc.errors:
        calc.result = compute(calc.values)
    return calc


def _money(cur: str, x: Decimal) -> str:
    return f"{cur} {x:,.0f}"


def formatted(calc: Calculation) -> dict[str, str]:
    """Display strings for the results (roi.js formats the same way)."""
    r, cur = calc.result, calc.currency
    if r is None:
        dash = "–"
        return {"net_year": dash, "payback": dash, "hours_saved_month": dash, "labor_saved_year": dash,
                "overcharges_year": dash, "cost_year": dash, "gross_year": dash,
                "payback_text": "Fix the highlighted numbers to see results."}
    if r.payback_months is None:
        payback, text = "Not reached", "At these numbers the savings don't cover the cost."
    else:
        payback = f"{r.payback_months:,.1f} months"
        text = f"Savings pay for a year of ShipMatch in {r.payback_months:,.1f} months."
    return {"net_year": _money(cur, r.net_year), "payback": payback, "payback_text": text,
            "hours_saved_month": f"{r.hours_saved_month:,.1f}", "labor_saved_year": _money(cur, r.labor_saved_year),
            "overcharges_year": _money(cur, r.overcharges_year), "cost_year": _money(cur, r.cost_year),
            "gross_year": _money(cur, r.gross_year)}
