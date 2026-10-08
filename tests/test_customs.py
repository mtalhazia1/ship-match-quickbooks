"""Customs entries and free time: reading, matching, duty and fee checks, last free days, alerts, permissions."""
import json
import shutil
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from django.urls import reverse

from apps.core.models import AuditEvent
from apps.customs.fees import FeeTableError, expected_mpf, mpf_rate_on, parse_override
from apps.customs.models import ContainerFreeTime, CustomsSettings, HtsReview
from apps.customs.services import alerts, freetime
from apps.customs.services.charges import duty_charges
from apps.customs.services.checks import (
    check_duty_math,
    check_hts_codes,
    check_user_fees,
    country_code,
)
from apps.documents.models import Document
from apps.documents.services import llm
from apps.documents.services.classify import classify
from apps.documents.services.corrections import EDITABLE
from apps.documents.services.extract import extract
from apps.documents.services.ingest import ingest_bytes
from apps.documents.services.ocr import read_text
from apps.notifications import delivery as delivery_mod
from apps.notifications import events
from apps.notifications.models import Channel, Delivery
from apps.shipments.models import MatchLink, Shipment
from apps.shipments.services.containers import make_container
from apps.shipments.services.evidence import issue_targets
from apps.shipments.services.validation import validate_shipment
from synthetic import customs as samples
from synthetic.generator import CARRIERS, Page

BL = "OSLN7712345678"
BOX = make_container("OSL", 712345)
BOX2 = make_container("OSL", 712346)
ENTRY = "HLB-2604117-3"


def text_of(pdf: bytes) -> str:
    return read_text(pdf).text


def ingest(org, name, pdf):
    doc, _ = ingest_bytes(org, name, pdf, process="sync")
    doc.refresh_from_db()
    return doc


def codes(shipment) -> dict:
    return {i.code: i for i in shipment.issues.filter(resolved=False)}


@pytest.fixture
def entry_shipment(org):
    """Commercial invoice (USD 23,325.00, origin China) and a matching, correct entry summary."""
    ingest(org, "ci.pdf", samples.commercial_invoice("CI-77001", bl=BL, containers=[BOX])[0])
    ingest(org, "entry.pdf", samples.cbp7501(ENTRY, bl=BL, containers=[BOX])[0])
    return Shipment.objects.get(organization=org)


# --------------------------------------------------------------------------- classification and reading


def test_entry_summary_is_classified_and_read():
    pdf, truth = samples.cbp7501(ENTRY, bl=BL, containers=[BOX], invoice_currency="EUR",
                                 exchange_rate=Decimal("1.0850"))
    text = text_of(pdf)
    assert classify(text)[0] == "customs_entry"
    fields, provider = extract("customs_entry", text)
    got = {f.name: f for f in fields}
    assert provider == "rules"
    for name in ("entry_number", "entry_date", "import_date", "port_of_entry", "importer_name", "bl_number",
                 "container_numbers", "country_of_origin", "total_entered_value", "total_duty",
                 "merchandise_processing_fee", "harbor_maintenance_fee", "total_duty_and_fees", "invoice_currency"):
        assert got[name].value == truth[name], name
        assert got[name].grounded and got[name].confidence >= 0.9, name
    assert got["exchange_rate"].value == "1.0850"
    lines = got["entry_lines"].value
    assert [ln["hts_code"] for ln in lines] == ["8518.22.0000", "9903.88.15", "8504.40.9550"]
    assert lines[1].get("entered_value") is None and lines[1]["duty_rate"] == "7.5%"  # Chapter 99: no own value
    assert lines[2]["duty_rate"] == "Free" and lines[2]["duty_amount"] == "0.00"
    assert got["entry_lines"].grounded
    assert got["broker_name"].value == "Harborlink Customs Brokerage LLC"  # address cut off


def test_generic_declaration_is_read():
    pdf, truth = samples.customs_declaration()
    text = text_of(pdf)
    assert classify(text)[0] == "customs_entry"
    got = {f.name: f.value for f in extract("customs_entry", text)[0]}
    assert got["entry_number"] == truth["entry_number"]
    assert got["currency"] == "EUR" and got["total_duty"] == "660.00"
    assert [ln["hts_code"] for ln in got["entry_lines"]] == truth["hts_codes"]


def test_arrival_notice_is_classified_and_read():
    pdf, truth = samples.arrival_notice(BL, [BOX, BOX2], discharge=date(2026, 4, 7), printed_lfd=True,
                                        lfd={BOX: date(2026, 4, 13), BOX2: date(2026, 4, 14)})
    text = text_of(pdf)
    assert classify(text)[0] == "arrival_notice"
    got = {f.name: f for f in extract("arrival_notice", text)[0]}
    for name in ("carrier_name", "bl_number", "container_numbers", "estimated_arrival_date", "terminal",
                 "port_of_discharge", "vessel_voyage", "discharge_date", "demurrage_free_days", "detention_free_days",
                 "total_amount", "notice_date"):
        assert got[name].value == truth[name], name
    assert got["free_time_basis"].value == "working days"
    assert got["container_dates"].value == [
        {"container_number": BOX, "discharge_date": "2026-04-07", "demurrage_last_free_day": "2026-04-13"},
        {"container_number": BOX2, "discharge_date": "2026-04-07", "demurrage_last_free_day": "2026-04-14"}]
    assert [li["amount"] for li in got["line_items"].value] == ["450.00", "75.00"]


