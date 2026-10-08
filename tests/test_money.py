"""Rates and savings (apps/rates): charge names, lanes, quote matching, tolerances, each rate rule,
CSV import/export, the savings outcome math, ROI math, permissions and demo data."""
from __future__ import annotations

import io
import itertools
import json
import shutil
import subprocess
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditEvent, Organization
from apps.documents.models import Document, ExtractedField
from apps.rates import charges, csvio, lanes, roi, savings
from apps.rates.matching import context_for, find_quote
from apps.rates.models import ApprovedAccessorial, CaughtCharge, ChargeAlias, Quote, QuoteCharge, RateSettings
from apps.rates.services import recheck
from apps.shipments.models import MatchLink, Shipment
from apps.shipments.services.containers import make_container
from apps.shipments.services.validation import can_approve, validate_shipment

D = Decimal
RATE_CODES = {"over_quote", "unapproved_accessorial", "accessorial_unchecked", "no_quote", "quote_currency"}
HL = "Harborlink Logistics LLC"
MD = "Metro Drayage Co."
_seq = itertools.count(1)


# --------------------------------------------------------------------------- helpers


def _doc(org, shipment, doc_type, fields, text=""):
    n = next(_seq)
    d = Document.objects.create(organization=org, original_filename=f"{doc_type}-{n}.pdf", sha256=uuid.uuid4().hex,
                                doc_type=doc_type, text=text, status=Document.Status.MATCHED)
    for k, v in fields.items():
        ExtractedField.objects.create(document=d, name=k, value=v, confidence=0.99, grounded=True)
    MatchLink.objects.create(document=d, shipment=shipment, method=MatchLink.Method.EXACT_BL, score=1.0)
    return d


def make_shipment(org, *, vendor=HL, lines=(("Ocean Freight", "4800.00"),), boxes=2, pol="Ningbo",
                  pod="Long Beach, CA", issue_date="2026-03-10", equipment="40HC", currency="USD", with_bl=True,
                  quantities=None, validate=True):
    n = next(_seq)
    containers = [make_container("OSL", 100000 + n * 10 + i) for i in range(boxes)]
    bl = f"OSLN{9000000000 + n}"
    s = Shipment.objects.create(organization=org, bl_number=bl if with_bl else "", container_numbers=containers)
    if with_bl:
        table = "\n".join(f"{c} SL{n}{i} {equipment} 12,000" for i, c in enumerate(containers))
        _doc(org, s, "bill_of_lading", {"carrier_name": "Oceanic Star Line", "bl_number": bl, "issue_date": issue_date,
                                        "port_of_loading": pol, "port_of_discharge": pod,
                                        "container_numbers": containers}, text=f"BILL OF LADING\n{table}")
    items = []
    for i, (desc, amount) in enumerate(lines):
        item = {"description": desc, "amount": amount}
        if quantities and quantities[i] is not None:
            item["quantity"] = quantities[i]
        items.append(item)
    total = sum((D(a) for _, a in lines), D("0.00"))
    inv = _doc(org, s, "freight_invoice", {
        "vendor_name": vendor, "invoice_number": f"INV-{n}", "invoice_date": "2026-03-28", "currency": currency,
        "bl_number": bl, "container_numbers": containers, "line_items": items, "total_amount": f"{total:.2f}"})
    if validate:
        validate_shipment(s)
    return s, inv


def make_quote(org, vendor=HL, origin="Ningbo", destination="Long Beach, CA", equipment="40HC",
               lines=(("ocean_freight", "2400.00", "container"),), valid_from=date(2026, 1, 1),
               valid_to=date(2026, 12, 31), currency="USD", ref=None, all_in=False):
    q = Quote.objects.create(organization=org, vendor_name=vendor, origin=origin, destination=destination,
                             equipment=equipment, valid_from=valid_from, valid_to=valid_to, currency=currency,
                             reference=ref or f"Q-{next(_seq)}", all_in=all_in)
    for code, amount, basis in lines:
        QuoteCharge.objects.create(quote=q, code=code, amount=D(amount), basis=basis)
    return q


def approve_extra(org, vendor=MD, code="detention", unit="day", free=4, per_unit="100.00", cap=None):
    return ApprovedAccessorial.objects.create(organization=org, vendor_name=vendor, code=code, unit=unit,
                                              free_units=free, max_per_unit=D(per_unit) if per_unit else None,
                                              max_amount=D(cap) if cap else None, currency="USD")


def rate_issues(s, code=None):
    qs = s.issues.filter(code__in=RATE_CODES).order_by("id")
    return list(qs.filter(code=code) if code else qs)


def set_lines(doc, lines):
    f = doc.fields.get(name="line_items")
    f.value = [{"description": d, "amount": a} for d, a in lines]
    f.save()
    t = doc.fields.get(name="total_amount")
    t.value = f"{sum((D(a) for _, a in lines), D('0.00')):.2f}"
    t.save()


# --------------------------------------------------------------------------- charge names


@pytest.mark.parametrize("text,code", [
    ("Ocean Freight", "ocean_freight"), ("O/F 40HC", "ocean_freight"), ("Basic freight", "ocean_freight"),
    ("Terminal Handling Charge (THC)", "thc_destination"), ("DTHC", "thc_destination"), ("OTHC", "thc_origin"),
    ("THC at origin", "thc_origin"), ("BAF", "baf"), ("Bunker surcharge", "baf"), ("Low Sulphur Surcharge", "baf"),
    ("CAF", "caf"), ("Documentation Fee", "documentation"), ("Docs fee", "documentation"), ("B/L fee", "bl_fee"),
    ("Telex release", "bl_fee"), ("ISF Filing Fee", "security_filing"), ("AMS", "security_filing"),
    ("Customs Clearance", "customs_clearance"), ("Customs brokerage", "customs_clearance"),
    ("Customs exam (CET)", "exam"), ("X-ray inspection", "exam"), ("Drayage Port to Warehouse", "trucking"),
    ("Chassis Rental", "chassis"), ("Chassis split", "chassis_split"), ("Fuel Surcharge", "fuel_surcharge"),
    ("FSC", "fuel_surcharge"), ("Detention 6 days @ 125.00/day", "detention"), ("Demurrage", "demurrage"),
    ("D&D charges", "demurrage"), ("Storage 3 days", "storage"), ("Per diem", "per_diem"),
    ("Waiting Time 3 hrs", "waiting_time"), ("Driver detention", "waiting_time"), ("Re-delivery", "redelivery"),
    ("Dry run", "redelivery"), ("Port Congestion Surcharge", "congestion"), ("PSS", "congestion"),
    ("Admin Fee", "admin_fee"), ("Handling fee", "admin_fee"), ("Pre-pull", "pre_pull"),
    ("Overweight surcharge", "overweight"), ("Hazmat surcharge", "hazmat"),
])
def test_keyword_table(text, code):
    assert charges.match_keywords(text) == code


def test_unknown_charge_is_other_and_counts_as_extra():
    assert charges.match_keywords("Gate-in fee") is None
    assert charges.classify("Gate-in fee") == "other"
    assert charges.is_accessorial("other") and not charges.is_accessorial("ocean_freight")


