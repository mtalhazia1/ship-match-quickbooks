"""Validation rules for customs entries and arrival notices, registered from CustomsConfig.ready().

Plain code, no AI (the optional AI tariff code check only reads opinions saved earlier, see hts_ai.py). Each issue
carries `amount_at_risk` when the entry asks for more money than the numbers on it justify, so the catch counts on
the Savings page. Amounts are in the entry's currency (USD for a US entry summary).
"""
from __future__ import annotations

import re
from decimal import Decimal

from apps.documents.models import Document
from apps.shipments.services.validation import ERROR, WARNING, IssueSpec

from ..fees import expected_hmf, expected_mpf, hmf_percent
from .entry import (
    day_text,
    dec,
    duty_tolerance,
    entered_value,
    entry_currency,
    hts_digits,
    is_chapter_99,
    is_us_entry,
    line_checks,
    money,
    normalize_entry_number,
    stated_fees,
    total_duty_and_fees,
    value_tolerance_percent,
)

CUSTOMS = Document.DocType.CUSTOMS_ENTRY
NOTICE = Document.DocType.ARRIVAL_NOTICE


def _entry_date(data: dict):
    from apps.documents.services.normalize import parse_date

    return parse_date(data.get("entry_date")) or parse_date(data.get("import_date"))


def _ev(targets: list[str], label: str) -> dict:
    return {"evidence": {"targets": targets, "label": label}}


# --------------------------------------------------------------------------- duty arithmetic


def check_duty_math(doc: Document, data: dict):
    """Each line's duty against its rate and entered value, the lines against the printed totals."""
    if doc.doc_type != CUSTOMS:
        return
    name, cur, tol = doc.original_filename, entry_currency(data), duty_tolerance()
    checks = line_checks(data)
    for c in checks:
        if not c.mismatch:
            continue
        diff = c.difference
        over = diff > 0
        base = "" if c.own_value else " (the value of the line above, as for every Chapter 99 line)"
        yield IssueSpec(
            "duty_line_mismatch", ERROR if over else WARNING,
            f"{name}: {c.label} ({c.hts or 'no tariff number'}) shows duty {money(c.stated, cur)}, but "
            f"{c.rate_text} of {money(c.value, cur)}{base} is {money(c.expected, cur)}",
            doc, {"key": str(c.index), "line": c.index, "stated": str(c.stated), "expected": str(c.expected),
                  **_ev([f"entry_lines={c.index}"], c.label)},
            amount_at_risk=diff if over else None, currency=cur)

    lines = [c.stated for c in checks if c.stated is not None]
    total = dec(data.get("total_duty"))
    if lines and total is not None:
        line_sum = sum(lines, Decimal("0.00"))
        if abs(total - line_sum) > tol:
            over = total > line_sum
            yield IssueSpec(
                "duty_total_mismatch", ERROR if over else WARNING,
                f"{name}: the lines' duty adds up to {money(line_sum, cur)} but the total duty is {money(total, cur)}",
                doc, {"line_sum": str(line_sum), "total": str(total),
                      **_ev(["total_duty", "entry_lines.amount"], "the total duty and the line duties")},
                amount_at_risk=(total - line_sum) if over else None, currency=cur)

    values = [c.value for c in checks if c.own_value and c.value is not None]
    total_value = dec(data.get("total_entered_value"))
    if values and total_value is not None:
        value_sum = sum(values, Decimal("0.00"))
        if abs(total_value - value_sum) > tol:
            yield IssueSpec(
                "entered_value_total_mismatch", WARNING,
                f"{name}: the lines' entered values add up to {money(value_sum, cur)} but the total entered value "
                f"is {money(total_value, cur)}", doc,
                {"line_sum": str(value_sum), "total": str(total_value),
                 **_ev(["total_entered_value"], "the total entered value")}, currency=cur)

    grand = dec(data.get("total_duty_and_fees"))
    duty = total if total is not None else (sum(lines, Decimal("0.00")) if lines else None)
    if grand is not None and duty is not None:
        expected = duty + stated_fees(data)
        if abs(grand - expected) > tol:
            over = grand > expected
            yield IssueSpec(
                "duty_fees_total_mismatch", ERROR if over else WARNING,
                f"{name}: duty {money(duty, cur)} plus fees {money(stated_fees(data), cur)} is {money(expected, cur)}, "
                f"but the entry's total is {money(grand, cur)}", doc,
                {"expected": str(expected), "total": str(grand),
                 **_ev(["total_duty_and_fees"], "the total duty and fees")},
                amount_at_risk=(grand - expected) if over else None, currency=cur)