def test_broker_invoices_stay_freight_invoices():
    broker = ("Harborlink Customs Brokerage LLC\nINVOICE\nInvoice No.: HB-2231\nCustoms Entry No.: HLB-2604117-3\n"
              "Description Amount\nCustoms clearance 125.00\nDuty (disbursement) 2,287.80\n"
              "Merchandise Processing Fee 80.80\nHarbor Maintenance Fee 29.16\nTotal due: 2,522.76\nPlease remit")
    assert classify(broker)[0] == "freight_invoice"
    combined = ("Oceanic Star Line\nARRIVAL NOTICE AND FREIGHT INVOICE\nInvoice No.: OS-1\nOcean freight 2,150.00\n"
                "Terminal handling 310.00\nTotal due: 2,460.00\nRemit to Oceanic")
    assert classify(combined)[0] == "freight_invoice"


def test_default_dataset_types_are_unchanged(dataset):
    truth = json.loads((dataset / "ground_truth.json").read_text())
    assert not any(d["doc_type"] in ("customs_entry", "arrival_notice") for d in truth["documents"])
    for d in truth["documents"]:
        if d["scanned"]:
            continue
        assert classify(text_of((dataset / "pdf" / d["file"]).read_bytes()))[0] == d["doc_type"], d["file"]


def test_ai_reading_of_an_entry(settings, monkeypatch):
    settings.EXTRACTION_PROVIDER, settings.ANTHROPIC_API_KEY, settings.LLM_INPUT = "anthropic", "k", "text"
    pdf, truth = samples.cbp7501(ENTRY, bl=BL, containers=[BOX])
    seen = {}

    def fake(system, user, schema, name="record", timeout=120.0, client=None, pdf=None, purpose="extract"):
        seen["schema"] = schema
        return {"entry_number": ENTRY, "entry_date": "2026-04-02", "total_duty": 2287.8, "total_entered_value": 23325,
                "entry_lines": [{"line_number": "001", "hts_code": "8518.22.0000", "description": "Bluetooth speaker BX-20",
                                 "entered_value": 18450, "duty_rate": "4.9%", "duty_amount": 904.05}],
                "container_numbers": [BOX], "bl_number": BL, "merchandise_processing_fee": "not a number"}

    monkeypatch.setattr(llm, "structured_call", fake)
    got = {f.name: f for f in extract("customs_entry", text_of(pdf))[0]}
    lines = seen["schema"]["properties"]["entry_lines"]
    assert lines["items"]["additionalProperties"] is False and "duty_rate" in lines["items"]["required"]
    assert "pattern" not in json.dumps(seen["schema"]) and "$ref" not in json.dumps(seen["schema"])
    assert got["entry_lines"].value[0]["hts_code"] == "8518.22.0000" and got["entry_lines"].grounded
    assert got["total_duty"].grounded and "merchandise_processing_fee" not in got  # unparseable value dropped


# --------------------------------------------------------------------------- matching


def test_entry_and_notice_join_the_shipment_by_bl(org, dataset):
    for f in ["S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf"]:
        ingest(org, f, (dataset / "pdf" / f).read_bytes())
    s = Shipment.objects.get(organization=org)
    entry = ingest(org, "entry.pdf", samples.cbp7501(ENTRY, bl=s.bl_number, containers=s.container_numbers)[0])
    notice = ingest(org, "an.pdf", samples.arrival_notice(s.bl_number, s.container_numbers, discharge=date(2026, 4, 7))[0])
    assert Shipment.objects.filter(organization=org).count() == 1
    assert entry.doc_type == "customs_entry" and entry.match.method == MatchLink.Method.EXACT_BL
    assert notice.doc_type == "arrival_notice" and notice.match.shipment_id == s.pk
    assert not entry.is_payable and not entry.posts_to_accounting


def test_entry_joins_by_container_when_the_bl_differs(org, entry_shipment):
    doc = ingest(org, "entry-hbl.pdf", samples.cbp7501("HLB-2609999-1", bl="HBL55501234", containers=[BOX])[0])
    assert doc.match.shipment_id == entry_shipment.pk and doc.match.method == MatchLink.Method.EXACT_CONTAINER


def test_corrected_entry_joins_by_entry_number(org, entry_shipment):
    pdf, _ = samples.cbp7501(ENTRY, bl="", containers=[], errors=("mpf",))
    doc = ingest(org, "entry-corrected.pdf", pdf)
    assert doc.match.shipment_id == entry_shipment.pk
    assert doc.match.method == MatchLink.Method.ENTRY_NUMBER and "same customs entry" in doc.match.reason
    assert "customs_entry_changed" in codes(entry_shipment)


def _broker_invoice(entry_number: str) -> bytes:
    p = Page(0)
    p.line((50, "Harborlink Customs Brokerage LLC"), (545, "INVOICE", "r"), size=13, bold=True, step=16)
    p.line((50, "Invoice No.:"), (170, "HB-2231"))
    p.line((50, "Invoice Date:"), (170, "2026-04-03"))
    p.line((50, "Customs Entry No.:"), (170, entry_number))
    p.line((50, "Description"), (545, "Amount (USD)", "r"), bold=True)
    p.line((50, "Customs clearance"), (545, "125.00", "r"))
    p.line((50, "ISF filing"), (545, "35.00", "r"))
    p.line((50, "Duty (disbursement)"), (545, "2,287.80", "r"))
    p.line((330, "Total due:"), (545, "2,447.80", "r"), bold=True)
    p.line((50, "Please remit to Harborlink Customs Brokerage LLC."), size=8)
    return p.pdf()


def test_broker_invoice_joins_by_the_entry_number_it_prints(org, entry_shipment):
    doc = ingest(org, "broker.pdf", _broker_invoice(ENTRY))
    assert doc.doc_type == "freight_invoice"
    assert doc.match.shipment_id == entry_shipment.pk and doc.match.method == MatchLink.Method.ENTRY_NUMBER


