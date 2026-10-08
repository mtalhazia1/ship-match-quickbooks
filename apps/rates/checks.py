"""Rate checks, run as part of shipment validation (registered in RatesConfig.ready()).

Codes raised (titles and guidance are in apps/shipments/labels.py):

  over_quote             error    A charge on a freight invoice costs more than the matching quote
                                  allows, beyond the organization's tolerance. A base charge that
                                  isn't in the quote at all counts as quoted at zero (unless the
                                  quote is all-in, see below). amount_at_risk = the excess.
  unapproved_accessorial warning  An extra charge (detention, demurrage, exam, ...) that is neither
                                  in the quote nor on the vendor's approved list
                                  (amount_at_risk = the whole charge), or that is above the quoted
                                  price or the approved cap (amount_at_risk = the excess).
  accessorial_unchecked  warning  An approved extra charge whose days or hours can't be read from
                                  the line, so its per-day cap can't be checked. No amount.
  no_quote               warning  The vendor has quotes on file but none fits this shipment's lane,
                                  date and equipment (or several could and the documents don't say
                                  which). Organizations can turn this off.
  quote_currency         warning  Quote and invoice are in different currencies and there's no
                                  exchange rate to compare them.

Only freight invoices are checked, and only from vendors with quotes or approved extra charges
on file (unless the organization asks to check every vendor's extra charges).

Tolerance: an overcharge is flagged when it is more than the larger of `tolerance_percent` of the
allowed amount and `tolerance_amount` (home currency, converted to the invoice currency when an
exchange rate exists). Equal to the tolerance is not flagged.

All-in quotes: base charges the quote doesn't list (BAF, CAF, ...) are added to ocean freight
before comparing, because an all-in rate includes them.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from apps.accounting.models import vendor_key
from apps.documents.models import Document
from apps.shipments.services.validation import ERROR, WARNING, IssueSpec

from . import charges
from .matching import QuoteMatch, ShipmentContext, context_for, find_quote
from .models import ApprovedAccessorial, Quote, QuoteCharge, RateSettings

CENT = Decimal("0.01")
UNIT_BASES = {QuoteCharge.Basis.KG, QuoteCharge.Basis.CBM, QuoteCharge.Basis.DAY, QuoteCharge.Basis.HOUR}


@dataclass
class Line:
    description: str
    amount: Decimal
    quantity: object
    code: str


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v)).quantize(CENT) if v not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _money(cur: str, amount: Decimal) -> str:
    return f"{cur} {amount:,.2f}"


def _num(x) -> str:
    x = Decimal(str(x))
    return f"{x:,.0f}" if x == x.to_integral_value() else f"{x:,.2f}".rstrip("0").rstrip(".")


def invoice_lines(org, data: dict) -> list[Line]:
    items = [i for i in (data.get("line_items") or []) if isinstance(i, dict)]
    descs = [str(i.get("description") or "").strip() for i in items]
    codes = charges.classify_many([d for d in descs if d], org=org) if any(descs) else {}
    out = []
    for item, desc in zip(items, descs):
        amount = _dec(item.get("amount"))
        if amount is None or amount == 0:
            continue
        out.append(Line(desc, amount, item.get("quantity"), codes.get(desc, "other") if desc else "other"))
    return out


class Converter:
    """Amounts between currencies through the organization's home-currency rates."""

    def __init__(self, org):
        self.org = org

    def convert(self, amount: Decimal, src: str, dst: str) -> Decimal | None:
        src, dst = (src or self.org.home_currency).upper(), (dst or self.org.home_currency).upper()
        if src == dst:
            return amount
        home = self.org.to_home(amount, src)
        if home is None:
            return None
        if dst == self.org.home_currency:
            return home
        one = self.org.to_home(Decimal("1"), dst)
        if not one:
            return None
        return (home / one).quantize(CENT)


# --------------------------------------------------------------------------- rule entry point


def check_rates(shipment, docs: list[Document]):
    invoices = [d for d in docs if d.doc_type == Document.DocType.FREIGHT_INVOICE]
    if not invoices:
        return
    cfg = RateSettings.for_org(shipment.organization)
    for doc in invoices:
        yield from check_invoice(shipment, docs, doc, cfg)