def test_code_for_accepts_codes_labels_and_names():
    assert charges.code_for("ocean_freight") == "ocean_freight"
    assert charges.code_for("Terminal handling at destination") == "thc_destination"
    assert charges.code_for("THC") == "thc_destination"
    assert charges.code_for("nonsense") is None


def test_alias_key_ignores_amounts_and_counts():
    assert charges.alias_key("Detention 6 days @ 125.00/day") == charges.alias_key("Detention 2 days") == "detention"


@pytest.mark.parametrize("text,unit,qty,units,chargeable,free", [
    ("Detention 6 days @ 125.00/day", "day", None, 6.0, False, None),
    ("Detention 6 days (4 free)", "day", None, 6.0, False, 4),
    ("Storage 3 chargeable days", "day", None, 3.0, True, None),
    ("Waiting Time 3 hrs @ 95.00/hr", "hour", None, 3.0, False, None),
    ("Detention", "day", "5", 5.0, False, None),
    ("Detention", "day", None, None, False, None),
    ("Exam fee", "each", None, 1.0, False, None),
])
def test_units_on_line(text, unit, qty, units, chargeable, free):
    u = charges.units_on_line(text, qty, unit)
    assert (u.units, u.already_chargeable, u.free_on_line) == (units, chargeable, free)


@pytest.mark.django_db
def test_learned_name_beats_keywords(org):
    ChargeAlias.objects.create(organization=org, key="thc", code="thc_origin")
    assert charges.classify("THC", org=org) == "thc_origin"
    assert charges.classify("THC") == "thc_destination"


@pytest.mark.django_db
def test_ai_names_unknown_charges_once_and_falls_back(org, settings, monkeypatch):
    from apps.documents.services import llm

    settings.EXTRACTION_PROVIDER = "anthropic"
    calls = []

    def fake(system, user, schema, **kw):
        calls.append(user)
        assert schema["additionalProperties"] is False
        assert schema["properties"]["lines"]["items"]["additionalProperties"] is False
        return {"lines": [{"index": 0, "code": "storage"}, {"index": 1, "code": "not-a-code"}]}

    monkeypatch.setattr(llm, "structured_call", fake)
    out = charges.classify_many(["Gate-in fee", "Strange thing"], org=org)
    assert out == {"Gate-in fee": "storage", "Strange thing": "other"}
    assert ChargeAlias.objects.get(organization=org, key="gate in fee").source == ChargeAlias.Source.AI
    assert charges.classify("Gate-in fee 2", org=org) == "storage"   # remembered: no second call for it
    assert len(calls) == 1

    def boom(*a, **kw):
        raise llm.LLMError("down")

    monkeypatch.setattr(llm, "structured_call", boom)
    assert charges.classify("Another unknown", org=org) == "other"
    assert not ChargeAlias.objects.filter(key="another unknown").exists()


@pytest.mark.django_db
def test_ai_not_used_when_org_turns_it_off(org, settings, monkeypatch):
    from apps.documents.services import llm

    settings.EXTRACTION_PROVIDER = "anthropic"
    RateSettings.objects.create(organization=org, ai_classify=False)
    monkeypatch.setattr(llm, "structured_call", lambda *a, **k: pytest.fail("AI must not be called"))
    assert charges.classify("Gate-in fee", org=org) == "other"


# --------------------------------------------------------------------------- lanes


@pytest.mark.parametrize("text,code", [
    ("Shenzhen (Yantian)", "CNYTN"), ("Ningbo", "CNNGB"), ("CNNGB", "CNNGB"), ("cnsha", "CNSHA"),
    ("Istanbul (Ambarli)", "TRAMB"), ("Ho Chi Minh City (Cat Lai)", "VNCLI"), ("Long Beach, CA", "USLGB"),
    ("Port of Long Beach, CA, USA", "USLGB"), ("Newark, NJ", "USEWR"), ("Houston, TX", "USHOU"),
    ("Ningbo (CNNGB)", "CNNGB"), ("Jebel Ali", "AEJEA"),
])
def test_ports_resolve(text, code):
    assert lanes.resolve(text).code == code


def test_port_matching_levels():
    assert lanes.match_score("Yantian", "Shenzhen (Yantian)") == 1.0
    assert lanes.match_score("Shenzhen", "Shenzhen (Yantian)") == 0.8          # same port area
    assert lanes.match_score("Los Angeles", "Long Beach, CA") == 0.8
    assert lanes.match_score("Ningbo", "Shanghai") is None
    assert lanes.match_score("Kingston", "Kingston, Jamaica") == 0.7            # not in the table: by name
    assert lanes.match_score("", "Ningbo") == 0.5                                # empty quote place = any
    assert lanes.resolve("Kingston") is None and lanes.place_key("Kingston, Jamaica") == "kingston jamaica"


def test_equipment_normalization():
    assert lanes.normalize_equipment("40HQ") == "40HC"
    assert lanes.normalize_equipment("20' DV") == "20GP"
    assert lanes.normalize_equipment("40 REEFER") == "40RF"
    assert lanes.normalize_equipment("banana") is None
    assert lanes.equipment_in_text("OSLU1 SL1 40HC 9,000\nOSLU2 SL2 40'HC 9,000") == {"40HC": 2}


# --------------------------------------------------------------------------- quote matching


@pytest.mark.django_db
def test_most_specific_and_newest_quote_wins(org):
    make_quote(org, origin="", ref="ANY")
    make_quote(org, origin="Shenzhen", ref="AREA")
    make_quote(org, origin="Yantian", ref="EXACT-OLD", valid_from=date(2026, 1, 1))
    make_quote(org, origin="Yantian", ref="EXACT-NEW", valid_from=date(2026, 3, 1))
    s, inv = make_shipment(org, pol="Shenzhen (Yantian)", validate=False)
    ctx = context_for(s, list(s.documents.prefetch_related("fields")), inv)
    assert (ctx.origin, ctx.equipment, ctx.containers, ctx.ship_date) == ("Shenzhen (Yantian)", "40HC", 2,
                                                                        date(2026, 3, 10))
    assert find_quote(org, "harborlink logistics", ctx).quote.reference == "EXACT-NEW"


@pytest.mark.django_db
def test_no_match_reasons(org):
    make_quote(org, valid_to=date(2026, 2, 28))
    s, inv = make_shipment(org, validate=False)
    ctx = context_for(s, list(s.documents.prefetch_related("fields")), inv)
    m = find_quote(org, "harborlink logistics", ctx)
    assert m.status == "no_match" and "valid on 10 Mar 2026" in m.reason
    make_quote(org, equipment="20GP")
    m = find_quote(org, "harborlink logistics", ctx)
    assert m.status == "no_match" and "Ningbo to Long Beach, CA for 40HC" in m.reason
    assert find_quote(org, "unknown vendor", ctx).status == "none_on_file"


@pytest.mark.django_db
def test_without_bill_of_lading_only_an_unambiguous_quote_is_used(org):
    make_quote(org, origin="Ningbo", ref="ONE")
    s, inv = make_shipment(org, with_bl=False, validate=False)
    ctx = context_for(s, list(s.documents.prefetch_related("fields")), inv)
    m = find_quote(org, "harborlink logistics", ctx)
    assert m.status == "matched" and "bill of lading" in m.assumed[0]
    make_quote(org, origin="Shanghai", ref="TWO")
    m = find_quote(org, "harborlink logistics", ctx)
    assert m.status == "ambiguous" and "no bill of lading" in m.reason