def test_entry_with_no_reference_waits_unmatched(org, entry_shipment):
    doc = ingest(org, "orphan.pdf", samples.cbp7501("ZZZ-9990001-5", bl="", containers=[])[0])
    assert doc.status == Document.Status.UNMATCHED and not hasattr(doc, "match")


# --------------------------------------------------------------------------- checks


def test_correct_entry_raises_no_customs_issue(entry_shipment):
    found = codes(entry_shipment)
    customs = {c for c in found if c not in {"missing_bl", "low_confidence"}}
    assert customs == set(), found


def test_overstated_line_duty_is_an_error_with_money_at_risk(org):
    ingest(org, "entry.pdf", samples.cbp7501(ENTRY, bl=BL, containers=[BOX], errors=("line_duty",))[0])
    s = Shipment.objects.get(organization=org)
    issue = codes(s)["duty_line_mismatch"]
    assert issue.severity == "error" and issue.amount_at_risk == Decimal("100.00") and issue.currency == "USD"
    assert "4.9% of USD 18,450.00 is USD 904.05" in issue.message
    assert issue_targets(issue) == (["entry_lines=0"], "line 001")
    loc = issue.document.fields.get(name="entry_lines").location
    assert loc["status"] == "found" and loc["items"][0]["amount"]  # evidence: the line's duty is boxed
    assert "duty_total_mismatch" not in codes(s)  # the printed total follows the printed lines


def _doc(doc_type="customs_entry"):
    return SimpleNamespace(doc_type=doc_type, original_filename="e.pdf", pk=1)


def _entry(**kw):
    data = {"entry_number": ENTRY, "entry_date": "2026-04-02", "currency": "USD",
            "entry_lines": [{"line_number": "1", "hts_code": "8518.22.0000", "description": "Speakers",
                             "entered_value": "10000.00", "duty_rate": "4.9%", "duty_amount": "490.00"}]}
    data.update(kw)
    return data


@pytest.mark.parametrize("stated,flagged,severity", [
    ("491.00", False, None),      # exactly the tolerance (1.00): rounding
    ("491.01", True, "error"),    # one cent more: overcharged
    ("489.00", False, None),
    ("488.99", True, "warning"),  # understated: CBP may bill, nothing overpaid
])
def test_line_duty_tolerance_edges(stated, flagged, severity):
    data = _entry()
    data["entry_lines"][0]["duty_amount"] = stated
    found = [s for s in check_duty_math(_doc(), data) if s.code == "duty_line_mismatch"]
    assert bool(found) == flagged
    if found:
        assert found[0].severity == severity
        assert found[0].amount_at_risk == (Decimal(stated) - Decimal("490.00") if severity == "error" else None)


def test_totals_and_grand_total_are_checked():
    data = _entry(total_duty="740.00", merchandise_processing_fee="34.64", harbor_maintenance_fee="12.50",
                  total_duty_and_fees="900.00", total_entered_value="10000.00")
    found = {s.code: s for s in check_duty_math(_doc(), data)}
    assert found["duty_total_mismatch"].amount_at_risk == Decimal("250.00")
    assert found["duty_fees_total_mismatch"].amount_at_risk == Decimal("900.00") - Decimal("787.14")
    assert "entered_value_total_mismatch" not in found


def test_entered_values_that_do_not_add_up_and_understated_totals():
    data = _entry(total_entered_value="12000.00", total_duty="400.00", total_duty_and_fees="300.00")
    found = {s.code: s for s in check_duty_math(_doc(), data)}
    assert found["entered_value_total_mismatch"].severity == "warning"
    assert found["duty_total_mismatch"].severity == "warning" and found["duty_total_mismatch"].amount_at_risk is None
    assert found["duty_fees_total_mismatch"].severity == "warning"
    assert list(check_duty_math(_doc("arrival_notice"), data)) == []  # only customs entries are checked


def test_chapter_99_and_specific_rates():
    data = _entry(entry_lines=[
        {"hts_code": "8518.22.0000", "entered_value": "10000.00", "duty_rate": "4.9%", "duty_amount": "490.00"},
        {"hts_code": "9903.88.15", "duty_rate": "7.5%", "duty_amount": "750.00"},          # uses 10,000.00
        {"hts_code": "0805.10.0020", "entered_value": "500.00", "duty_rate": "1.9¢/kg", "duty_amount": "12.00"},
    ])
    assert list(check_duty_math(_doc(), data)) == []
    data["entry_lines"][1]["duty_amount"] = "2500.00"   # 25% instead of 7.5%
    issue = next(check_duty_math(_doc(), data))
    assert issue.code == "duty_line_mismatch" and issue.amount_at_risk == Decimal("1750.00")
    assert "the value of the line above" in issue.message


@pytest.mark.parametrize("value,day,stated,code_key,at_risk", [
    ("500000.00", "2026-04-02", "1732.00", "above_max", "1080.50"),   # FY2026 maximum 651.50
    ("500000.00", "2026-04-02", "651.50", None, None),
    ("500000.00", "2026-09-30", "670.86", "above_max", "19.36"),      # last day of FY2026
    ("500000.00", "2026-10-01", "670.86", None, None),                # FY2027 maximum 670.86
    ("300000.00", "2023-09-30", "614.35", "above_max", "39.00"),      # FY2023 maximum 575.35
    ("300000.00", "2023-10-01", "614.35", None, None),                # FY2024 maximum 614.35
    ("2000.00", "2026-04-02", "6.93", "below_min", None),             # 0.3464% under the minimum 33.58
    ("2000.00", "2026-04-02", "33.58", None, None),
    ("20000.00", "2026-04-02", "70.28", None, None),                  # 69.28 + exactly the tolerance
    ("20000.00", "2026-04-02", "70.29", "rate", "1.01"),
])
def test_mpf_bounds_follow_the_entry_date(value, day, stated, code_key, at_risk):
    data = _entry(entry_date=day, total_entered_value=value, merchandise_processing_fee=stated, entry_lines=[])
    found = [s for s in check_user_fees(_doc(), data) if s.code == "mpf_incorrect"]
    assert [s.data["key"] for s in found] == ([code_key] if code_key else [])
    if found:
        assert found[0].amount_at_risk == (Decimal(at_risk) if at_risk else None)
        assert found[0].data["source"]