# --------------------------------------------------------------------------- MPF and HMF


def check_user_fees(doc: Document, data: dict):
    """MPF between the minimum and maximum in force on the entry date; HMF at its rate. US entries only."""
    if doc.doc_type != CUSTOMS or not is_us_entry(data):
        return
    name, cur, tol = doc.original_filename, entry_currency(data), duty_tolerance()
    value, day = entered_value(data), _entry_date(data)
    mpf = dec(data.get("merchandise_processing_fee"))
    if mpf is not None and mpf > 0 and value is not None and day is not None:
        expected, r = expected_mpf(value, day)
        bounds = f"{money(r.minimum, cur)} to {money(r.maximum, cur)} for entries from {day_text(r.start)}" \
            if r.start.year > 1 else f"{money(r.minimum, cur)} to {money(r.maximum, cur)}"
        data_ = {"stated": str(mpf), "expected": str(expected), "minimum": str(r.minimum), "maximum": str(r.maximum),
                 "rate_from": r.start.isoformat(), "source": r.source,
                 **_ev(["merchandise_processing_fee"], "the merchandise processing fee")}
        if mpf > r.maximum + tol:
            yield IssueSpec("mpf_incorrect", ERROR,
                            f"{name}: merchandise processing fee {money(mpf, cur)} is above the maximum of "
                            f"{money(r.maximum, cur)} on {day_text(day)} ({bounds}); it should be {money(expected, cur)}",
                            doc, {**data_, "key": "above_max"}, amount_at_risk=mpf - expected, currency=cur)
        elif mpf < r.minimum - tol:
            yield IssueSpec("mpf_incorrect", WARNING,
                            f"{name}: merchandise processing fee {money(mpf, cur)} is below the minimum of "
                            f"{money(r.minimum, cur)} on {day_text(day)}; CBP may bill the difference "
                            f"({money(expected - mpf, cur)})", doc, {**data_, "key": "below_min"}, currency=cur)
        elif abs(mpf - expected) > tol:
            over = mpf > expected
            yield IssueSpec("mpf_incorrect", ERROR if over else WARNING,
                            f"{name}: merchandise processing fee {money(mpf, cur)}, but {r.percent}% of "
                            f"{money(value, cur)} within {bounds} is {money(expected, cur)}",
                            doc, {**data_, "key": "rate"}, amount_at_risk=(mpf - expected) if over else None,
                            currency=cur)
    hmf = dec(data.get("harbor_maintenance_fee"))
    if hmf is not None and hmf > 0 and value is not None:
        expected = expected_hmf(value)
        if abs(hmf - expected) > tol:
            over = hmf > expected
            yield IssueSpec("hmf_incorrect", ERROR if over else WARNING,
                            f"{name}: harbor maintenance fee {money(hmf, cur)}, but {hmf_percent()}% of "
                            f"{money(value, cur)} is {money(expected, cur)}", doc,
                            {"stated": str(hmf), "expected": str(expected),
                             **_ev(["harbor_maintenance_fee"], "the harbor maintenance fee")},
                            amount_at_risk=(hmf - expected) if over else None, currency=cur)


# --------------------------------------------------------------------------- tariff numbers