# --------------------------------------------------------------------------- rule (a): over quote


@pytest.mark.django_db
def test_over_quote_is_an_error_with_the_excess_at_risk(org):
    make_quote(org, ref="HL-26-1", lines=[("ocean_freight", "2400.00", "container"),
                                          ("thc_destination", "350.00", "container")])
    s, _ = make_shipment(org, lines=[("Ocean Freight", "5000.00"), ("Terminal Handling Charge (THC)", "710.00")])
    issues = rate_issues(s)
    assert [i.code for i in issues] == ["over_quote"]          # THC 10 over is within tolerance (14.00)
    i = issues[0]
    assert i.severity == "error" and i.amount_at_risk == D("200.00") and i.currency == "USD"
    assert "HL-26-1" in i.message and "USD 5,000.00" in i.message and "USD 4,800.00" in i.message
    assert "2 containers at 2,400.00" in i.message
    assert i.data["charge_code"] == "ocean_freight" and i.data["quote_id"]
    assert can_approve(s)[0] is False


@pytest.mark.django_db
def test_tolerance_boundaries_and_org_setting(org):
    make_quote(org, lines=[("ocean_freight", "2400.00", "container")])
    # Tolerance = max(2% of 4,800.00 = 96.00, 10.00) = 96.00
    s, _ = make_shipment(org, lines=[("Ocean Freight", "4896.00")])
    assert not rate_issues(s)
    s2, _ = make_shipment(org, lines=[("Ocean Freight", "4896.01")])
    assert rate_issues(s2)[0].amount_at_risk == D("96.01")
    RateSettings.objects.create(organization=org, tolerance_percent=D("0"), tolerance_amount=D("0"))
    validate_shipment(s)
    assert rate_issues(s)[0].amount_at_risk == D("96.00")
    assert RateSettings.for_org(org).tolerance_for(D("100")) == D("0")


@pytest.mark.django_db
def test_fixed_tolerance_applies_to_small_charges(org):
    make_quote(org, lines=[("ocean_freight", "2400.00", "container"), ("documentation", "75.00", "bl")])
    s, _ = make_shipment(org, lines=[("Ocean Freight", "4800.00"), ("Documentation Fee", "85.00")])
    assert not rate_issues(s)                                   # 10.00 over = the fixed 10.00: let through
    s2, _ = make_shipment(org, lines=[("Ocean Freight", "4800.00"), ("Documentation Fee", "86.00")])
    assert rate_issues(s2)[0].amount_at_risk == D("11.00")


@pytest.mark.django_db
def test_base_charge_missing_from_quote_and_all_in(org):
    make_quote(org, lines=[("ocean_freight", "2400.00", "container")])
    s, _ = make_shipment(org, lines=[("Ocean Freight", "4700.00"), ("CAF", "120.00")])
    i = rate_issues(s, "over_quote")[0]
    assert i.amount_at_risk == D("120.00") and "not in" in i.message
    Quote.objects.filter(organization=org).update(all_in=True)
    validate_shipment(s)
    assert not rate_issues(s)                                   # 4,820 vs 4,800 all-in: within tolerance


@pytest.mark.django_db
def test_invoice_without_lines_compares_the_total(org):
    make_quote(org, lines=[("ocean_freight", "2400.00", "container"), ("customs_clearance", "195.00", "shipment")])
    s, inv = make_shipment(org, lines=[], validate=False)
    inv.fields.filter(name="total_amount").update(value="5500.00")
    validate_shipment(s)
    i = rate_issues(s, "over_quote")[0]
    assert i.amount_at_risk == D("505.00") and i.data["catch_key"] == "total"


@pytest.mark.django_db
def test_changed_amount_raises_again_after_acceptance(org):
    make_quote(org)
    s, inv = make_shipment(org, lines=[("Ocean Freight", "5000.00")])
    i = rate_issues(s)[0]
    i.resolved, i.resolution_note = True, "Agreed by phone"
    i.save()
    validate_shipment(s)
    assert not s.issues.filter(code="over_quote", resolved=False).exists()
    set_lines(inv, [("Ocean Freight", "5400.00")])
    validate_shipment(s)
    assert s.issues.get(code="over_quote", resolved=False).amount_at_risk == D("600.00")


@pytest.mark.django_db
def test_quote_in_other_currency(org):
    org.fx_rates = {"EUR": "1.10"}
    org.save()
    make_quote(org, currency="EUR", lines=[("ocean_freight", "2000.00", "container")])  # = USD 2,200 a box
    s, _ = make_shipment(org, lines=[("Ocean Freight", "4700.00")])
    i = rate_issues(s, "over_quote")[0]
    assert i.amount_at_risk == D("300.00") and "converted" in i.message
    org.fx_rates = {}
    org.save()
    validate_shipment(s)
    assert [x.code for x in rate_issues(s)] == ["quote_currency"]


# --------------------------------------------------------------------------- rule (b): extra charges


@pytest.mark.django_db
def test_unapproved_extra_charge_is_a_warning_for_the_whole_line(org):
    make_quote(org)
    s, _ = make_shipment(org, lines=[("Ocean Freight", "4800.00"), ("Exam Fee (CET)", "350.00")])
    i = rate_issues(s)[0]
    assert (i.code, i.severity, i.amount_at_risk) == ("unapproved_accessorial", "warning", D("350.00"))
    assert "customs exam" in i.message and HL in i.message
    assert can_approve(s)[0] is True                            # warnings don't block approval


@pytest.mark.django_db
def test_approved_extra_with_free_days_and_daily_cap(org):
    make_quote(org, vendor=MD, origin="", lines=[("trucking", "600.00", "container")])
    approve_extra(org)                                           # detention after 4 free days, 100.00 a day
    s, _ = make_shipment(org, vendor=MD, lines=[("Drayage", "1200.00"), ("Detention 6 days @ 125.00/day", "750.00")])
    i = rate_issues(s)[0]
    assert i.code == "unapproved_accessorial" and i.amount_at_risk == D("550.00")
    assert "USD 200.00 is allowed for 6 days less 4 free" in i.message
    s2, _ = make_shipment(org, vendor=MD, lines=[("Drayage", "1200.00"), ("Detention 3 chargeable days", "300.00")])
    assert not rate_issues(s2)                                   # already chargeable: 3 x 100 = 300 allowed
    s3, _ = make_shipment(org, vendor=MD, lines=[("Drayage", "1200.00"), ("Detention 5 days", "100.00")])
    assert not rate_issues(s3)


@pytest.mark.django_db
def test_extra_charge_total_cap_and_unreadable_days(org):
    approve_extra(org, vendor=MD, code="storage", free=0, per_unit="50.00", cap="200.00")
    approve_extra(org, vendor=MD, code="waiting_time", unit="hour", free=2, per_unit="85.00")
    s, _ = make_shipment(org, vendor=MD, lines=[("Storage 10 days", "500.00")])
    assert rate_issues(s)[0].amount_at_risk == D("300.00")      # capped at 200.00 per invoice
    s2, _ = make_shipment(org, vendor=MD, lines=[("Waiting time", "300.00")])
    i = rate_issues(s2)[0]
    assert i.code == "accessorial_unchecked" and i.amount_at_risk is None and "how many hours" in i.message
    s3, _ = make_shipment(org, vendor=MD, lines=[("Waiting time", "300.00")], quantities=[4])
    assert rate_issues(s3)[0].amount_at_risk == D("130.00")      # quantity column: (4 - 2) x 85 = 170 allowed