def test_mpf_table_is_dated_with_sources_and_can_be_overridden(settings):
    assert mpf_rate_on(date(2025, 10, 1)).maximum == Decimal("651.50")
    assert mpf_rate_on(date(2017, 12, 31)).minimum == Decimal("25.00")
    assert "91 FR 46530" in mpf_rate_on(date(2026, 10, 1)).source
    settings.CUSTOMS_MPF_TABLE = json.dumps([{"from": "2027-10-01", "min": "35.50", "max": "690.00"},
                                             {"from": "2026-10-01", "min": "34.58", "max": "671.00"}])
    assert mpf_rate_on(date(2027, 10, 2)).maximum == Decimal("690.00")
    assert mpf_rate_on(date(2026, 10, 2)).maximum == Decimal("671.00")
    assert expected_mpf(Decimal("1000000"), date(2027, 11, 1))[0] == Decimal("690.00")


@pytest.mark.parametrize("raw,words", [
    ("{not json", "not valid JSON"), ('{"from": "2027-10-01"}', "must be a JSON list"),
    ('[{"from": "Oct 2027", "min": "1", "max": "2"}]', "must be a date"),
    ('[{"from": "2027-10-01", "min": "9", "max": "2"}]', "min is larger than max"),
    ('[{"from": "2027-10-01", "min": "x", "max": "2"}]', "is not a number"),
])
def test_bad_mpf_override_is_reported_by_manage_py_check(settings, raw, words):
    from apps.customs.checks import customs_fee_settings

    with pytest.raises(FeeTableError, match=words):
        parse_override(raw)
    settings.CUSTOMS_MPF_TABLE = raw
    errors = customs_fee_settings()
    assert [e.id for e in errors] == ["customs.E001"] and words in errors[0].msg


def test_hmf_rate_is_checked(org):
    ingest(org, "entry.pdf", samples.cbp7501(ENTRY, bl=BL, containers=[BOX], errors=("hmf",))[0])
    issue = codes(Shipment.objects.get(organization=org))["hmf_incorrect"]
    assert issue.severity == "error" and issue.amount_at_risk == Decimal("5.83")  # 34.99 - 29.16


@pytest.mark.parametrize("code,data,ok", [
    ("8518.22.0000", {}, True), ("8518220000", {}, True), ("9903.88.15", {}, True),
    ("8518.22.000", {}, False), ("8518.22", {}, False), ("7700.00.0000", {}, False),
    ("85182200", {"entry_number": "26NL0003961234567A"}, True),        # another country: 6 to 10 digits
    ("85182", {"entry_number": "26NL0003961234567A"}, False),
])
def test_tariff_number_format(code, data, ok):
    entry = _entry(**data)
    entry["entry_lines"][0]["hts_code"] = code
    found = list(check_hts_codes(_doc(), entry))
    assert (not found) == ok
    if found:
        assert found[0].code == "hts_code_format" and found[0].data["evidence"]["targets"] == ["entry_lines=0"]


def test_duplicate_entry_is_an_error_and_a_correction_a_warning(org, entry_shipment):
    pdf, truth = samples.cbp7501(ENTRY, bl=BL, containers=[BOX], vessel="MV Coral Dawn / resent")
    ingest(org, "entry-again.pdf", pdf)
    dup = codes(entry_shipment)["duplicate_customs_entry"]
    assert dup.severity == "error" and dup.amount_at_risk == Decimal(truth["total_duty_and_fees"])
    from apps.rates.savings import WHOLE_INVOICE

    assert "duplicate_customs_entry" in WHOLE_INVOICE  # counted once on Savings
    ingest(org, "entry-psc.pdf", samples.cbp7501(ENTRY, bl=BL, containers=[BOX], errors=("mpf",))[0])
    changed = codes(entry_shipment)["customs_entry_changed"]
    assert changed.severity == "warning" and changed.amount_at_risk is None


def test_entered_value_against_the_invoice_in_another_currency(org):
    ingest(org, "ci.pdf", samples.commercial_invoice("CI-EU", bl=BL, containers=[BOX], currency="EUR",
                                                      items=[("Cotton bath towel 70x140", 4000, "5.00")])[0])
    lines = [samples.Line("6302.60.0010", "Cotton bath towel 70x140", Decimal("21700.00"), "9.1%")]
    ingest(org, "entry.pdf", samples.cbp7501(ENTRY, bl=BL, containers=[BOX], lines=lines, origin="CN",
                                             invoice_currency="EUR", exchange_rate=Decimal("1.0850"))[0])
    s = Shipment.objects.get(organization=org)
    assert not {"entered_value_low", "entered_value_high"} & set(codes(s))  # EUR 20,000 x 1.0850 = USD 21,700


@pytest.mark.parametrize("entered,code,severity", [("18000.00", "entered_value_low", "error"),
                                                     ("26000.00", "entered_value_high", "warning")])