def check_hts_codes(doc: Document, data: dict):
    """US tariff numbers have 10 digits (8 for a Chapter 99 provision such as 9903.88.15); other countries'
    commodity codes have 6 to 10 digits."""
    if doc.doc_type != CUSTOMS:
        return
    us = is_us_entry(data)
    for c in line_checks(data):
        if not c.hts:
            continue
        digits = hts_digits(c.hts)
        if us:
            ok = len(digits) == 10 or (is_chapter_99(digits) and len(digits) == 8)
            rule = "US tariff numbers have 10 digits"
        else:
            ok = 6 <= len(digits) <= 10
            rule = "commodity codes have 6 to 10 digits"
        ok = ok and not re.search(r"[^\d.\s]", c.hts) and digits[:2] not in {"00", "77"}
        if not ok:
            yield IssueSpec("hts_code_format", ERROR,
                            f"{doc.original_filename}: {c.label} tariff number {c.hts} has {len(digits)} digits; "
                            f"{rule}", doc, {"key": str(c.index), **_ev([f"entry_lines={c.index}"], c.label)})


def check_hts_doubts(doc: Document, data: dict):
    """Warnings from the optional AI check of tariff numbers against descriptions (saved by hts_ai.py)."""
    if doc.doc_type != CUSTOMS:
        return
    from .hts_ai import doubts_for

    for c, reason in doubts_for(doc, data):
        yield IssueSpec("hts_description_doubt", WARNING,
                        f"{doc.original_filename}: {c.label} tariff number {c.hts} may not fit "
                        f"“{c.description[:80]}”: {reason}", doc,
                        {"key": str(c.index), **_ev([f"entry_lines={c.index}"], c.label)})


# --------------------------------------------------------------------------- duplicates


def check_duplicate_entry(doc: Document, data: dict):
    """The same entry number received before: with the same total it is a duplicate (never pay duty twice);
    with other amounts it is probably a corrected entry (post summary correction)."""
    if doc.doc_type != CUSTOMS or not data.get("entry_number"):
        return
    number = normalize_entry_number(data.get("entry_number"))
    total = total_duty_and_fees(data)
    earlier = (Document.objects.filter(organization=doc.organization, doc_type=CUSTOMS, pk__lt=doc.pk,
                                       fields__name="entry_number")
               .prefetch_related("fields").distinct().order_by("pk"))
    for other in earlier:
        od = other.data()
        if normalize_entry_number(od.get("entry_number")) != number:
            continue
        cur = entry_currency(data)
        if total_duty_and_fees(od) == total:
            yield IssueSpec("duplicate_customs_entry", ERROR,
                            f"{doc.original_filename}: entry {data.get('entry_number')} with the same total "
                            f"({money(total, cur)}) was received before as {other.original_filename}. "
                            "Don't pay the duty twice.", doc,
                            {"duplicate_of": other.pk, **_ev(["entry_number"], "the entry number")},
                            amount_at_risk=total, currency=cur)
        else:
            yield IssueSpec("customs_entry_changed", WARNING,
                            f"{doc.original_filename}: entry {data.get('entry_number')} was received before as "
                            f"{other.original_filename} with a total of {money(total_duty_and_fees(od), cur)}; "
                            f"this copy says {money(total, cur)}", doc,
                            {"duplicate_of": other.pk, **_ev(["entry_number"], "the entry number")}, currency=cur)
        return


# --------------------------------------------------------------------------- shipment: entry against the invoices


ORIGIN_LABEL = re.compile(r"(?:country of origin|origin country|country of manufacture|made in)\s*[:\-]?\s*"
                          r"(?P<v>[A-Za-z][A-Za-z .,'()]{1,60})", re.I)