@pytest.mark.django_db
def test_extra_charge_priced_in_the_quote(org):
    make_quote(org, lines=[("ocean_freight", "2400.00", "container"), ("exam", "250.00", "shipment")])
    s, _ = make_shipment(org, lines=[("Ocean Freight", "4800.00"), ("Exam fee", "300.00")])
    i = rate_issues(s)[0]
    assert i.code == "unapproved_accessorial" and i.amount_at_risk == D("50.00")
    s2, _ = make_shipment(org, lines=[("Ocean Freight", "4800.00"), ("Exam fee", "250.00")])
    assert not rate_issues(s2)


@pytest.mark.django_db
def test_vendors_without_rates_are_not_checked_unless_asked(org):
    s, _ = make_shipment(org, vendor="Unknown Freight Co", lines=[("Ocean Freight", "9000.00"), ("Exam fee", "300.00")])
    assert not rate_issues(s)
    RateSettings.objects.create(organization=org, check_unlisted_vendors=True)
    validate_shipment(s)
    assert [(i.code, i.amount_at_risk) for i in rate_issues(s)] == [("unapproved_accessorial", D("300.00"))]


# --------------------------------------------------------------------------- rule (c): no quote


@pytest.mark.django_db
def test_no_quote_warning_and_switch(org):
    make_quote(org, origin="Shanghai")
    s, _ = make_shipment(org, lines=[("Ocean Freight", "9000.00")])
    i = rate_issues(s)[0]
    assert (i.code, i.severity, i.amount_at_risk) == ("no_quote", "warning", None)
    assert "none covers Ningbo to Long Beach, CA for 40HC" in i.message
    RateSettings.objects.create(organization=org, warn_no_quote=False)
    validate_shipment(s)
    assert not rate_issues(s)


# --------------------------------------------------------------------------- savings


def _catch(s, code="over_quote"):
    return CaughtCharge.objects.get(shipment=s, code=code)


@pytest.mark.django_db
def test_savings_outcome_rules(org):
    make_quote(org)                                              # 2,400.00 a container, 2 containers
    make_quote(org, vendor="Swift Cargo Forwarding Inc.", lines=[("ocean_freight", "2400.00", "container")])
    over = [("Ocean Freight", "5000.00")]                        # 200.00 over
    a, _ = make_shipment(org, lines=over)                        # stays open
    b, _ = make_shipment(org, lines=over)                        # accepted
    c, _ = make_shipment(org, lines=over)                        # rejected
    d, d_inv = make_shipment(org, lines=over)                    # invoice corrected
    e, e_inv = make_shipment(org, lines=over)                    # partly corrected
    f, _ = make_shipment(org, vendor="Swift Cargo Forwarding Inc.", lines=over)   # quote raised later
    g, _ = make_shipment(org, lines=[("Ocean Freight", "4800.00"), ("Exam fee", "350.00")])  # approved, warning open

    issue = b.issues.get(code="over_quote")
    issue.resolved, issue.resolution_note = True, "Accepted after checking"
    issue.save()
    c.status = Shipment.Status.REJECTED
    c.save()
    set_lines(d_inv, [("Ocean Freight", "4800.00")])
    validate_shipment(d)
    set_lines(e_inv, [("Ocean Freight", "4900.00")])
    validate_shipment(e)
    Quote.objects.filter(vendor_name="Swift Cargo Forwarding Inc.").first().charges.update(amount=D("2500.00"))
    recheck(org)
    g.status = Shipment.Status.APPROVED
    g.save()

    assert _catch(d).issue_id is None and _catch(d).cleared_reason == "invoice"
    assert (_catch(e).amount_caught, _catch(e).amount_latest) == (D("200.00"), D("100.00"))
    assert _catch(f).cleared_reason == "rates"

    s = savings.summary(org, date(2000, 1, 1), timezone.localdate())
    assert s.prevented == D("500.00")          # c 200 + d 200 + e 100
    assert s.at_risk == D("300.00")            # a 200 + e 100
    assert s.accepted == D("550.00")           # b 200 + g 350
    assert s.caught == D("1350.00") and s.withdrawn == D("200.00")
    assert s.counts["withdrawn"] == 1 and s.saved == s.prevented
    rows = {r.catch.shipment_id: r for r in s.catches}
    assert rows[e.pk].outcome == "at_risk" and rows[e.pk].outcome_label == "Still at risk, partly prevented"
    by_code = {r.key: r for r in s.by_code}
    assert by_code["over_quote"].caught == D("1000.00") and by_code["unapproved_accessorial"].accepted == D("350.00")
    assert s.top_vendors[0].label == HL


@pytest.mark.django_db
def test_rate_change_does_not_erase_earlier_prevention(org):
    RateSettings.objects.create(organization=org, tolerance_percent=D("0"), tolerance_amount=D("0"))
    q = make_quote(org)
    s, inv = make_shipment(org, lines=[("Ocean Freight", "5000.00")])
    set_lines(inv, [("Ocean Freight", "4900.00")])               # vendor corrects part: 100 prevented
    validate_shipment(s)
    q.charges.update(amount=D("2425.00"))                        # quote raised: excess now 50
    recheck(org)
    c = _catch(s)
    assert (c.amount_caught, c.amount_latest) == (D("150.00"), D("50.00"))
    q.charges.update(amount=D("2500.00"))                        # no longer over: withdrawn, 100 still prevented
    recheck(org)
    out = savings.summary(org, date(2000, 1, 1), timezone.localdate())
    assert out.prevented == D("100.00") and out.withdrawn == D("50.00") and out.at_risk == 0


@pytest.mark.django_db
def test_overlapping_checks_count_once(org):
    make_quote(org)
    s, inv = make_shipment(org, lines=[("Ocean Freight", "5000.00")])
    from apps.shipments.models import ValidationIssue

    ValidationIssue.objects.create(organization=org, shipment=s, document=inv, code="amount_outlier",
                                   severity="warning", message="x", fingerprint="amount_outlier:x",
                                   amount_at_risk=D("260.00"), currency="USD")
    out = savings.summary(org, date(2000, 1, 1), timezone.localdate())
    assert out.caught == D("260.00")                                 # 200 over quote + 60 not explained by it
    row = next(r for r in out.catches if r.catch.code == "amount_outlier")
    assert row.partly_counted and row.amount == D("60.00")
    ValidationIssue.objects.create(organization=org, shipment=s, document=inv, code="duplicate_invoice",
                                   severity="error", message="dup", fingerprint="duplicate_invoice:x",
                                   amount_at_risk=D("5000.00"), currency="USD")
    out = savings.summary(org, date(2000, 1, 1), timezone.localdate())
    assert out.caught == D("5000.00") and out.counts["overlap"] == 2