def test_under_and_overvaluation(org, entered, code, severity):
    ingest(org, "ci.pdf", samples.commercial_invoice("CI-1", bl=BL, containers=[BOX],
                                                      items=[("Bluetooth speaker BX-20", 1000, "21.70")])[0])
    lines = [samples.Line("8518.22.0000", "Bluetooth speaker BX-20", Decimal(entered), "4.9%")]
    ingest(org, "entry.pdf", samples.cbp7501(ENTRY, bl=BL, containers=[BOX], lines=lines)[0])
    issue = codes(Shipment.objects.get(organization=org))[code]
    assert issue.severity == severity and "USD 21,700.00" in issue.message
    if code == "entered_value_high":
        # 4.9% duty, 0.125% HMF and 0.3464% MPF on the USD 4,300.00 difference
        assert issue.amount_at_risk == Decimal("210.70") + Decimal("5.37") + Decimal("14.89")
    else:
        assert issue.amount_at_risk is None


def test_no_exchange_rate_means_no_value_comparison(org):
    ingest(org, "ci.pdf", samples.commercial_invoice("CI-2", bl=BL, containers=[BOX], currency="EUR")[0])
    ingest(org, "entry.pdf", samples.cbp7501(ENTRY, bl=BL, containers=[BOX])[0])
    assert not {"entered_value_low", "entered_value_high"} & set(codes(Shipment.objects.get(organization=org)))


def test_country_of_origin_against_the_invoice(org):
    ingest(org, "ci.pdf", samples.commercial_invoice("CI-3", bl=BL, containers=[BOX], origin_country="Viet Nam")[0])
    ingest(org, "entry.pdf", samples.cbp7501(ENTRY, bl=BL, containers=[BOX], origin="CN")[0])
    issue = codes(Shipment.objects.get(organization=org))["origin_mismatch"]
    assert issue.severity == "warning" and "says VN" in issue.message
    assert country_code("People's Republic of China") == "CN" and country_code("cn") == "CN"


def test_ai_tariff_check_warns_once_and_falls_back_quietly(org, monkeypatch, django_capture_on_commit_callbacks):
    from apps.customs.services import hts_ai

    calls = []

    def fake(system, user, schema, name="record", timeout=120.0, client=None, pdf=None, purpose="extract"):
        calls.append(purpose)
        assert schema["properties"]["lines"]["items"]["additionalProperties"] is False
        assert "Cotton bath towels" in user and "HTSUS" in user
        return {"lines": [{"index": 0, "plausible": False, "reason": "8518.22 covers loudspeakers, not towels"},
                          {"index": 1, "plausible": True, "reason": "Static converters"}]}

    monkeypatch.setattr(hts_ai, "enabled", lambda: True)   # AI on for this check only; reading stays rule-based
    monkeypatch.setattr(llm, "structured_call", fake)
    lines = [samples.Line("8518.22.0000", "Cotton bath towels", Decimal("1000.00"), "4.9%"),
             samples.Line("8504.40.9550", "USB-C charger 65W", Decimal("500.00"), "Free")]
    with django_capture_on_commit_callbacks(execute=True):  # runs right after reading, outside a transaction
        ingest(org, "entry.pdf", samples.cbp7501(ENTRY, bl=BL, containers=[BOX], lines=lines)[0])
    s = Shipment.objects.get(organization=org)
    validate_shipment(s)
    issue = codes(s)["hts_description_doubt"]
    assert issue.severity == "warning" and "covers loudspeakers" in issue.message
    assert calls == ["customs_hts"] and HtsReview.objects.count() == 2
    validate_shipment(s)  # checking again reads the saved answers
    assert calls == ["customs_hts"] and "hts_description_doubt" in codes(s)

    def broken(*a, **k):
        raise llm.LLMError("overloaded")

    monkeypatch.setattr(llm, "structured_call", broken)
    lines2 = [samples.Line("6302.60.0010", "Steel bolts", Decimal("800.00"), "9.1%")]
    with django_capture_on_commit_callbacks(execute=True):
        doc = ingest(org, "entry2.pdf", samples.cbp7501("HLB-2604118-1", bl=BL, containers=[BOX], lines=lines2)[0])
    validate_shipment(s)
    assert doc.status == "matched" and HtsReview.objects.count() == 2  # no answer, no new warning, no failure
    assert [i.document_id for i in s.issues.filter(code="hts_description_doubt")] != [doc.pk]


def test_ai_tariff_check_is_off_without_ai():
    from apps.customs.services import hts_ai

    assert hts_ai.enabled() is False  # tests read with EXTRACTION_PROVIDER=rules


def test_arrival_notice_with_impossible_dates_is_flagged(org):
    pdf, _ = samples.arrival_notice(BL, [BOX], discharge=date(2026, 4, 7), printed_lfd=True, lfd={BOX: date(2026, 4, 3)})
    ingest(org, "an.pdf", pdf)
    assert codes(Shipment.objects.get(organization=org))["free_time_dates"].severity == "warning"


# --------------------------------------------------------------------------- last free day rules


@pytest.mark.parametrize("start,days,weekends,holidays,expected", [
    (date(2026, 4, 7), 4, False, set(), date(2026, 4, 13)),     # Tue + 4 working days = Mon
    (date(2026, 4, 7), 4, True, set(), date(2026, 4, 11)),      # calendar days = Sat
    (date(2026, 4, 10), 1, False, set(), date(2026, 4, 13)),    # discharged Friday: Monday is day 1
    (date(2026, 4, 11), 2, False, set(), date(2026, 4, 14)),    # discharged Saturday
    (date(2026, 4, 7), 4, False, {date(2026, 4, 13)}, date(2026, 4, 14)),   # a holiday isn't counted
    (date(2026, 4, 7), 4, True, {date(2026, 4, 9)}, date(2026, 4, 11)),     # calendar days count holidays
    (date(2026, 4, 7), 0, False, set(), date(2026, 4, 7)),
])
def test_last_free_day(start, days, weekends, holidays, expected):
    assert freetime.last_free_day(start, days, weekends, holidays) == expected