def check_invoice(shipment, docs, doc: Document, cfg: RateSettings):
    org = shipment.organization
    data = doc.data()
    vname = (data.get("vendor_name") or "").strip().rstrip(".")
    vk = vendor_key(vname)
    if not vk:
        return
    has_quotes = Quote.objects.filter(organization=org, vendor_key=vk, archived=False).exists()
    approvals = list(ApprovedAccessorial.objects.filter(organization=org, vendor_key=vk))
    if not has_quotes and not approvals and not cfg.check_unlisted_vendors:
        return
    lines = invoice_lines(org, data)
    cur = (data.get("currency") or org.home_currency).upper()
    ctx = context_for(shipment, docs, doc, data)
    match = find_quote(org, vk, ctx) if has_quotes else QuoteMatch("none_on_file")
    fx = Converter(org)
    fixed = fx.convert(cfg.tolerance_amount, org.home_currency, cur) or cfg.tolerance_amount
    c = _Check(doc, vname, cur, ctx, match, cfg, fx, fixed)

    if match.status in ("no_match", "ambiguous") and cfg.warn_no_quote:
        yield IssueSpec("no_quote", WARNING,
                        f"{doc.original_filename}: {vname} has {match.on_file} quote{'s' if match.on_file != 1 else ''} "
                        f"on file, but {match.reason}.", doc,
                        {"key": "no_quote", "reason": match.reason, "lane": ctx.lane, "equipment": ctx.equipment or "",
                         "candidates": [q.pk for q in match.candidates[:10]]})

    quote = match.quote
    if quote is not None and quote.currency != cur and fx.convert(Decimal("1"), quote.currency, cur) is None:
        yield IssueSpec("quote_currency", WARNING,
                        f"{doc.original_filename}: {quote.title} is in {quote.currency} and the invoice is in {cur}. "
                        f"Add an exchange rate for {quote.currency if quote.currency != org.home_currency else cur} "
                        "in Settings so the charges can be compared.", doc,
                        {"key": f"quote_currency:{quote.pk}", "quote_id": quote.pk})
        quote = None
        c.match = QuoteMatch("currency", on_file=match.on_file)

    base = [ln for ln in lines if not charges.is_accessorial(ln.code)]
    extras = [ln for ln in lines if charges.is_accessorial(ln.code)]
    if quote is not None:
        if lines:
            yield from c.base_charges(quote, base)
        elif _dec(data.get("total_amount")) is not None:
            yield from c.invoice_total(quote, _dec(data.get("total_amount")))
    yield from c.accessorials(quote, extras, approvals)


# --------------------------------------------------------------------------- the comparisons


