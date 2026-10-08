"""Vendor learning: corrections teach ShipMatch how a vendor prints values; the next document is read right."""
from datetime import date

import pytest
from django.urls import reverse

from apps.core.models import AuditEvent
from apps.documents.services.ingest import ingest_bytes
from apps.learning.context import current_hint
from apps.learning.models import DocumentLearning, VendorProfile
from apps.learning.samples import VENDOR, broker_invoice_pdf, broker_pair
from apps.learning.services import labels
from apps.learning.services.apply import build_hint


def _ingest(org, name, pdf):
    doc, _ = ingest_bytes(org, name, pdf, process="sync")
    return doc


def _correct(client, doc, name, value):
    return client.post(reverse("review:update_field", args=[doc.pk]), {"name": name, "value": value}, follow=True)


@pytest.fixture
def taught(client, org, user):
    """The first broker invoice, read by the rules, corrected by a reviewer."""
    (name1, ref1, day1, pdf1), second = broker_pair()
    doc1 = _ingest(org, name1, pdf1)
    assert doc1.field("vendor_name") == VENDOR
    assert doc1.field("invoice_number") is None                 # "Ref No." is not a label the rules know
    assert doc1.field("invoice_date") == "2026-03-08"            # 03/08/2026 read month first
    client.force_login(user)
    _correct(client, doc1, "invoice_number", ref1)
    _correct(client, doc1, "invoice_date", day1.isoformat())
    return doc1, second


@pytest.mark.django_db
def test_correction_teaches_label_and_date_order(org, taught):
    profile = VendorProfile.objects.get(organization=org)
    assert profile.doc_type == "freight_invoice"
    assert profile.labels["invoice_number"]["label"] == "Ref No."
    assert profile.labels["invoice_date"]["label"] == "Invoice Date"
    assert profile.date_format == "dmy"
    assert profile.correction_count == 2
    assert [e["field"] for e in profile.examples] == ["invoice_number", "invoice_date"]
    assert AuditEvent.objects.filter(organization=org, action="learning.updated").exists()


@pytest.mark.django_db
def test_next_document_from_vendor_is_read_correctly(client, org, taught):
    _, (name2, ref2, day2, pdf2) = taught
    doc2 = _ingest(org, name2, pdf2)
    assert doc2.field("invoice_number") == ref2
    assert doc2.field("invoice_date") == day2.isoformat()
    f = doc2.fields.get(name="invoice_number")
    assert f.source == "rules" and f.grounded and f.confidence >= 0.9
    record = DocumentLearning.objects.get(document=doc2)
    assert record.fields["invoice_number"]["how"] == "label"
    assert record.fields["invoice_date"]["replaced"] == "2026-04-09"   # what the rules alone would have read
    # The review screen says so.
    page = client.get(reverse("review:document", args=[doc2.pk]), follow=True).content.decode()
    assert "Learned from 2 corrections for this vendor." in page
    assert "read after the label “Ref No.”" in page


@pytest.mark.django_db
def test_other_vendors_and_orgs_are_unaffected(org, taught, django_user_model):
    from apps.core.models import Organization

    other = Organization.objects.create(name="Other Co", slug="other")
    _, (name2, ref2, _, pdf2) = taught
    doc = _ingest(other, name2, pdf2)
    assert doc.field("invoice_number") is None
    assert not DocumentLearning.objects.filter(document=doc).exists()


@pytest.mark.django_db
def test_correction_without_known_vendor_learns_nothing(client, org, user):
    (name1, ref1, _, pdf1), _ = broker_pair()
    doc = _ingest(org, name1, pdf1)
    doc.fields.filter(name="vendor_name").delete()
    client.force_login(user)
    _correct(client, doc, "invoice_number", ref1)
    assert doc.field("invoice_number") == ref1          # the correction itself is saved
    assert not VendorProfile.objects.exists()


@pytest.mark.django_db
def test_human_corrections_survive_reprocessing_with_learning(client, org, taught):
    from apps.documents.services.pipeline import process_document

    doc1, _ = taught
    process_document(doc1.pk)
    doc1.refresh_from_db()
    assert doc1.fields.get(name="invoice_number").source == "human"
    assert "invoice_number" not in DocumentLearning.objects.get(document=doc1).fields


def test_label_before_and_value_after():
    text = "Coastline Ltd\nDate: 03/08/2026 Ref No.: CCB/24117\nDue Date: 05/09/2026\nShipper:\nAcme Exports Ltd\n"
    assert labels.label_before(text, "invoice_number", "CCB/24117") == "Ref No."
    assert labels.label_before(text, "shipper", "Acme Exports Ltd") == "Shipper"
    assert labels.label_before(text, "invoice_number", "not printed") == ""
    assert labels.value_after(text, "Ref No.", "invoice_number") == "CCB/24117"
    assert labels.value_after(text, "Date", "invoice_date", "dmy") == "2026-08-03"   # not the "Due Date" line
    assert labels.value_after(text, "Shipper", "shipper") == "Acme Exports Ltd"     # value on the next line


# --------------------------------------------------------------------------- AI reader hint


def _profile(org, **kw):
    defaults = {"organization": org, "vendor_key": "coastline customs brokers", "doc_type": "freight_invoice",
                "display_name": VENDOR}
    return VendorProfile.objects.create(**{**defaults, **kw})