@pytest.mark.django_db
def test_savings_period_months_currency_and_tenancy(org):
    make_quote(org)
    s, _ = make_shipment(org, lines=[("Ocean Freight", "5000.00")])
    c = _catch(s)
    c.first_caught_at = timezone.make_aware(datetime(2026, 2, 15, 12))
    c.save()
    other = Organization.objects.create(name="Other", slug="other")
    make_quote(other)
    make_shipment(other, lines=[("Ocean Freight", "9000.00")])
    feb = savings.summary(org, date(2026, 2, 1), date(2026, 2, 28))
    assert feb.caught == D("200.00") and [m.caught for m in feb.by_month] == [D("200.00")]
    assert savings.summary(org, date(2026, 3, 1), date(2026, 3, 31)).caught == 0
    year = savings.summary(org, date(2026, 1, 1), date(2026, 4, 30))
    assert [m.label for m in year.by_month] == ["Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026"]
    c.currency = "EUR"
    c.save()
    out = savings.summary(org, date(2026, 2, 1), date(2026, 2, 28))
    assert out.caught == 0 and out.unconverted == {"EUR": D("200.00")}


def test_period_ranges():
    today = date(2026, 10, 3)
    assert savings.period_range("this_month", today)[:2] == (date(2026, 10, 1), today)
    assert savings.period_range("last_month", today)[:2] == (date(2026, 9, 1), date(2026, 9, 30))
    assert savings.period_range("last_12", today)[:2] == (date(2025, 11, 1), today)
    assert savings.period_range("this_year", today)[:2] == (date(2026, 1, 1), today)
    assert savings.period_range("custom", today, date(2026, 5, 1), date(2026, 4, 1))[:2] == (date(2026, 4, 1),
                                                                                             date(2026, 5, 1))
    assert savings.period_range("bogus", today)[0] == date(2026, 10, 1)


@pytest.mark.django_db
def test_recovery_sources(org, monkeypatch):
    monkeypatch.setattr(savings, "RECOVERY_SOURCES", [])
    savings.register_recovery_source(lambda org, start, end: D("120.00"))
    savings.register_recovery_source(lambda org, start, end: [{"amount": "100", "currency": "EUR", "vendor_name": "X"}])

    def broken(org, start, end):
        raise RuntimeError("disputes app is down")

    savings.register_recovery_source(broken)
    org.fx_rates = {"EUR": "1.10"}
    org.save()
    out = savings.summary(org, date(2026, 1, 1), date(2026, 12, 31))
    assert out.recovered == D("230.00") and out.saved == D("230.00") and out.recovery_sources == 3


@pytest.mark.django_db
def test_ledger_sync_picks_up_issues_created_without_signals(org):
    from apps.shipments.models import ValidationIssue

    s = Shipment.objects.create(organization=org)
    ValidationIssue.objects.bulk_create([ValidationIssue(organization=org, shipment=s, code="total_mismatch",
                                                         severity="error", message="m", fingerprint="f",
                                                         amount_at_risk=D("40.00"), currency="USD")])
    assert not CaughtCharge.objects.exists()
    assert savings.summary(org, date(2000, 1, 1), timezone.localdate()).at_risk == D("40.00")


# --------------------------------------------------------------------------- ROI


def _roi(**kw):
    return roi.calculate({k: str(v) for k, v in kw.items()})


def test_roi_math_with_example_values():
    r = roi.calculate({}).result
    # 400 x (12 - 3) / 60 = 60 h; x 12 x 45 = 32,400; 400 x 12 x 5% x 150 = 36,000; cost 12,000
    assert (r.hours_saved_month, r.labor_saved_year, r.overcharges_year, r.cost_year) == (
        D("60.0"), D("32400"), D("36000"), D("12000"))
    assert r.net_year == D("56400") and r.payback_months == D("2.1")


def test_roi_math_edge_cases():
    r = _roi(invoices=0, price=500).result
    assert r.net_year == D("-6000") and r.payback_months is None
    r = _roi(invoices=1000, minutes_today=10, minutes_with=10, error_share=0, price=0).result
    assert r.hours_saved_month == 0 and r.net_year == 0 and r.payback_months is None
    assert _roi(invoices="1,200").result.hours_saved_month == D("180.0")


@pytest.mark.parametrize("params,field,text", [
    ({"invoices": "abc"}, "invoices", "Enter a number"),
    ({"invoices": "10.5"}, "invoices", "whole number"),
    ({"error_share": "120"}, "error_share", "from 0 to 100"),
    ({"hourly_cost": "-5"}, "hourly_cost", "from 0 to 10,000"),
    ({"minutes_with": "20"}, "minutes_with", "can't be more than minutes today"),
    ({"price": "NaN"}, "price", "Enter a number"),
])
def test_roi_validation(params, field, text):
    calc = roi.calculate(params)
    assert text in calc.errors[field] and calc.result is None
    assert roi.formatted(calc)["net_year"] == "–"


@pytest.mark.django_db
def test_roi_page_is_public_and_works_without_javascript(client):
    r = client.get(reverse("roi:calculator"), {"invoices": "1000", "currency": "EUR"})
    html = r.content.decode()
    assert r.status_code == 200 and "Sign in" in html and "js/roi.js" in html
    assert "EUR 159,000" in html          # 150 h x 12 x 45 = 81,000 + 1,000 x 12 x 5% x 150 = 90,000 - 12,000
    assert "<script>" not in html
    bad = client.get(reverse("roi:calculator"), {"minutes_with": "99"}).content.decode()
    assert "can&#x27;t be more than minutes today" in bad


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_roi_js_matches_python():
    js = Path(__file__).resolve().parent.parent / "static" / "js" / "roi.js"
    cases = [{}, {"invoices": 1234, "minutes_today": 17.5, "minutes_with": 2.5, "hourly_cost": 61.35,
                  "error_share": 7.3, "avg_overcharge": 212.4, "price": 1799.99},
             {"invoices": 0, "price": 500}, {"invoices": 50, "minutes_today": 4, "minutes_with": 3.5}]
    full = []
    for c in cases:
        values = {f.name: D(str(c.get(f.name, f.default))) for f in roi.FIELDS}
        full.append({k: float(v) for k, v in values.items()})
    script = (
        "global.window={};global.document={querySelector:()=>({addEventListener(){}}),querySelectorAll:()=>[]};"
        f"require({json.dumps(str(js))});"
        f"console.log(JSON.stringify({json.dumps(full)}.map(v=>window.ShipMatchROI.compute(v))));"
    )
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    for c, js_r in zip(full, out):
        py = roi.compute({k: D(str(v)) for k, v in c.items()})
        assert D(str(js_r["hours"])) == py.hours_saved_month
        assert (D(str(js_r["labor"])), D(str(js_r["over"])), D(str(js_r["net"]))) == (
            py.labor_saved_year, py.overcharges_year, py.net_year)
        assert (js_r["payback"] is None) == (py.payback_months is None)
        if py.payback_months is not None:
            assert D(str(js_r["payback"])) == py.payback_months


# --------------------------------------------------------------------------- CSV