@pytest.mark.parametrize("basis,default,weekends", [
    ("calendar days", False, True), ("working days", True, False), ("excluding Saturdays, Sundays", True, False),
    (None, False, False), (None, True, True), ("", False, False),
])
def test_how_free_days_are_counted_is_explicit(basis, default, weekends):
    counted, why = freetime.counts_weekends(basis, default)
    assert counted is weekends and why


def test_notice_free_days_become_last_free_days(org):
    ingest(org, "an.pdf", samples.arrival_notice(BL, [BOX, BOX2], discharge=date(2026, 4, 7), basis="working")[0])
    rows = {r.container_number: r for r in ContainerFreeTime.objects.filter(organization=org)}
    row = rows[BOX]
    assert row.lfd_demurrage == date(2026, 4, 13) and not row.lfd_demurrage_printed
    assert row.lfd_detention is None and row.detention_free_days == 5  # detention starts at pickup
    assert row.carrier_name == "Oceanic Star Line" and row.terminal == "Pier T Container Terminal"
    assert "working days" in row.basis_note and set(rows) == {BOX, BOX2}


def test_printed_last_free_day_wins_and_calendar_notices(org):
    ingest(org, "an.pdf", samples.arrival_notice(BL, [BOX], discharge=date(2026, 4, 7), basis="calendar",
                                                 printed_lfd=True, lfd={BOX: date(2026, 4, 15)})[0])
    row = ContainerFreeTime.objects.get(organization=org)
    assert row.lfd_demurrage == date(2026, 4, 15) and row.lfd_demurrage_printed and row.count_weekends


def test_eta_only_is_an_estimate_and_an_updated_notice_wins(org, user, client):
    ingest(org, "an1.pdf", samples.arrival_notice(BL, [BOX], eta=date(2026, 4, 6), basis="")[0])
    row = ContainerFreeTime.objects.get(organization=org)
    assert row.discharge_estimated and row.lfd_demurrage == date(2026, 4, 10)  # Mon + 4 working days
    assert "doesn't say" in row.basis_note
    row.picked_up_on = date(2026, 4, 9)
    row.save()
    ingest(org, "an2.pdf", samples.arrival_notice(BL, [BOX], eta=date(2026, 4, 6), discharge=date(2026, 4, 8),
                                                  notice_date=date(2026, 4, 8), basis="working")[0])
    row = ContainerFreeTime.objects.get(organization=org)
    assert not row.discharge_estimated and row.lfd_demurrage == date(2026, 4, 14)
    assert row.picked_up_on == date(2026, 4, 9)  # a person's dates survive a new notice


def test_today_and_countdown_use_the_organization_time_zone(org):
    late_evening_utc = datetime(2026, 4, 12, 23, 30, tzinfo=dt_timezone.utc)
    org.timezone = "America/Los_Angeles"
    assert freetime.org_today(org, late_evening_utc) == date(2026, 4, 12)
    org.timezone = "Asia/Tokyo"
    assert freetime.org_today(org, late_evening_utc) == date(2026, 4, 13)
    org.timezone = "Not/AZone"
    assert freetime.org_today(org, late_evening_utc) == date(2026, 4, 12)  # unknown zone: UTC
    row = ContainerFreeTime(organization=org, container_number=BOX, lfd_demurrage=date(2026, 4, 13))
    assert freetime.status_for(row, date(2026, 4, 12), 2, with_cost=False).text == "1 day left"
    assert freetime.status_for(row, date(2026, 4, 13), 2, with_cost=False).text == "Last free day today"
    late = freetime.status_for(row, date(2026, 4, 16), 2, with_cost=False)
    assert late.tone == "late" and late.text == "Demurrage for 3 days"


# --------------------------------------------------------------------------- alerts


SLACK_URL = "https://hooks.slack.com/services/T0000/B0000/abcdefghijklmnop"


@pytest.fixture
def hooks(monkeypatch, settings):
    settings.SITE_URL = "https://shipmatch.example.com"
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(delivery_mod, "_TRANSPORT", httpx.MockTransport(handler))
    return sent


@pytest.fixture
def notice_row(org):
    ingest(org, "an.pdf", samples.arrival_notice(BL, [BOX], discharge=date(2026, 4, 7), basis="working")[0])
    return ContainerFreeTime.objects.get(organization=org)   # last free day Mon 13 Apr 2026


def _at(day: date, hour: int = 9) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=dt_timezone.utc)


def test_last_free_day_alert_is_sent_once_per_container_and_date(org, notice_row, hooks,
                                                                 django_capture_on_commit_callbacks):
    Channel.objects.create(organization=org, kind="slack", name="ops", webhook_url=SLACK_URL,
                           events=[alerts.EVENT_SOON, alerts.EVENT_LATE])
    assert alerts.check_org(org, _at(date(2026, 4, 10))) == 0          # 3 days left, lead is 2
    assert alerts.check_org(org, _at(date(2026, 4, 11), hour=6)) == 0  # before CUSTOMS_ALERT_HOUR
    with django_capture_on_commit_callbacks(execute=True):
        assert alerts.check_org(org, _at(date(2026, 4, 11))) == 1
        assert alerts.check_org(org, _at(date(2026, 4, 11), hour=15)) == 0
        assert alerts.check_org(org, _at(date(2026, 4, 12))) == 0       # same last free day: once
    assert AuditEvent.objects.filter(action="free_time.lfd_soon").count() == 1
    assert hooks[0]["blocks"][0]["text"]["text"] == f"Last free day in 2 days: {BOX}"
    assert "by 13 Apr 2026 to avoid demurrage" in hooks[0]["blocks"][1]["text"]["text"]
    # an updated notice moves the last free day: the team hears about the new date
    ContainerFreeTime.objects.filter(pk=notice_row.pk).update(lfd_demurrage=date(2026, 4, 14))
    assert alerts.check_org(org, _at(date(2026, 4, 12))) == 1


