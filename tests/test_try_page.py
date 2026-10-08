"""Public try page: limits, honeypot, token-only access, isolation and purge."""
from datetime import timedelta
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Organization
from apps.demo.models import TrySubmission
from apps.documents.models import Document


@pytest.fixture(autouse=True)
def _try_on(settings):
    settings.TRY_ENABLED = True
    settings.TRY_RATE_PER_HOUR = 5
    settings.TRY_RATE_PER_DAY = 20
    settings.TRY_DAILY_LIMIT = 200
    settings.TRY_MAX_MB = 10
    settings.TRY_MAX_PAGES = 10
    settings.TRY_RETENTION_HOURS = 24
    settings.DEMO_CONTACT_URL = "https://example.com/talk"


@pytest.fixture
def invoice(dataset):
    return (dataset / "pdf" / "S03_3_freight_invoice.pdf").read_bytes()


def _upload(client, content, name="invoice.pdf", **extra):
    return client.post(reverse("demo:try"), {"file": SimpleUploadedFile(name, content, "application/pdf"), **extra})


def _token(response):
    return response["Location"].rstrip("/").split("/")[-1]


def _pdf_with_pages(n):
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf)
    for i in range(n):
        c.drawString(72, 720, f"Page {i + 1} of a long document")
        c.showPage()
    c.save()
    return buf.getvalue()


@pytest.mark.django_db
def test_page_is_off_unless_enabled(client, settings):
    settings.TRY_ENABLED = False
    assert client.get(reverse("demo:try")).status_code == 404


@pytest.mark.django_db
def test_upload_shows_fields_type_and_issues_without_login(client, invoice):
    page = client.get(reverse("demo:try"))
    assert page.status_code == 200 and "Your document stays private" in page.content.decode()
    r = _upload(client, invoice)
    assert r.status_code == 302
    result = client.get(r["Location"])
    html = result.content.decode()
    assert result.status_code == 200
    assert "Freight invoice" in html and "Invoice number" in html
    assert "Total doesn&#x27;t match line items" in html or "Total doesn't match line items" in html  # S03 is planted
    assert "https://example.com/talk" in html
    assert result["X-Robots-Tag"] == "noindex, nofollow" and result["Referrer-Policy"] == "same-origin"
    assert "no-store" in result["Cache-Control"]
    sub = TrySubmission.objects.get()
    assert sub.organization.slug.startswith("try-") and not sub.organization.memberships.exists()
    assert sub.document.organization == sub.organization


@pytest.mark.django_db
def test_each_upload_gets_its_own_sandbox(client, invoice, org):
    _upload(client, invoice)
    _upload(client, invoice)  # the same file twice must not be flagged as a duplicate of another visitor's
    subs = list(TrySubmission.objects.all())
    assert len(subs) == 2 and subs[0].organization_id != subs[1].organization_id
    assert not Document.objects.filter(organization=org).exists()   # never a customer organization
    for s in subs:
        assert s.document.organization_id == s.organization_id


@pytest.mark.django_db
def test_results_need_the_exact_token(client, invoice):
    token = _token(_upload(client, invoice))
    assert client.get(reverse("demo:try_result", args=[token])).status_code == 200
    assert client.get(reverse("demo:try_result", args=[token[:-1] + ("A" if token[-1] != "A" else "B")])).status_code == 404
    assert client.get(reverse("demo:try_result", args=["x" * 43])).status_code == 404
    assert len(token) >= 40
    # Only a hash of the token is stored.
    assert not TrySubmission.objects.filter(token_hash=token).exists()


@pytest.mark.django_db
def test_honeypot_upload_is_ignored(client, invoice):
    r = _upload(client, invoice, website="https://spam.example")
    assert r.status_code == 302 and r["Location"] == reverse("demo:try")
    assert not TrySubmission.objects.exists() and not Document.objects.exists()


@pytest.mark.django_db
def test_csrf_is_required(invoice):
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    r = strict.post(reverse("demo:try"), {"file": SimpleUploadedFile("a.pdf", invoice, "application/pdf")})
    assert r.status_code == 403
    assert not TrySubmission.objects.exists()