@pytest.mark.django_db
def test_csv_template_imports_and_reimport_updates(org, approver):
    result = csvio.import_quotes(org, csvio.template_csv().encode(), actor=approver, filename="t.csv")
    assert result.ok and len(result.created) == 2 and result.rows == 4
    q = Quote.objects.get(organization=org, reference="Q-2026-014")
    assert (q.origin_key, q.destination_key, q.equipment, q.charges.count()) == ("CNNGB", "USLGB", "40HC", 3)
    drayage = Quote.objects.get(organization=org, reference="D-77")
    assert drayage.origin == "" and drayage.valid_to is None
    data = csvio.template_csv().replace("2400.00", "2450.00").encode()
    again = csvio.import_quotes(org, data, actor=approver, filename="t.csv")
    assert len(again.updated) == 2 and not again.created and Quote.objects.filter(organization=org).count() == 2
    assert q.charges.get(code="ocean_freight").amount == D("2450.00") and q.charges.count() == 3
    assert AuditEvent.objects.filter(organization=org, action="quote.updated", data__source="CSV import").count() == 2
    assert AuditEvent.objects.filter(organization=org, action="quote.imported").count() == 2


@pytest.mark.django_db
def test_csv_row_errors_import_nothing(org):
    rows = [csvio.COLUMNS,
            ["Ok Vendor", "R1", "Ningbo", "Long Beach", "40HC", "2026-01-01", "", "USD", "no", "ocean_freight", "", "2400", "container", ""],
            ["", "R2", "Ningbo", "Long Beach", "40XX", "someday", "", "US", "maybe", "banana", "", "abc", "pallet", ""],
            ["Ok Vendor", "R1", "Ningbo", "Long Beach", "40HC", "2026-01-01", "", "EUR", "no", "thc", "", "350", "container", ""],
            ["Ok Vendor", "R3", "", "", "", "2026-06-01", "2026-01-01", "USD", "", "documentation", "", "-5", "bl", ""]]
    import csv as _csv
    import io

    buf = io.StringIO()
    _csv.writer(buf).writerows(rows)
    result = csvio.import_quotes(org, buf.getvalue().encode())
    assert not result.ok and not Quote.objects.exists()
    found = {(e.row, e.column) for e in result.errors}
    assert {(3, "vendor"), (3, "equipment"), (3, "valid_from"), (3, "currency"), (3, "all_in"), (3, "charge_code"),
            (3, "amount"), (3, "basis"), (4, "currency"), (5, "valid_to"), (5, "amount")} <= found
    assert "Differs from row 2" in next(e.message for e in result.errors if (e.row, e.column) == (4, "currency"))


@pytest.mark.parametrize("data,message", [
    (b"", "empty"),
    (b"vendor,amount\nX,1\n", "Missing columns: valid_from, currency, charge_code, basis"),
    (",".join(csvio.COLUMNS).encode() + b"\n", "no rows"),
    (b"x" * (csvio.MAX_BYTES + 1), "larger than 2 MB"),
], ids=["empty", "missing-columns", "no-rows", "too-large"])  # short ids: Windows caps env vars at 32767 chars
@pytest.mark.django_db
def test_csv_file_level_errors(org, data, message):
    result = csvio.import_quotes(org, data)
    assert message in result.file_error


@pytest.mark.django_db
def test_csv_semicolons_aliases_and_windows_encoding(org):
    text = ("Vendor;Quote;POL;POD;Container type;Start;Currency;Charge;Rate;Per\n"
            "Café Lines;A1;CNNGB;USLGB;40HQ;31 Jan 2026;usd;THC;€350,00;box\n").replace("€350,00", "350.00")
    result = csvio.import_quotes(org, text.encode("cp1252"))
    assert result.ok, result.errors
    q = Quote.objects.get()
    assert (q.vendor_name, q.equipment, q.valid_from, q.currency) == ("Café Lines", "40HC", date(2026, 1, 31), "USD")
    assert q.charges.get().code == "thc_destination"


@pytest.mark.django_db
def test_csv_export_round_trips(org):
    make_quote(org, ref="RT-1", lines=[("ocean_freight", "2400.00", "container"), ("documentation", "75.00", "bl")])
    make_quote(org, vendor=MD, origin="", ref="RT-2", valid_to=None, lines=[("trucking", "600.00", "container")])
    exported = csvio.export_csv(Quote.objects.filter(organization=org).prefetch_related("charges"))
    other = Organization.objects.create(name="Other", slug="other")
    result = csvio.import_quotes(other, exported.encode())
    assert result.ok and len(result.created) == 2
    again = csvio.export_csv(Quote.objects.filter(organization=other).prefetch_related("charges"))
    assert again == exported


# --------------------------------------------------------------------------- views and permissions


def _quote_post(**over):
    data = {"vendor_name": HL, "reference": "NEW-1", "origin": "Ningbo", "destination": "Long Beach, CA",
            "equipment": "40HC", "valid_from": "2026-01-01", "valid_to": "2026-12-31", "currency": "usd", "notes": "",
            "charges-TOTAL_FORMS": "2", "charges-INITIAL_FORMS": "0", "charges-MIN_NUM_FORMS": "1",
            "charges-MAX_NUM_FORMS": "60",
            "charges-0-code": "ocean_freight", "charges-0-description": "", "charges-0-amount": "2400.00",
            "charges-0-basis": "container",
            "charges-1-code": "", "charges-1-description": "", "charges-1-amount": "", "charges-1-basis": "container"}
    data.update(over)
    return data


@pytest.mark.django_db
def test_rates_permissions(client, org, viewer, user, approver, admin_user):
    q = make_quote(org)
    client.force_login(viewer)
    for url in (reverse("rates:list"), reverse("rates:detail", args=[q.pk]), reverse("rates:extras"),
                reverse("rates:charge_names"), reverse("rates:rules"), reverse("rates:export"),
                reverse("rates:template"), reverse("savings:summary"), reverse("savings:export")):
        assert client.get(url).status_code == 200, url
    for who in (viewer, user):
        client.force_login(who)
        assert client.get(reverse("rates:create")).status_code == 403
        assert client.post(reverse("rates:create"), _quote_post()).status_code == 403
        assert client.post(reverse("rates:archive", args=[q.pk])).status_code == 403
        assert client.post(reverse("rates:import")).status_code == 403
        assert client.post(reverse("rates:extra_create"), {}).status_code == 403
    client.force_login(approver)
    assert client.post(reverse("rates:rules"), {"tolerance_percent": "1", "tolerance_amount": "5"}).status_code == 403
    assert client.post(reverse("rates:create"), _quote_post()).status_code == 302
    client.force_login(admin_user)
    r = client.post(reverse("rates:rules"), {"tolerance_percent": "1", "tolerance_amount": "5",
                                             "warn_no_quote": "on"})
    assert r.status_code == 302 and RateSettings.objects.get(organization=org).tolerance_amount == D("5")
    assert AuditEvent.objects.filter(action="rate_settings.updated").exists()


@pytest.mark.django_db
def test_other_organizations_quotes_are_not_reachable(client, org, approver):
    other = Organization.objects.create(name="Other", slug="other")
    q = make_quote(other)
    client.force_login(approver)
    assert client.get(reverse("rates:detail", args=[q.pk])).status_code == 404
    assert client.post(reverse("rates:delete", args=[q.pk]), {"confirm": "delete"}).status_code == 404
    assert q.vendor_name not in client.get(reverse("rates:list")).content.decode()