COUNTRIES = {
    "CN": ["china", "peoples republic of china", "prc", "p r china"], "VN": ["vietnam", "viet nam"],
    "TR": ["turkey", "turkiye", "türkiye"], "IN": ["india"], "MX": ["mexico"], "CA": ["canada"],
    "US": ["united states", "usa", "us", "united states of america"], "DE": ["germany"], "IT": ["italy"],
    "FR": ["france"], "ES": ["spain"], "NL": ["netherlands", "the netherlands", "holland"], "GB": ["united kingdom",
    "uk", "great britain", "england"], "JP": ["japan"], "KR": ["korea", "south korea", "republic of korea"],
    "TW": ["taiwan"], "TH": ["thailand"], "MY": ["malaysia"], "ID": ["indonesia"], "BD": ["bangladesh"],
    "PK": ["pakistan"], "KH": ["cambodia"], "HK": ["hong kong"], "SG": ["singapore"], "PH": ["philippines"],
    "BR": ["brazil"], "LK": ["sri lanka"], "PL": ["poland"], "PT": ["portugal"], "BE": ["belgium"],
}
_NAMES = {name: code for code, names in COUNTRIES.items() for name in names}


def _plain(raw) -> str:
    text = re.sub(r"[^a-zà-ÿ ]", " ", str(raw or "").lower().replace("'", "").replace("’", ""))
    return re.sub(r"\s+", " ", text).strip()


def country_code(raw) -> str:
    """'China', 'PRC', "People's Republic of China" and 'CN' all give CN; an unknown name is kept as written
    (upper case), so two unknown names are still compared."""
    text = _plain(raw)
    if not text:
        return ""
    if len(text) == 2 and text.upper() in COUNTRIES:
        return text.upper()
    return _NAMES.get(text, text.upper())


def invoice_origin(doc: Document) -> str:
    """Country of origin printed on a commercial invoice ('Country of Origin: China', 'Made in Vietnam'), as an
    ISO code; empty when none is printed or the name isn't one ShipMatch knows (then nothing is compared)."""
    for line in (doc.text or "").splitlines():
        m = ORIGIN_LABEL.search(line)
        if not m:
            continue
        text = _plain(m.group("v"))
        for name in sorted(_NAMES, key=len, reverse=True):
            if text == name or text.startswith(name + " "):
                return _NAMES[name]
        first = text.split(" ")[0] if text else ""
        if len(first) == 2 and first.upper() in COUNTRIES:
            return first.upper()
    return ""


def invoice_value(org, invoices: list[Document], data: dict) -> tuple[Decimal, str, str] | None:
    """The commercial invoices' total in the entry's currency: (value, how it was converted, invoice currency)."""
    cur = entry_currency(data) or org.home_currency
    totals: dict[str, Decimal] = {}
    for inv in invoices:
        amount = dec(inv.field("total_amount"))
        if amount is None:
            continue
        icur = (inv.field("currency") or cur).upper()
        totals[icur] = totals.get(icur, Decimal("0.00")) + amount
    if len(totals) != 1:
        return None  # nothing to compare, or invoices in several currencies
    icur, amount = next(iter(totals.items()))
    if icur == cur:
        return amount, "", icur
    raw_rate = data.get("exchange_rate")
    stated_cur = str(data.get("invoice_currency") or "").upper()
    if raw_rate not in (None, "") and (not stated_cur or stated_cur == icur):
        try:
            rate = Decimal(str(raw_rate))
        except Exception:
            rate = None
        if rate:
            return (amount * rate).quantize(Decimal("0.01")), f"{icur} {amount:,.2f} at the entry's rate {rate}", icur
    if cur == org.home_currency:
        converted = org.to_home(amount, icur)
        if converted is not None:
            return converted, f"{icur} {amount:,.2f} at the organization's rate", icur
    return None