@pytest.mark.django_db
def test_rejects_non_pdf_large_and_long_files(client, settings, invoice):
    r = _upload(client, b"hello, not a pdf", name="notes.txt")
    assert "Only PDF files" in client.get(r["Location"]).content.decode()
    settings.TRY_MAX_MB = 0.001
    r = _upload(client, invoice)
    assert "The limit here is" in client.get(r["Location"]).content.decode()
    settings.TRY_MAX_MB = 10
    settings.TRY_MAX_PAGES = 2
    r = _upload(client, _pdf_with_pages(3))
    assert "This PDF has 3 pages" in client.get(r["Location"]).content.decode()
    r = _upload(client, b"%PDF-1.4 broken")
    assert "couldn&#x27;t open this PDF" in client.get(r["Location"]).content.decode()
    r = client.post(reverse("demo:try"), {})
    assert "Choose a PDF" in client.get(r["Location"]).content.decode()
    assert not TrySubmission.objects.exists()


@pytest.mark.django_db
def test_per_ip_hourly_limit(client, settings, invoice):
    settings.TRY_RATE_PER_HOUR = 2
    for _ in range(2):
        assert _upload(client, invoice).status_code == 302
    r = _upload(client, invoice)
    assert r.status_code == 429 and "the limit for this page" in r.content.decode()
    assert TrySubmission.objects.count() == 2
    # Another visitor is not affected.
    other = client.post(reverse("demo:try"), {"file": SimpleUploadedFile("a.pdf", invoice, "application/pdf")},
                        REMOTE_ADDR="10.9.9.9")
    assert other.status_code == 302


@pytest.mark.django_db
def test_per_ip_daily_limit_counts_rejected_attempts(client, settings):
    settings.TRY_RATE_PER_DAY = 3
    for _ in range(3):
        _upload(client, b"junk")
    assert _upload(client, b"junk").status_code == 429


@pytest.mark.django_db
def test_global_daily_cap(client, settings, invoice):
    settings.TRY_DAILY_LIMIT = 1
    assert _upload(client, invoice).status_code == 302
    r = client.post(reverse("demo:try"), {"file": SimpleUploadedFile("a.pdf", invoice, "application/pdf")},
                    REMOTE_ADDR="10.1.1.1")
    assert r.status_code == 503 and "free tries are used up" in r.content.decode()
    assert TrySubmission.objects.count() == 1


@pytest.mark.django_db
def test_delete_now_removes_everything(client, invoice, settings):
    token = _token(_upload(client, invoice))
    sub = TrySubmission.objects.get()
    path = sub.document.file.path
    slug = sub.organization.slug
    r = client.post(reverse("demo:try_delete", args=[token]), follow=True)
    assert "were deleted" in r.content.decode()
    assert not TrySubmission.objects.exists() and not Organization.objects.filter(slug=slug).exists()
    assert not Document.objects.exists()
    import os

    assert not os.path.exists(path)
    assert client.get(reverse("demo:try_result", args=[token])).status_code == 404


@pytest.mark.django_db
def test_purge_deletes_expired_uploads_only(client, invoice, dataset, org):
    from apps.documents.services.ingest import ingest_bytes

    customer_doc, _ = ingest_bytes(org, "keep.pdf", (dataset / "pdf" / "S01_1_commercial_invoice.pdf").read_bytes(),
                                   process="sync")
    old_token = _token(_upload(client, invoice))
    _upload(client, (dataset / "pdf" / "S01_3_freight_invoice.pdf").read_bytes())
    old = TrySubmission.objects.order_by("created_at").first()
    TrySubmission.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    old_file = old.document.file.path
    call_command("purge_try")
    assert TrySubmission.objects.count() == 1
    assert not Organization.objects.filter(pk=old.organization_id).exists()
    import os

    assert not os.path.exists(old_file)
    assert Document.objects.filter(pk=customer_doc.pk).exists()          # customer data untouched
    assert client.get(reverse("demo:try_result", args=[old_token])).status_code == 404


@pytest.mark.django_db
def test_expired_result_is_not_shown_before_purge(client, invoice):
    token = _token(_upload(client, invoice))
    TrySubmission.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert client.get(reverse("demo:try_result", args=[token])).status_code == 404


def test_purge_task_is_scheduled():
    from django.conf import settings

    entry = settings.CELERY_BEAT_SCHEDULE["purge-try-uploads-hourly"]
    assert entry["task"] == "apps.demo.tasks.purge_try_uploads"