def test_free_time_passed_alert_estimates_the_daily_cost(org, notice_row, hooks, django_capture_on_commit_callbacks):
    from apps.rates.models import ApprovedAccessorial

    ApprovedAccessorial.objects.create(organization=org, vendor_name="Oceanic Star Line", code="demurrage", unit="day",
                                       free_units=4, max_per_unit=Decimal("175.00"), currency="USD")
    Channel.objects.create(organization=org, kind="slack", name="ops", webhook_url=SLACK_URL,
                           events=[alerts.EVENT_LATE])
    with django_capture_on_commit_callbacks(execute=True):
        assert alerts.check_org(org, _at(date(2026, 4, 15))) == 1   # 2 days past
        assert alerts.check_org(org, _at(date(2026, 4, 16))) == 0
    body = hooks[0]["blocks"]
    assert body[0]["text"]["text"] == f"Free time passed: demurrage is accruing on {BOX}"
    assert "USD 175.00 a day" in body[1]["text"]["text"] and "USD 350.00 so far" in body[1]["text"]["text"]
    assert Delivery.objects.filter(event=alerts.EVENT_LATE, status="sent").count() == 1


def test_no_rate_on_file_degrades_gracefully(org, notice_row):
    alerts.check_org(org, _at(date(2026, 4, 15)))
    e = AuditEvent.objects.get(action="free_time.accruing")
    msg = events.audit_message(alerts.EVENT_LATE, e)
    assert "No approved demurrage rate is on file for Oceanic Star Line" in msg.text


def test_detention_alerts_after_pickup_and_none_after_return(org, notice_row):
    notice_row.picked_up_on = date(2026, 4, 9)
    freetime.recompute(notice_row)
    notice_row.save()
    assert notice_row.lfd_detention == date(2026, 4, 16)    # Thu + 5 working days
    alerts.check_org(org, _at(date(2026, 4, 15)))
    e = AuditEvent.objects.get(action="free_time.lfd_soon")
    assert e.data["stage"] == "detention" and "Return the empty" in events.audit_message(alerts.EVENT_SOON, e).text
    notice_row.returned_on = date(2026, 4, 15)
    notice_row.save()
    assert alerts.check_org(org, _at(date(2026, 4, 20))) == 0


def test_scheduled_task_checks_every_organization(org, notice_row, settings):
    from apps.customs.tasks import check_free_time

    assert "apps.customs.tasks.check_free_time" in [v["task"] for v in settings.CELERY_BEAT_SCHEDULE.values()]
    assert check_free_time.delay().get() >= 0  # runs eagerly; today's date decides whether anything is due


def test_alert_types_are_registered():
    assert events.EVENT_LABELS[alerts.EVENT_SOON] == "Last free day coming up"
    assert events.AUDIT_EVENTS["free_time.accruing"] == alerts.EVENT_LATE


# --------------------------------------------------------------------------- pickup and return dates


def _dates_url(row):
    return reverse("customs:set_dates", args=[row.pk])


def test_viewer_cannot_enter_dates(client, org, viewer, notice_row):
    client.force_login(viewer)
    assert client.post(_dates_url(notice_row), {"picked_up_on": "2026-04-09"}).status_code == 403
    notice_row.refresh_from_db()
    assert notice_row.picked_up_on is None


def test_reviewer_enters_dates_and_they_are_audited(client, org, user, notice_row, monkeypatch):
    monkeypatch.setattr("apps.customs.views.org_today", lambda o: date(2026, 4, 20))
    client.force_login(user)
    r = client.post(_dates_url(notice_row), {"picked_up_on": "2026-04-09", "returned_on": ""}, follow=True)
    assert "Return the empty by 16 Apr 2026" in r.content.decode()
    notice_row.refresh_from_db()
    assert notice_row.picked_up_on == date(2026, 4, 9) and notice_row.picked_up_by == user
    e = AuditEvent.objects.get(action="free_time.picked_up")
    assert e.actor == user and e.data["new"] == "2026-04-09" and e.data["old"] is None
    client.post(_dates_url(notice_row), {"picked_up_on": "2026-04-09", "returned_on": "2026-04-14"})
    notice_row.refresh_from_db()
    assert notice_row.returned_on == date(2026, 4, 14) and notice_row.stage == "closed"
    assert AuditEvent.objects.filter(action="free_time.returned", actor=user).count() == 1


@pytest.mark.parametrize("post,words", [
    ({"picked_up_on": "2026-04-25"}, "after today"),
    ({"returned_on": "2026-04-12"}, "Enter the pickup date"),
    ({"picked_up_on": "2026-04-12", "returned_on": "2026-04-10"}, "before its pickup date"),
    ({"picked_up_on": "2026-04-05"}, "before it was discharged"),
    ({"picked_up_on": "12/04/2026"}, "Dates must be written like"),
])
def test_impossible_dates_are_refused(client, org, user, notice_row, monkeypatch, post, words):
    monkeypatch.setattr("apps.customs.views.org_today", lambda o: date(2026, 4, 20))
    client.force_login(user)
    r = client.post(_dates_url(notice_row), post, follow=True)
    assert words in r.content.decode()
    notice_row.refresh_from_db()
    assert notice_row.picked_up_on is None and notice_row.returned_on is None
    assert not AuditEvent.objects.filter(action__startswith="free_time.p").exists()