def check_entry_against_invoices(shipment, docs: list[Document]):
    """Entered value against the commercial invoices (converted with the entry's exchange rate), and the
    country of origin against the invoices."""
    entries = [d for d in docs if d.doc_type == CUSTOMS]
    invoices = [d for d in docs if d.doc_type == Document.DocType.COMMERCIAL_INVOICE]
    if not entries or not invoices:
        return
    org = shipment.organization
    for doc in entries:
        data = doc.data()
        cur = entry_currency(data) or org.home_currency
        value = entered_value(data)
        found = invoice_value(org, invoices, data)
        if value is not None and found is not None:
            inv_value, how, _ = found
            if inv_value > 0:
                diff = value - inv_value
                pct = (diff / inv_value * 100).quantize(Decimal("0.1"))
                how_text = f" ({how})" if how else ""
                ev = _ev(["total_entered_value"], "the total entered value")
                if pct < -value_tolerance_percent():
                    yield IssueSpec(
                        "entered_value_low", ERROR,
                        f"{doc.original_filename}: entered value {money(value, cur)} is {money(-diff, cur)} "
                        f"({abs(pct)}%) below the commercial invoices, {money(inv_value, cur)}{how_text}",
                        doc, {"entered": str(value), "invoices": str(inv_value), "percent": str(pct), **ev}, currency=cur)
                elif pct > value_tolerance_percent():
                    yield IssueSpec(
                        "entered_value_high", WARNING,
                        f"{doc.original_filename}: entered value {money(value, cur)} is {money(diff, cur)} ({pct}%) "
                        f"above the commercial invoices, {money(inv_value, cur)}{how_text}. Duty and fees are "
                        "paid on the difference too.", doc,
                        {"entered": str(value), "invoices": str(inv_value), "percent": str(pct), **ev},
                        amount_at_risk=_overvaluation_cost(data, value, inv_value), currency=cur)
        origin = country_code(data.get("country_of_origin"))
        if origin:
            for inv in invoices:
                inv_origin = invoice_origin(inv)
                if inv_origin and inv_origin != origin:
                    yield IssueSpec(
                        "origin_mismatch", WARNING,
                        f"{doc.original_filename}: country of origin {data.get('country_of_origin')}, but "
                        f"{inv.original_filename} says {inv_origin}", doc,
                        {"key": inv_origin, "entry": origin, "invoice": inv_origin,
                         **_ev(["country_of_origin"], "the country of origin")})
                    break


def _overvaluation_cost(data: dict, entered: Decimal, invoiced: Decimal) -> Decimal | None:
    """Extra duty and fees paid because the entered value is above the invoices: the duty at the entry's average
    rate, HMF at its rate, and MPF up to its maximum, on the difference."""
    diff = entered - invoiced
    if diff <= 0 or entered <= 0:
        return None
    extra = Decimal("0.00")
    duty = dec(data.get("total_duty"))
    if duty:
        extra += (diff * duty / entered).quantize(Decimal("0.01"))
    if dec(data.get("harbor_maintenance_fee")):
        extra += expected_hmf(entered) - expected_hmf(invoiced)
    day = _entry_date(data)
    if dec(data.get("merchandise_processing_fee")) and day is not None:
        extra += expected_mpf(entered, day)[0] - expected_mpf(invoiced, day)[0]
    return extra if extra > 0 else None


# --------------------------------------------------------------------------- arrival notices


def check_arrival_notice(doc: Document, data: dict):
    """Dates that can't be right: a last free day before the containers were discharged."""
    if doc.doc_type != NOTICE:
        return
    from apps.documents.services.normalize import parse_date

    start = parse_date(data.get("discharge_date")) or parse_date(data.get("actual_arrival_date"))
    rows = [r for r in data.get("container_dates") or [] if isinstance(r, dict)]
    header = parse_date(data.get("demurrage_last_free_day"))
    for i, row in enumerate(rows or [{}]):
        discharged = parse_date(row.get("discharge_date")) or start
        lfd = parse_date(row.get("demurrage_last_free_day")) or header
        if discharged and lfd and lfd < discharged:
            who = row.get("container_number") or "the containers"
            yield IssueSpec("free_time_dates", WARNING,
                            f"{doc.original_filename}: last free day {day_text(lfd)} for {who} is before the "
                            f"discharge date {day_text(discharged)}", doc,
                            {"key": str(row.get("container_number") or i)})
            return