@pytest.mark.django_db
def test_quote_lifecycle_through_the_ui(client, org, approver):
    s, _ = make_shipment(org, lines=[("Ocean Freight", "5000.00")])
    assert not rate_issues(s)
    client.force_login(approver)
    r = client.post(reverse("rates:create"), _quote_post(), follow=True)
    q = Quote.objects.get(organization=org, reference="NEW-1")
    assert q.currency == "USD" and q.charges.count() == 1 and q.created_by == approver
    assert "Checked 1 open shipment" in r.content.decode()
    assert rate_issues(s, "over_quote")[0].amount_at_risk == D("200.00")        # re-checked right away

    edit = _quote_post(**{"charges-TOTAL_FORMS": "1", "charges-INITIAL_FORMS": "1", "charges-0-id": str(
        q.charges.get().pk), "charges-0-quote": str(q.pk), "charges-0-amount": "2500.00"})
    client.post(reverse("rates:edit", args=[q.pk]), edit)
    assert q.charges.get().amount == D("2500.00") and not rate_issues(s)
    ev = AuditEvent.objects.get(action="quote.updated", object_id=str(q.pk))
    assert ev.data["changes"]["charges"] == [["ocean_freight 2400.00 container"], ["ocean_freight 2500.00 container"]]
    assert _catch(s).cleared_reason == "rates"                                    # withdrawn, not savings

    client.post(reverse("rates:archive", args=[q.pk]))
    q.refresh_from_db()
    assert q.archived and AuditEvent.objects.filter(action="quote.archived").exists()
    r = client.get(reverse("rates:create") + f"?copy={q.pk}")
    assert r.status_code == 200 and r.context["form"].initial["valid_from"] == date(2027, 1, 1)
    client.post(reverse("rates:delete", args=[q.pk]))                             # not confirmed
    assert Quote.objects.filter(pk=q.pk).exists()
    client.post(reverse("rates:delete", args=[q.pk]), {"confirm": "delete"})
    assert not Quote.objects.filter(pk=q.pk).exists()
    assert AuditEvent.objects.get(action="quote.deleted").data["quote"]["reference"] == "NEW-1"


@pytest.mark.django_db
def test_quote_form_errors_save_nothing(client, org, approver):
    client.force_login(approver)
    r = client.post(reverse("rates:create"), _quote_post(valid_to="2025-01-01", currency="dollars",
                                                          vendor_name="LLC"))
    assert r.status_code == 200 and not Quote.objects.exists()
    errors = r.context["form"].errors
    assert {"valid_to", "currency", "vendor_name"} <= set(errors)
    r = client.post(reverse("rates:create"), _quote_post(**{"charges-0-code": "", "charges-0-amount": ""}))
    assert r.status_code == 200 and "Add at least one charge line." in r.content.decode()
    r = client.post(reverse("rates:create"), _quote_post(**{"charges-0-amount": "-1"}))
    assert "Use a positive amount" in r.content.decode() and not Quote.objects.exists()


@pytest.mark.django_db
def test_import_view_reports_rows_and_downloads_problems(client, org, approver):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.force_login(approver)
    bad = ",".join(csvio.COLUMNS) + "\nX,,,,,2026-01-01,,USD,,banana,,1,container,\n"
    r = client.post(reverse("rates:import"), {"file": SimpleUploadedFile("q.csv", bad.encode())})
    html = r.content.decode()
    assert r.status_code == 200 and "Nothing was imported" in html and "Unknown charge" in html
    report = client.get(reverse("rates:import_errors")).content.decode()
    assert report.splitlines()[0] == "row,column,value,problem" and "2,charge_code,banana" in report
    r = client.post(reverse("rates:import"), {"file": SimpleUploadedFile("q.xlsx", b"PK..")}, follow=True)
    assert "isn&#x27;t a CSV file" in r.content.decode()
    r = client.post(reverse("rates:import"), {"file": SimpleUploadedFile("q.csv", csvio.template_csv().encode())})
    assert r.status_code == 302 and Quote.objects.filter(organization=org).count() == 2
    export = client.get(reverse("rates:export")).content.decode()
    assert export.startswith(",".join(csvio.COLUMNS)) and "Q-2026-014" in export


@pytest.mark.django_db
def test_approved_extras_and_charge_names_views(client, org, approver):
    make_quote(org, vendor=MD, origin="", lines=[("trucking", "600.00", "container")])
    s, _ = make_shipment(org, vendor=MD, lines=[("Drayage", "1200.00"), ("Gate-in fee", "80.00")])
    assert rate_issues(s)[0].data["charge_code"] == "other"
    client.force_login(approver)
    client.post(reverse("rates:charge_names"), {"example": "Gate-in fee", "code": "trucking"})
    assert ChargeAlias.objects.get(organization=org, key="gate in fee").code == "trucking"
    # Re-checked at once: now 1,280.00 of trucking against 1,200.00 quoted, an over-quote error.
    assert [(i.code, i.amount_at_risk) for i in rate_issues(s)] == [("over_quote", D("80.00"))]
    alias = ChargeAlias.objects.get(organization=org)
    client.post(reverse("rates:charge_name_delete", args=[alias.pk]))
    assert not ChargeAlias.objects.filter(organization=org).exists()

    r = client.post(reverse("rates:extra_create"), {"vendor_name": MD, "code": "detention", "unit": "day",
                                                    "free_units": "4", "max_per_unit": "100", "max_amount": "",
                                                    "currency": "USD", "valid_from": "", "valid_to": "", "notes": ""})
    assert r.status_code == 302
    extra = ApprovedAccessorial.objects.get(organization=org)
    assert extra.terms == "after 4 free days, up to USD 100.00 a day"
    r = client.post(reverse("rates:extra_edit", args=[extra.pk]), {
        "vendor_name": MD, "code": "detention", "unit": "day", "free_units": "4", "max_per_unit": "-1",
        "currency": "USD"})
    assert r.status_code == 200 and "Use a positive amount" in r.content.decode()
    client.post(reverse("rates:extra_delete", args=[extra.pk]))
    assert not ApprovedAccessorial.objects.exists()
    actions = set(AuditEvent.objects.filter(organization=org).values_list("action", flat=True))
    assert {"charge_name.updated", "charge_name.deleted", "accessorial.created", "accessorial.deleted"} <= actions


@pytest.mark.django_db
def test_savings_page_tile_and_export(client, org, viewer):
    make_quote(org)
    make_shipment(org, lines=[("Ocean Freight", "5000.00")])
    client.force_login(viewer)
    dash = client.get(reverse("core:dashboard")).content.decode()
    assert "Overcharges caught this month" in dash and "200" in dash
    page = client.get(reverse("savings:summary"))
    assert page.status_code == 200 and page.context["s"].at_risk == D("200.00")
    custom = client.get(reverse("savings:summary"), {"period": "custom", "from": "2020-01-01", "to": "2020-01-31"})
    assert custom.context["s"].caught == 0 and custom.context["months_label"] == "Last 6 months"
    csv_text = client.get(reverse("savings:export")).content.decode()
    assert "Charged more than the quote" in csv_text and "200.00" in csv_text
    client.logout()
    assert client.get(reverse("savings:summary")).status_code == 302


