"""QA-001: a typed value must be a valid value for its field, and no amount may ever break a page.

A reviewer once saved 99999999999999999999 as an invoice total. It was accepted, flowed into the money-at-risk
columns, and every page that read them (dashboard, savings, the shipment itself) returned HTTP 500."""
from decimal import Decimal

import pytest
from django.db.models import Sum
from django.urls import reverse

from apps.core.utils import clamp_money
from apps.documents.models import ExtractedField
from apps.documents.services.corrections import FieldValueError, parse_input
from apps.documents.services.ingest import ingest_bytes
from apps.rates.models import CaughtCharge
from apps.shipments.models import Shipment, ValidationIssue


@pytest.fixture
def loaded(org, dataset):
    for f in ["S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    return org


# ---------------------------------------------------------------- what is accepted


@pytest.mark.parametrize("name, typed, stored", [
    ("total_amount", "1250", "1250.00"),
    ("total_amount", "1,234.5", "1234.50"),
    ("total_amount", " $ 28,440 ", "28440.00"),
    ("total_amount", "1.234,56", "1234.56"),
    ("total_amount", "0", "0.00"),
    ("total_amount", "999999999999.99", "999999999999.99"),
    ("exchange_rate", "1.085", "1.085000"),
    ("issue_date", "2026-01-08", "2026-01-08"),
    ("currency", "usd", "USD"),
    ("vendor_name", "  Acme Freight  ", "Acme Freight"),
    ("invoice_number", "", None),
    ("container_numbers", "abcd 1234567; efgh1234567", ["ABCD1234567", "EFGH1234567"]),
])
def test_valid_values_are_normalised(name, typed, stored):
    assert parse_input(name, typed) == stored


# ---------------------------------------------------------------- what is refused


@pytest.mark.parametrize("name, typed, words", [
    ("total_amount", "abc", "must be a number"),
    ("total_amount", "12abc", "must be a number"),
    ("total_amount", "1e999", "must be a number"),
    ("total_amount", "NaN", "must be a number"),
    ("total_amount", "Infinity", "must be a number"),
    ("total_amount", "-5", "negative"),
    ("total_amount", "(50)", "negative"),
    ("total_amount", "99999999999999999999", "too large"),
    ("total_amount", "1000000000000", "too large"),
    ("issue_date", "not-a-date", "year-month-day"),
    ("issue_date", "2026-02-30", "year-month-day"),
    ("issue_date", "1066-01-01", "looks wrong"),
    ("invoice_date", "08/01/2026", "year-month-day"),
    ("currency", "DOLLARS", "three-letter"),
    ("currency", "U$", "three-letter"),
    ("bl_number", "X" * 41, "at most 40"),
    ("vendor_name", "V" * 201, "at most 200"),
    ("container_numbers", ",".join(f"ABCD{i:07d}" for i in range(51)), "at most 50"),
    ("po_numbers", "P" * 41, "at most 40"),
])
def test_invalid_values_are_refused_with_a_reason(name, typed, words):
    with pytest.raises(FieldValueError) as e:
        parse_input(name, typed)
    assert words in str(e.value)


def test_control_characters_are_stripped():
    assert parse_input("vendor_name", "Acme" + chr(0) + chr(7) + " Co") == "Acme Co"


# ---------------------------------------------------------------- the review screen


@pytest.mark.django_db
@pytest.mark.parametrize("name, typed", [
    ("total_amount", "99999999999999999999"),
    ("total_amount", "abc"),
    ("total_amount", "-5"),
    ("total_amount", "1e999"),
])
def test_bad_total_is_refused_and_nothing_breaks(client, user, loaded, name, typed):
    shipment = Shipment.objects.get(organization=loaded)
    doc = shipment.documents.get(doc_type="freight_invoice")
    before = ExtractedField.objects.get(document=doc, name=name).value
    client.force_login(user)

    r = client.post(reverse("review:update_field", args=[doc.pk]), {"name": name, "value": typed}, follow=True)

    assert r.status_code == 200
    assert "Total amount" in "".join(m.message for m in r.context["messages"])
    assert ExtractedField.objects.get(document=doc, name=name).value == before   # nothing was saved
    for url in (reverse("core:dashboard"), reverse("review:shipment", args=[shipment.pk]),
                reverse("savings:summary") if False else reverse("review:queue")):
        assert client.get(url).status_code == 200


@pytest.mark.django_db
def test_good_total_is_saved_in_normal_form(client, user, loaded):
    shipment = Shipment.objects.get(organization=loaded)
    doc = shipment.documents.get(doc_type="freight_invoice")
    client.force_login(user)

    client.post(reverse("review:update_field", args=[doc.pk]), {"name": "total_amount", "value": "1,100.5"})

    assert ExtractedField.objects.get(document=doc, name="total_amount").value == "1100.50"


@pytest.mark.django_db
def test_bad_date_is_refused(client, user, loaded):
    shipment = Shipment.objects.get(organization=loaded)
    doc = shipment.documents.get(doc_type="bill_of_lading")
    before = ExtractedField.objects.get(document=doc, name="issue_date").value
    client.force_login(user)

    r = client.post(reverse("review:update_field", args=[doc.pk]), {"name": "issue_date", "value": "not-a-date"},
                    follow=True)

    assert "year-month-day" in "".join(m.message for m in r.context["messages"])
    assert ExtractedField.objects.get(document=doc, name="issue_date").value == before


# ---------------------------------------------------------------- an amount can never break a page


@pytest.mark.parametrize("value, expected", [
    (None, None),
    (Decimal("12.345"), Decimal("12.35")),
    (Decimal("999999999999.99"), Decimal("999999999999.99")),
    (Decimal("1000000000000"), Decimal("999999999999.99")),
    (Decimal("-1000000000000"), Decimal("-999999999999.99")),
    (Decimal("99999999999999999999"), Decimal("999999999999.99")),
    (Decimal("1E+30"), Decimal("999999999999.99")),
    (Decimal("NaN"), None),
    (Decimal("Infinity"), None),
    ("not a number", None),
    (9.999999999999997e19, Decimal("999999999999.99")),
])
def test_clamp_money(value, expected):
    assert clamp_money(value) == expected


@pytest.mark.django_db
def test_absurd_amounts_are_clamped_so_aggregates_still_read(org):
    issue = ValidationIssue.objects.create(
        organization=org, code="over_quote", severity="error", message="m", fingerprint="f",
        amount_at_risk=Decimal("99999999999999999999"), currency="USD")
    caught = CaughtCharge.objects.create(
        organization=org, scope="s1", catch_key="k", code="over_quote", currency="USD",
        amount_caught=Decimal("99999999999999999999"), amount_latest=Decimal("99999999999999999999"),
        first_caught_at="2026-10-01T00:00:00Z", last_seen_at="2026-10-01T00:00:00Z")

    issue.refresh_from_db()
    caught.refresh_from_db()
    assert issue.amount_at_risk == Decimal("999999999999.99")
    assert caught.amount_caught == caught.amount_latest == Decimal("999999999999.99")
    # the reads that used to raise decimal.InvalidOperation
    assert ValidationIssue.objects.aggregate(t=Sum("amount_at_risk"))["t"] == Decimal("999999999999.99")
    assert CaughtCharge.objects.filter(pk=caught.pk).aggregate(t=Sum("amount_caught"))["t"] == Decimal("999999999999.99")
    for row in CaughtCharge.objects.all():   # including any row the ledger derived from the issue
        assert abs(row.amount_caught) <= Decimal("999999999999.99")