class _Check:
    def __init__(self, doc, vname, cur, ctx: ShipmentContext, match: QuoteMatch, cfg, fx: Converter, fixed):
        self.doc, self.vname, self.cur, self.ctx, self.match = doc, vname, cur, ctx, match
        self.cfg, self.fx, self.fixed = cfg, fx, fixed

    # ---- helpers

    def containers(self) -> tuple[int, bool]:
        return (self.ctx.containers, False) if self.ctx.containers else (1, True)

    def expected(self, quoted: list[QuoteCharge], lines: list[Line]) -> tuple[Decimal | None, str]:
        """What the quote allows for these lines, in the quote's currency, and how it was worked out."""
        total, how = Decimal("0.00"), []
        for qc in quoted:
            if qc.basis == QuoteCharge.Basis.CONTAINER:
                n, assumed = self.containers()
                how.append(f"{n} container{'s' if n != 1 else ''}{' (assumed)' if assumed else ''} at {qc.amount:,.2f}")
            elif qc.basis in UNIT_BASES:
                unit = {"kg": "kg", "cbm": "cbm", "day": "day", "hour": "hour"}[qc.basis]
                counted = [charges.units_on_line(ln.description, ln.quantity,
                                                 unit if unit in ("day", "hour") else "qty").units for ln in lines]
                if not counted or any(u is None for u in counted):
                    return None, f"the invoice doesn't show how many {unit}{'s' if unit in ('day', 'hour') else ''}"
                n = Decimal(str(sum(counted)))
                how.append(f"{_num(n)} {unit}{'s' if unit in ('day', 'hour') and n != 1 else ''} at {qc.amount:,.2f}")
            else:
                n = 1
                how.append(f"{qc.amount:,.2f} {qc.get_basis_display().lower()}")
            total += (qc.amount * Decimal(str(n))).quantize(CENT)
        return total, ", ".join(how)

    def tolerance(self, allowed: Decimal) -> Decimal:
        return self.cfg.tolerance_for(allowed, fixed=self.fixed)

    def note(self) -> str:
        return f" Note: {'; '.join(self.match.assumed)}." if self.match.assumed else ""

    # ---- (a) base charges against the quote

    def base_charges(self, quote: Quote, lines: list[Line]):
        quoted: dict[str, list[QuoteCharge]] = defaultdict(list)
        for qc in quote.charges.all():
            quoted[qc.code].append(qc)
        groups: dict[str, list[Line]] = defaultdict(list)
        for ln in lines:
            code = ln.code
            if quote.all_in and code not in quoted and "ocean_freight" in quoted:
                code = "ocean_freight"  # an all-in rate includes surcharges it doesn't list
            groups[code].append(ln)
        for code, group in groups.items():
            actual = sum((ln.amount for ln in group), Decimal("0.00"))
            if code in quoted:
                allowed_q, how = self.expected(quoted[code], group)
                if allowed_q is None:
                    continue  # e.g. per-kg rate but no weight on the invoice: nothing reliable to compare
                allowed = self.fx.convert(allowed_q, quote.currency, self.cur)
                if allowed is None:
                    continue
                conv = f" ({_money(quote.currency, allowed_q)} converted)" if quote.currency != self.cur else ""
                detail = f"{quote.title} allows {_money(self.cur, allowed)}{conv} ({how})"
            else:
                allowed, how = Decimal("0.00"), "not in the quote"
                listed = ", ".join(sorted({charges.label(k).lower() for k in quoted})) or "no charges"
                detail = f"it is not in {quote.title}, which lists {listed}"
            excess = actual - allowed
            tol = self.tolerance(allowed)
            if excess > tol:
                yield IssueSpec(
                    "over_quote", ERROR,
                    f"{self.doc.original_filename}: {charges.label(code).lower()} charged {_money(self.cur, actual)}, "
                    f"but {detail}. Over by {_money(self.cur, excess)}.{self.note()}",
                    self.doc,
                    {"key": f"{code}:{actual}", "catch_key": code, "charge_code": code, "quote_id": quote.pk,
                     "quote": quote.title, "invoiced": str(actual), "allowed": str(allowed), "excess": str(excess),
                     "tolerance": str(tol), "how": how},
                    amount_at_risk=excess, currency=self.cur)

    def invoice_total(self, quote: Quote, total: Decimal):
        """No line items could be read: compare the invoice total with everything the quote allows."""
        allowed_q, how = self.expected(list(quote.charges.all()), [])
        if allowed_q is None or allowed_q == 0:
            return
        allowed = self.fx.convert(allowed_q, quote.currency, self.cur)
        if allowed is None:
            return
        excess = total - allowed
        tol = self.tolerance(allowed)
        if excess > tol:
            yield IssueSpec(
                "over_quote", ERROR,
                f"{self.doc.original_filename}: invoice total {_money(self.cur, total)}, but {quote.title} allows "
                f"{_money(self.cur, allowed)} in total. Over by {_money(self.cur, excess)}. The charge lines "
                f"couldn't be read, so check them against the quote.{self.note()}",
                self.doc,
                {"key": f"total:{total}", "catch_key": "total", "charge_code": "", "quote_id": quote.pk,
                 "quote": quote.title, "invoiced": str(total), "allowed": str(allowed), "excess": str(excess),
                 "tolerance": str(tol), "how": how},
                amount_at_risk=excess, currency=self.cur)

    # ---- (b) extra charges against the quote and the approved list

    def accessorials(self, quote: Quote | None, lines: list[Line], approvals: list[ApprovedAccessorial]):
        groups: dict[str, list[Line]] = defaultdict(list)
        for ln in lines:
            groups[ln.code].append(ln)
        quoted: dict[str, list[QuoteCharge]] = defaultdict(list)
        if quote is not None:
            for qc in quote.charges.all():
                quoted[qc.code].append(qc)
        day = self.ctx.ship_date
        for code, group in groups.items():
            actual = sum((ln.amount for ln in group), Decimal("0.00"))
            name = charges.label(code).lower() if code != "other" else f"“{group[0].description[:60]}”"
            base = {"charge_code": code, "invoiced": str(actual), "lines": [ln.description[:80] for ln in group]}
            if code in quoted:
                allowed_q, how = self.expected(quoted[code], group)
                allowed = self.fx.convert(allowed_q, quote.currency, self.cur) if allowed_q is not None else None
                if allowed is None:  # per-day / per-kg price, but the line doesn't show the count
                    yield self._unchecked(code, name, actual, f"{quote.title} prices it per unit and {how}")
                    continue
                yield from self._over_cap(code, name, actual, allowed, f"{quote.title} allows {_money(self.cur, allowed)} "
                                          f"({how})", {**base, "quote_id": quote.pk, "quote": quote.title})
                continue
            rules = sorted((a for a in approvals if a.code == code and a.is_valid_on(day)),
                           key=lambda a: (a.valid_from is not None, a.valid_from or day, a.pk), reverse=True)
            if rules:
                rule = rules[0]
                allowed_r, explain = self._allowed_by_rule(rule, group)
                if allowed_r is None:
                    yield self._unchecked(code, name, actual, explain)
                    continue
                allowed = self.fx.convert(allowed_r, rule.currency, self.cur)
                if allowed is None:
                    yield self._unchecked(code, name, actual, f"the approval is in {rule.currency} and there's no "
                                          "exchange rate to compare it")
                    continue
                yield from self._over_cap(code, name, actual, allowed, explain,
                                          {**base, "approval_id": rule.pk, "terms": rule.terms})
                continue
            where = f"{quote.title}" if quote is not None else "a quote"
            yield IssueSpec(
                "unapproved_accessorial", WARNING,
                f"{self.doc.original_filename}: {name} {_money(self.cur, actual)} is not in {where} or on the "
                f"approved extra charges for {self.vname}.",
                self.doc, {**base, "key": f"{code}:{actual}", "catch_key": code, "allowed": "0.00",
                           "excess": str(actual), **({"quote_id": quote.pk} if quote is not None else {})},
                amount_at_risk=actual, currency=self.cur)

    def _over_cap(self, code, name, actual, allowed, explain, data):
        excess = actual - allowed
        tol = self.tolerance(allowed)
        if excess > tol:
            yield IssueSpec(
                "unapproved_accessorial", WARNING,
                f"{self.doc.original_filename}: {name} charged {_money(self.cur, actual)}; {explain}. "
                f"Over by {_money(self.cur, excess)}.",
                self.doc, {**data, "key": f"{code}:{actual}", "catch_key": code, "allowed": str(allowed),
                           "excess": str(excess), "tolerance": str(tol)},
                amount_at_risk=excess, currency=self.cur)

    def _unchecked(self, code, name, actual, why) -> IssueSpec:
        return IssueSpec(
            "accessorial_unchecked", WARNING,
            f"{self.doc.original_filename}: {name} {_money(self.cur, actual)} is approved, but {why}. "
            "Check it against the vendor's supporting documents.",
            self.doc, {"key": f"{code}:{actual}", "charge_code": code, "invoiced": str(actual)})

    def _allowed_by_rule(self, rule: ApprovedAccessorial, lines: list[Line]) -> tuple[Decimal | None, str]:
        """Most the approval allows for these lines (in the approval's currency), with an explanation."""
        cur, unit = rule.currency, rule.unit
        words = {"day": ("day", "days", "a day"), "hour": ("hour", "hours", "an hour"),
                 "each": ("time", "times", "each time")}[unit]
        if rule.max_per_unit is None and rule.max_amount is None:
            return sum((ln.amount for ln in lines), Decimal("0.00")), "approved with no cap"
        total, parts = Decimal("0.00"), []
        if rule.max_per_unit is not None:
            for ln in lines:
                u = charges.units_on_line(ln.description, ln.quantity, unit)
                if u.units is None:
                    if rule.max_amount is not None:
                        total = None
                        break
                    return None, (f"the line doesn't say how many {words[1]}, so the cap of "
                                  f"{_money(cur, rule.max_per_unit)} {words[2]} can't be checked")
                units = Decimal(str(u.units))
                billable = units if u.already_chargeable else max(Decimal("0"), units - rule.free_units)
                total += (billable * rule.max_per_unit).quantize(CENT)
                free = "" if u.already_chargeable or not rule.free_units else f" less {rule.free_units} free"
                parts.append(f"{_num(units)} {words[1] if units != 1 else words[0]}{free}")
        else:
            total = None
        if total is not None:
            explain = (f"approved {rule.terms}, so {_money(cur, total)} is allowed for {', '.join(parts)}")
            if rule.max_amount is not None and total > rule.max_amount:
                total = rule.max_amount
                explain = f"approved {rule.terms}, so {_money(cur, total)} is allowed"
        else:
            total = rule.max_amount
            explain = f"approved {rule.terms}, so {_money(cur, total)} is allowed"
        return total, explain