def test_other_organizations_rows_are_not_reachable(client, notice_row, db):
    from django.contrib.auth import get_user_model

    from apps.core.models import Membership, Organization

    other = Organization.objects.create(name="Other", slug="other")
    stranger = get_user_model().objects.create_user("stranger", password="pw-123456789-test")
    Membership.objects.create(user=stranger, organization=other, role="admin")
    client.force_login(stranger)
    assert client.post(_dates_url(notice_row), {"picked_up_on": "2026-04-09"}).status_code == 404


# --------------------------------------------------------------------------- settings


def test_customs_settings_are_for_admins(client, org, user, approver, admin_user, notice_row):
    url = reverse("customs:settings")
    for who in (user, approver):
        client.force_login(who)
        assert client.get(url).status_code == 403
    client.force_login(admin_user)
    assert "Merchandise processing fee" in client.get(url).content.decode()
    r = client.post(url, {"lfd_alert_days": "3", "holidays": "2026-04-13\n2026-04-14"}, follow=True)
    assert "were recalculated" in r.content.decode()
    cfg = CustomsSettings.for_org(org)
    assert cfg.lfd_alert_days == 3 and cfg.holidays == ["2026-04-13", "2026-04-14"]
    notice_row.refresh_from_db()
    assert notice_row.lfd_demurrage == date(2026, 4, 15)  # two holidays skipped
    assert AuditEvent.objects.get(action="customs_settings.updated").actor == admin_user
    r = client.post(url, {"lfd_alert_days": "45", "holidays": "next friday"})
    page = r.content.decode()
    assert "from 0 to 30" in page and "aren&#x27;t dates: next, friday" in page


# --------------------------------------------------------------------------- screens, charges, data


def test_shipment_page_dashboard_and_list(client, org, user, viewer, entry_shipment, monkeypatch):
    ingest(org, "an.pdf", samples.arrival_notice(BL, [BOX], discharge=date(2026, 4, 7))[0])
    monkeypatch.setattr("apps.customs.services.freetime.org_today", lambda o, now=None: date(2026, 4, 12))
    monkeypatch.setattr("apps.customs.templatetags.customs_tags.org_today", lambda o, now=None: date(2026, 4, 12))
    client.force_login(user)
    page = client.get(reverse("review:shipment", args=[entry_shipment.pk])).content.decode()
    for words in ("Customs duty", "Entry <span class=\"mono\">HLB-2604117-3</span>", "Every line matches rate times value",
                  "Should be", 'data-evidence-targets="entry_lines=0"', 'id="free-time"', "1 day left",
                  'name="picked_up_on"', "Free days counted in working days"):
        assert words in page, words
    dash = client.get(reverse("core:dashboard")).content.decode()
    assert "Containers near last free day" in dash and BOX in dash
    client.force_login(viewer)
    page = client.get(reverse("review:shipment", args=[entry_shipment.pk])).content.decode()
    assert 'name="picked_up_on"' not in page  # viewers see dates, can't change them
    listing = client.get(reverse("customs:free_time") + "?tab=open").content.decode()
    assert BOX in listing and entry_shipment.reference in listing


def test_table_fields_are_not_edited_as_text(client, org, user, entry_shipment):
    assert "entry_lines" not in EDITABLE and "container_dates" not in EDITABLE and "entry_number" in EDITABLE
    doc = entry_shipment.documents.get(doc_type="customs_entry")
    client.force_login(user)
    r = client.post(reverse("review:update_field", args=[doc.pk]), {"name": "entry_lines", "value": "x"}, follow=True)
    assert "can&#x27;t be edited" in r.content.decode()


def test_duty_charges_for_landed_cost_count_each_entry_once(org, entry_shipment):
    ingest(org, "entry-psc.pdf", samples.cbp7501(ENTRY, bl=BL, containers=[BOX], errors=("mpf",))[0])
    got = {c["code"]: c for c in duty_charges(entry_shipment)}
    assert set(got) == {"customs_duty", "merchandise_processing_fee", "harbor_maintenance_fee"}
    assert got["customs_duty"]["amount"] == Decimal("2287.80") and got["customs_duty"]["currency"] == "USD"
    assert got["merchandise_processing_fee"]["amount"] == Decimal("120.80")  # the latest copy of the entry
    assert all(c["basis_hint"] == "value" for c in got.values())
    assert duty_charges(Shipment.objects.create(organization=org)) == []


def test_customs_add_on_is_opt_in(dataset, tmp_path):
    copy = tmp_path / "ds"
    shutil.copytree(dataset, copy)
    before = json.loads((copy / "ground_truth.json").read_text())
    result = samples.add_to_dataset(copy, seed=11, today=date(2026, 4, 12))
    after = json.loads((copy / "ground_truth.json").read_text())
    assert result["customs_entries"] >= 2 and result["arrival_notices"] >= 2
    added = [d for d in after["documents"] if d["doc_type"] in ("customs_entry", "arrival_notice")]
    assert len(after["documents"]) == len(before["documents"]) + len(added)
    assert {d["file"] for d in before["documents"]} <= {d["file"] for d in after["documents"]}
    for d in added:
        assert classify(text_of((copy / "pdf" / d["file"]).read_bytes()))[0] == d["doc_type"], d["file"]
    emails = json.loads((copy / "emails.json").read_text())
    assert {d["file"] for d in added} <= {e["file"] for e in emails}


def test_carrier_names_in_samples_are_the_generators():
    assert samples.arrival_notice()[1]["carrier_name"] == CARRIERS[0][0].name