@pytest.mark.django_db
def test_hint_lists_labels_dates_and_recent_pairs(org):
    p = _profile(org, labels={"invoice_number": {"label": "Ref No."}}, date_format="dmy",
                 examples=[{"field": "invoice_number", "old": "", "new": f"CCB/{n}"} for n in range(5)])
    hint = build_hint(p)
    assert hint.startswith("\n\n<vendor_notes>") and hint.endswith("</vendor_notes>")
    assert 'printed after the label "Ref No."' in hint
    assert "day first" in hint
    assert hint.count("the correct value was") == 3          # only the 3 most recent pairs
    assert '"CCB/4"' in hint and '"CCB/1"' not in hint


@pytest.mark.django_db
def test_hint_has_hard_size_cap_and_escapes_values(org, settings):
    huge = [{"field": "invoice_number", "old": "<x>" * 200, "new": "y\n" * 400} for _ in range(3)]
    labels_ = {f"field_{i}": {"label": "L" * 300} for i in range(40)}
    p = _profile(org, labels=labels_, examples=huge, date_format="dmy")
    settings.LEARNING_HINT_MAX_CHARS = 600
    hint = build_hint(p)
    assert 0 < len(hint) <= 600
    assert "<x>" not in hint and hint.count("<vendor_notes>") == 1 and hint.count("</vendor_notes>") == 1
    assert build_hint(p, max_chars=50) == ""                 # too small for even one note: send nothing


@pytest.mark.django_db
def test_ai_prompt_carries_vendor_notes(org, settings, monkeypatch):
    from apps.documents.services import llm

    settings.EXTRACTION_PROVIDER = "anthropic"
    settings.ANTHROPIC_API_KEY = "test-key"
    _profile(org, labels={"invoice_number": {"label": "Ref No."}}, date_format="dmy",
             field_corrections={"invoice_number": 1}, correction_count=1,
             examples=[{"field": "invoice_number", "old": "", "new": "CCB/24117"}])
    seen = {}

    def fake_call(system, user, schema, name="record", pdf=None, purpose="extract", **kw):
        if purpose == "extract":
            seen["user"] = user
            assert current_hint() and current_hint() in user
            return {"vendor_name": VENDOR, "invoice_number": "CCB/24206", "invoice_date": "2026-09-04",
                    "total_amount": 300.0}
        return {"doc_type": "freight_invoice"}

    monkeypatch.setattr(llm, "structured_call", fake_call)
    doc = _ingest(org, "b.pdf", broker_invoice_pdf("CCB/24206", date(2026, 9, 4)))
    assert "<vendor_notes>" in seen["user"] and 'label "Ref No."' in seen["user"]
    assert len(seen["user"].split("<vendor_notes>")[1]) <= settings.LEARNING_HINT_MAX_CHARS
    assert doc.field("invoice_number") == "CCB/24206"
    assert DocumentLearning.objects.get(document=doc).fields["invoice_number"]["how"] == "hint"
    assert current_hint() == ""                               # the notes don't leak into the next call


# --------------------------------------------------------------------------- settings page and forget


@pytest.mark.django_db
def test_settings_page_and_forget(client, org, taught, admin_user):
    client.force_login(admin_user)
    page = client.get(reverse("learning:settings")).content.decode()
    assert VENDOR in page and "Invoice number is printed after “Ref No.”" in page
    profile = VendorProfile.objects.get(organization=org)
    r = client.post(reverse("learning:forget", args=[profile.pk]), follow=True)
    assert "forgot what it learned" in r.content.decode()
    assert not VendorProfile.objects.filter(pk=profile.pk).exists()
    event = AuditEvent.objects.get(organization=org, action="learning.forgotten")
    assert event.actor == admin_user and event.data["corrections"] == 2
    # Forgotten means forgotten: the next invoice is read by the plain rules again.
    _, (name2, _, _, pdf2) = taught
    assert _ingest(org, name2, pdf2).field("invoice_number") is None


@pytest.mark.django_db
def test_learning_settings_need_manage_permission(client, org, taught, user, viewer):
    profile = VendorProfile.objects.get(organization=org)
    for who in (user, viewer):
        client.force_login(who)
        assert client.get(reverse("learning:settings")).status_code == 403
        assert client.post(reverse("learning:forget", args=[profile.pk])).status_code == 403
    assert VendorProfile.objects.filter(pk=profile.pk).exists()


@pytest.mark.django_db
def test_cannot_forget_another_orgs_vendor(client, org, taught, django_user_model):
    from apps.core.models import Membership, Organization

    other = Organization.objects.create(name="Other Co", slug="other")
    boss = django_user_model.objects.create_user("boss", password="pw-123456789-test")
    Membership.objects.create(user=boss, organization=other, role="admin")
    client.force_login(boss)
    profile = VendorProfile.objects.get(organization=org)
    assert client.post(reverse("learning:forget", args=[profile.pk])).status_code == 404
    assert VendorProfile.objects.filter(pk=profile.pk).exists()


@pytest.mark.django_db
def test_learning_can_be_switched_off(client, org, user, settings):
    settings.VENDOR_LEARNING = False
    (name1, ref1, _, pdf1), _ = broker_pair()
    doc = _ingest(org, name1, pdf1)
    client.force_login(user)
    _correct(client, doc, "invoice_number", ref1)
    assert not VendorProfile.objects.exists()