@pytest.mark.django_db
def test_recheck_button_needs_approve(client, org, user, approver):
    make_quote(org)
    make_shipment(org)
    client.force_login(user)
    assert client.post(reverse("rates:recheck")).status_code == 403
    client.force_login(approver)
    r = client.post(reverse("rates:recheck"), follow=True)
    assert "Checked 1 open shipment" in r.content.decode()


# --------------------------------------------------------------------------- demo data


def test_generator_extra_charges_are_opt_in(tmp_path):
    from synthetic.generator import generate

    generate(tmp_path / "plain", n_shipments=8, seed=5)
    generate(tmp_path / "extras", n_shipments=8, seed=5, accessorials=True)
    plain = json.loads((tmp_path / "plain" / "ground_truth.json").read_text())
    extras = json.loads((tmp_path / "extras" / "ground_truth.json").read_text())
    assert not any("planted_charges" in d for d in plain["documents"])
    changed = [d for d in extras["documents"] if d.get("planted_charges")]
    assert changed
    by_file = {d["file"]: d for d in plain["documents"]}
    for d in extras["documents"]:
        before = by_file[d["file"]]
        if d.get("planted_charges"):
            added = len(d["planted_charges"])
            assert d["fields"]["line_items"][:-added] == before["fields"]["line_items"]
            assert D(d["fields"]["total_amount"]) - D(before["fields"]["total_amount"]) == sum(
                D(x["amount"]) for x in d["planted_charges"])
        else:
            assert d["fields"] == before["fields"]
    assert plain["shipments"] == extras["shipments"]


@pytest.fixture(scope="session")
def extras_dataset(tmp_path_factory):
    from synthetic.generator import generate

    out = tmp_path_factory.mktemp("synthetic_extras")
    generate(out, n_shipments=12, seed=42, accessorials=True)
    return out


@pytest.mark.django_db
def test_seed_rates_shows_real_catches(org, extras_dataset):
    from apps.documents.services.ingest import ingest_bytes

    for pdf in sorted((extras_dataset / "pdf").glob("*.pdf")):
        ingest_bytes(org, pdf.name, pdf.read_bytes(), process="sync")
    call_command("seed_rates", "--org", org.slug, stdout=io.StringIO())
    codes = list(org.issues.filter(code__in=RATE_CODES).values_list("code", flat=True))
    assert "over_quote" in codes and "unapproved_accessorial" in codes and "no_quote" in codes
    n_quotes = Quote.objects.filter(organization=org).count()
    call_command("seed_rates", "--org", org.slug, "--no-recheck", stdout=io.StringIO())
    assert Quote.objects.filter(organization=org).count() == n_quotes == 48
    s = savings.summary(org, date(2000, 1, 1), timezone.localdate() + timedelta(days=1))
    assert s.caught > 0 and s.top_vendors


@pytest.mark.django_db
def test_seed_rates_needs_an_existing_org():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("seed_rates", "--org", "nope")


# --------------------------------------------------------------------------- QA-068: quotes and extras that make no sense


def _quote_names(client):
    return list(Quote.objects.values_list("reference", flat=True))


@pytest.mark.django_db
@pytest.mark.parametrize("amount, words", [
    ("0", "above zero"), ("0.00", "above zero"), ("-5", "positive amount"),
    ("1000000.01", "no single charge"), ("1000000000", "no single charge"),
])
def test_a_quote_charge_must_be_a_sensible_amount(client, org, approver, amount, words):
    client.force_login(approver)
    r = client.post(reverse("rates:create"), _quote_post(**{"charges-0-amount": amount}), follow=True)
    assert words in r.content.decode()
    assert not Quote.objects.filter(organization=org).exists()


@pytest.mark.django_db
def test_the_largest_allowed_charge_is_accepted(client, org, approver):
    client.force_login(approver)
    client.post(reverse("rates:create"), _quote_post(**{"charges-0-amount": "1000000.00"}))
    assert Quote.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_the_same_charge_twice_with_the_same_basis_is_refused(client, org, approver):
    client.force_login(approver)
    data = _quote_post(**{"charges-1-code": "ocean_freight", "charges-1-amount": "2500.00"})
    r = client.post(reverse("rates:create"), data, follow=True)
    assert "listed twice" in r.content.decode()
    assert not Quote.objects.filter(organization=org).exists()


@pytest.mark.django_db
def test_the_same_charge_priced_on_a_different_basis_is_allowed(client, org, approver):
    client.force_login(approver)
    data = _quote_post(**{"charges-1-code": "ocean_freight", "charges-1-amount": "90.00", "charges-1-basis": "bl"})
    client.post(reverse("rates:create"), data)
    assert QuoteCharge.objects.filter(quote__organization=org).count() == 2


@pytest.mark.django_db
def test_an_identical_quote_is_refused_but_a_new_period_is_fine(client, org, approver):
    client.force_login(approver)
    assert client.post(reverse("rates:create"), _quote_post()).status_code == 302
    r = client.post(reverse("rates:create"), _quote_post(), follow=True)
    assert "already exists" in r.content.decode()
    assert Quote.objects.filter(organization=org).count() == 1

    assert client.post(reverse("rates:create"), _quote_post(valid_from="2027-01-01", valid_to="2027-12-31")).status_code == 302
    assert client.post(reverse("rates:create"), _quote_post(reference="OTHER-2")).status_code == 302
    assert Quote.objects.filter(organization=org).count() == 3


@pytest.mark.django_db
def test_editing_a_quote_does_not_clash_with_itself(client, org, approver):
    client.force_login(approver)
    client.post(reverse("rates:create"), _quote_post())
    quote = Quote.objects.get(organization=org)
    edit = _quote_post(notes="changed", **{"charges-TOTAL_FORMS": "3", "charges-INITIAL_FORMS": "1",
                                           "charges-0-id": quote.charges.get().pk, "charges-0-quote": quote.pk,
                                           "charges-2-code": "", "charges-2-description": "",
                                           "charges-2-amount": "", "charges-2-basis": "container"})
    r = client.post(reverse("rates:edit", args=[quote.pk]), edit)
    assert r.status_code == 302
    quote.refresh_from_db()
    assert quote.notes == "changed"


@pytest.mark.django_db
def test_an_extra_cannot_be_added_twice_and_has_a_sane_cap(client, org, approver):
    client.force_login(approver)
    base = {"vendor_name": MD, "code": "detention", "unit": "day", "free_units": "4", "max_per_unit": "100",
            "currency": "USD", "valid_from": "2026-01-01"}
    assert client.post(reverse("rates:extra_create"), base).status_code == 302
    r = client.post(reverse("rates:extra_create"), base, follow=True)
    assert "already exists" in r.content.decode()
    assert ApprovedAccessorial.objects.filter(organization=org).count() == 1
    r = client.post(reverse("rates:extra_create"), {**base, "valid_from": "2027-01-01", "max_per_unit": "99999999"},
                    follow=True)
    assert "unlikely" in r.content.decode()
    assert ApprovedAccessorial.objects.filter(organization=org).count() == 1
