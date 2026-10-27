"""Fixes for QA sessions 18-21: QA-091 (documents lost when the broker is down), QA-101 (production started
with a public secret key) and QA-102 (local credentials copied into the Docker image)."""
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone
from kombu.exceptions import OperationalError as BrokerError

from apps.documents.models import Document
from apps.documents.services.ingest import ingest_bytes
from apps.documents.tasks import MAX_REQUEUES, requeue_stalled_documents

from .pdfs import make_pdf

ROOT = Path(__file__).resolve().parent.parent
BROKER_DOWN = mock.patch("apps.documents.tasks.process_document_task.delay",
                         side_effect=BrokerError("Error 10061 connecting to localhost:6379"))


def _age(doc, minutes):
    Document.objects.filter(pk=doc.pk).update(updated_at=timezone.now() - timedelta(minutes=minutes))


# ---------------------------------------------------------------- QA-091


@pytest.mark.django_db
def test_upload_with_the_broker_down_is_kept_not_a_500(client, user, org, django_capture_on_commit_callbacks):
    client.force_login(user)
    pdf = make_pdf("COMMERCIAL INVOICE\nInvoice No: INV-91\nB/L: MAEU91")
    with BROKER_DOWN, django_capture_on_commit_callbacks(execute=True):
        r = client.post(reverse("review:upload"), {"files": [_file("inv.pdf", pdf)]})
    assert r.status_code == 302
    assert Document.objects.get(organization=org).status == Document.Status.RECEIVED


@pytest.mark.django_db
def test_uploading_an_unread_document_again_queues_it(org, django_capture_on_commit_callbacks):
    pdf = make_pdf("COMMERCIAL INVOICE\nInvoice No: INV-92")
    doc, _ = ingest_bytes(org, "inv.pdf", pdf, process="none")
    with mock.patch("apps.documents.tasks.process_document_task.delay") as delay, \
            django_capture_on_commit_callbacks(execute=True):
        ingest_bytes(org, "inv.pdf", pdf)   # just stored: probably still in the queue, leave it
        assert delay.call_count == 0
        _age(doc, 5)
        again, created = ingest_bytes(org, "inv.pdf", pdf)
    assert not created and again.pk == doc.pk
    delay.assert_called_once_with(doc.pk)


@pytest.mark.django_db
def test_sweep_reads_documents_whose_task_was_lost(org):
    stalled, _ = ingest_bytes(org, "a.pdf", make_pdf("COMMERCIAL INVOICE\nInvoice No: INV-93"), process="none")
    fresh, _ = ingest_bytes(org, "b.pdf", make_pdf("COMMERCIAL INVOICE\nInvoice No: INV-94"), process="none")
    _age(stalled, 20)
    assert requeue_stalled_documents() == 1   # CELERY_TASK_ALWAYS_EAGER: read inline
    stalled.refresh_from_db()
    fresh.refresh_from_db()
    assert stalled.status not in (Document.Status.RECEIVED, Document.Status.EXTRACTED)
    assert stalled.intake["requeued"] == 1
    assert fresh.status == Document.Status.RECEIVED


@pytest.mark.django_db
def test_sweep_waits_while_the_broker_is_down_without_using_up_attempts(org):
    doc, _ = ingest_bytes(org, "a.pdf", make_pdf("COMMERCIAL INVOICE\nInvoice No: INV-95"), process="none")
    _age(doc, 20)
    with BROKER_DOWN:
        assert requeue_stalled_documents() == 0
    doc.refresh_from_db()
    assert doc.status == Document.Status.RECEIVED and "requeued" not in doc.intake


@pytest.mark.django_db
def test_sweep_gives_up_on_a_document_that_never_finishes(org):
    doc, _ = ingest_bytes(org, "a.pdf", make_pdf("COMMERCIAL INVOICE\nInvoice No: INV-96"), process="none")
    Document.objects.filter(pk=doc.pk).update(intake={"requeued": MAX_REQUEUES})
    _age(doc, 20)
    with mock.patch("apps.documents.tasks.process_document_task.delay") as delay:
        assert requeue_stalled_documents() == 0
    delay.assert_not_called()
    doc.refresh_from_db()
    assert doc.status == Document.Status.ERROR and "didn't finish" in doc.error


def _file(name, content):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, content, content_type="application/pdf")


# ---------------------------------------------------------------- QA-101


def _start(**env):
    """Load the settings the way gunicorn, the worker and beat do, with DEBUG off."""
    full = {**os.environ, "DJANGO_DEBUG": "0", "DJANGO_SETTINGS_MODULE": "config.settings", **env}
    return subprocess.run([sys.executable, "-c", "import django; django.setup()"], cwd=ROOT, env=full,
                          capture_output=True, text=True, timeout=120)


GOOD_KEY = "q1Zr6cXlq3mP0b8yXe2bW3l0cHk3Vt9sQq7m2r5n8xA="


@pytest.mark.parametrize("secret", ["", "dev-insecure-key-change-me", "change-me", "django-insecure-abc"])
def test_production_refuses_a_missing_or_public_secret_key(secret):
    r = _start(DJANGO_SECRET_KEY=secret, FIELD_ENCRYPTION_KEY=GOOD_KEY)
    assert r.returncode != 0 and "DJANGO_SECRET_KEY must be set" in r.stderr


@pytest.mark.parametrize("key", ["", "not-a-fernet-key"])
def test_production_refuses_a_missing_or_broken_encryption_key(key):
    r = _start(DJANGO_SECRET_KEY="x" * 60, FIELD_ENCRYPTION_KEY=key)
    assert r.returncode != 0 and "FIELD_ENCRYPTION_KEY must be set" in r.stderr


def test_production_starts_with_real_keys():
    r = _start(DJANGO_SECRET_KEY="x" * 60, FIELD_ENCRYPTION_KEY=GOOD_KEY)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- QA-102


def test_local_credentials_are_not_copied_into_the_image():
    ignored = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()}
    assert {".env", "local-test-accounts.txt", "tests/", "qa/"} <= ignored


# ---------------------------------------------------------------- QA-092


@pytest.mark.django_db
def test_a_stalled_document_does_not_claim_nothing_was_found(client, user, org):
    client.force_login(user)
    doc, _ = ingest_bytes(org, "a.pdf", make_pdf("COMMERCIAL INVOICE\nInvoice No: INV-921"), process="none")
    Document.objects.filter(pk=doc.pk).update(status=Document.Status.EXTRACTED)
    page = client.get(reverse("review:document", args=[doc.pk])).content.decode()
    assert "being read now" in page and "No bill of lading" not in page
    _age(doc, 20)
    page = client.get(reverse("review:document", args=[doc.pk])).content.decode()
    assert "taking longer than usual" in page


# ---------------------------------------------------------------- QA-094


class _DownCache:
    """Every call fails the way Django's Redis cache does when Redis is unreachable."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            from redis.exceptions import ConnectionError as RedisConnectionError

            raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")
        return fail


@pytest.mark.django_db
def test_sign_in_works_while_the_cache_is_down(client, user):
    from .conftest import PASSWORD

    with mock.patch("apps.accounts.services.lockout.cache", _DownCache()):
        r = client.post(reverse("accounts:login"), {"username": user.username, "password": PASSWORD})
        assert r.status_code == 302 and "_auth_user_id" in client.session
        client.logout()
        r = client.post(reverse("accounts:login"), {"username": user.username, "password": "wrong-password-1"})
        assert r.status_code == 200   # the form again, not a 500


@pytest.mark.django_db
def test_api_works_while_the_cache_is_down(client, user, org):
    client.force_login(user)
    with mock.patch("apps.api.cache", _DownCache()):
        assert client.get(f"/api/{org.slug}/shipments").status_code == 200


@pytest.mark.django_db
def test_two_factor_code_accepted_while_the_cache_is_down(user):
    import pyotp

    from apps.accounts.services import mfa

    profile = mfa.profile_for(user)
    profile.mfa_secret = pyotp.random_base32()
    profile.save()
    with mock.patch("apps.accounts.services.mfa.cache", _DownCache()):
        assert mfa.verify_totp(user, pyotp.TOTP(profile.mfa_secret).now())


# ---------------------------------------------------------------- QA-095


@pytest.fixture
def ready_to_post(org, dataset):
    from apps.accounting.models import QBOConnection
    from apps.shipments.models import Shipment

    for f in ["S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    s = Shipment.objects.get(organization=org)
    s.status = Shipment.Status.APPROVED
    s.save()
    QBOConnection.objects.create(organization=org, realm_id="123", access_token="tok", refresh_token="ref",
                                 access_expires_at=timezone.now() + timedelta(hours=1), default_expense_account_id="77")
    return s


@pytest.mark.django_db
def test_post_bills_with_the_broker_down_says_so(client, admin_user, org, ready_to_post):
    from apps.core.models import AuditEvent

    client.force_login(admin_user)
    with mock.patch("apps.accounting.tasks.post_shipment_task.delay", side_effect=BrokerError("down")):
        r = client.post(reverse("review:post", args=[ready_to_post.pk]), follow=True)
    assert r.status_code == 200 and "background service is unavailable" in r.content.decode()
    assert not AuditEvent.objects.filter(action="shipment.post_requested").exists()


@pytest.mark.django_db
def test_check_payments_with_the_broker_down_says_so(client, admin_user, org, ready_to_post, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = False
    client.force_login(admin_user)
    with mock.patch("apps.accounting.tasks.sync_org_payments.delay", side_effect=BrokerError("down")):
        r = client.post(reverse("accounting:check_payments"), follow=True)
    assert r.status_code == 200 and "were not checked" in r.content.decode().replace("weren&#x27;t", "were not")


# ---------------------------------------------------------------- QA-096


@pytest.mark.django_db
def test_tasks_for_deleted_rows_are_skipped():
    from apps.accounting.tasks import post_shipment_task
    from apps.documents.tasks import process_document_task

    assert process_document_task.apply(args=[999999]).get()["skipped"] == "no_document"
    assert post_shipment_task.apply(args=[999999]).get() == {"skipped": "no_shipment"}


# ---------------------------------------------------------------- QA-097


@pytest.mark.django_db
def test_password_reset_emails_are_limited_per_address(client, user, mailoutbox, settings):
    settings.PASSWORD_RESET_PER_EMAIL_PER_HOUR = 3
    for _ in range(6):
        r = client.post(reverse("accounts:password_reset"), {"email": user.email})
        assert r.status_code == 302 and r.url == reverse("accounts:password_reset_done")
    assert len(mailoutbox) == 3


@pytest.mark.django_db
def test_password_reset_requests_are_limited_per_ip(client, db, mailoutbox, settings):
    from django.contrib.auth import get_user_model

    settings.PASSWORD_RESET_PER_IP_PER_HOUR = 2
    for n in range(4):
        get_user_model().objects.create_user(f"u{n}", email=f"u{n}@example.com", password="pw-123456789-test")
        client.post(reverse("accounts:password_reset"), {"email": f"u{n}@example.com"})
    assert len(mailoutbox) == 2


# ---------------------------------------------------------------- QA-098


def _pages(n: int) -> bytes:
    import io

    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, invariant=1)
    for i in range(n):
        pdf.drawString(72, 720, f"Page {i + 1}")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


@pytest.mark.django_db
def test_pdfs_over_the_page_limit_are_refused(org, settings):
    from apps.documents.services.ingest import RejectedFile

    settings.INTAKE_MAX_PAGES = 5
    ingest_bytes(org, "ok.pdf", _pages(5), process="none")
    with pytest.raises(RejectedFile, match="has 6 pages; the limit is 5"):
        ingest_bytes(org, "long.pdf", _pages(6), process="none")


def test_paid_reading_gets_only_the_first_pages(settings):
    import io

    from pypdf import PdfReader

    from apps.documents.services.ocr import first_pages

    settings.OCR_MAX_PAGES = 3
    assert len(PdfReader(io.BytesIO(first_pages(_pages(10)))).pages) == 3
    short = _pages(2)
    assert first_pages(short) is short


def test_a_document_stopped_by_the_time_limit_says_why():
    from billiard.exceptions import SoftTimeLimitExceeded

    from apps.documents.services.errors import TOO_SLOW, friendly

    assert friendly(SoftTimeLimitExceeded()) == TOO_SLOW


def test_reading_a_document_has_a_time_limit(settings):
    from apps.documents.tasks import process_document_task

    assert process_document_task.soft_time_limit == settings.DOCUMENT_TASK_TIME_LIMIT
    assert process_document_task.time_limit > process_document_task.soft_time_limit


# ---------------------------------------------------------------- QA-099


@pytest.mark.django_db
def test_downloading_a_missing_file_is_a_404_that_says_so(client, user, org, settings):
    settings.DEBUG = False
    client.force_login(user)
    doc, _ = ingest_bytes(org, "a.pdf", make_pdf("COMMERCIAL INVOICE\nInvoice No: INV-99"), process="none")
    doc.file.storage.delete(doc.file.name)
    r = client.get(reverse("review:document_file", args=[doc.pk]))
    assert r.status_code == 404 and "missing from storage" in r.content.decode()


@pytest.mark.django_db
def test_statement_file_is_removed_only_after_its_row(org, approver, client, django_capture_on_commit_callbacks):
    from django.core.files.base import ContentFile

    from apps.close.models import VendorStatement as Statement

    st = Statement(organization=org, original_filename="s.csv", source_format="csv", sha256="0" * 64)
    st.file.save("s.csv", ContentFile(b"date,amount\n"), save=True)
    storage, name = st.file.storage, st.file.name
    client.force_login(approver)
    client.raise_request_exception = True
    with mock.patch("apps.close.models.VendorStatement.delete", side_effect=RuntimeError("db failed")):
        with pytest.raises(RuntimeError):
            client.post(reverse("close:statement_delete", args=[st.pk]))
    assert storage.exists(name)   # the row stays, and so does its file
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("close:statement_delete", args=[st.pk]))
    assert not Statement.objects.filter(pk=st.pk).exists() and not storage.exists(name)


# ---------------------------------------------------------------- deployment files (QA-093, 100, 103, 104, 105)


def test_redis_keeps_queued_work_on_disk():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "--appendonly yes" in compose and "redisdata:/data" in compose


def test_caddy_accepts_the_largest_upload_the_app_does():
    import re

    from django.conf import settings

    caddy_mb = int(re.search(r"max_size (\d+)MB", (ROOT / "deploy" / "Caddyfile").read_text()).group(1))
    assert caddy_mb > max(settings.INTAKE_MAX_ARCHIVE_MB, settings.INBOUND_EMAIL_MAX_MB)


def test_backup_script_saves_the_uploaded_files():
    script = (ROOT / "deploy" / "backup.sh").read_text()
    assert "tar czf - -C /app/media" in script and "docker-compose.prod.yml" in script and "minio" not in script


def test_production_installs_locked_versions_without_test_tools():
    assert "requirements.lock" in (ROOT / "Dockerfile").read_text()
    assert "pytest" not in (ROOT / "requirements.txt").read_text()
    lock = (ROOT / "requirements.lock").read_text()
    assert "django==" in lock.lower() and "--hash=sha256:" in lock and "pytest" not in lock


def test_web_server_scales_with_the_machine(monkeypatch):
    import runpy

    monkeypatch.delenv("GUNICORN_WORKERS", raising=False)
    conf = runpy.run_path(str(ROOT / "config" / "gunicorn.conf.py"))
    assert conf["worker_class"] == "gthread" and conf["threads"] == 4 and 3 <= conf["workers"] <= 9
    monkeypatch.setenv("GUNICORN_WORKERS", "2")
    assert runpy.run_path(str(ROOT / "config" / "gunicorn.conf.py"))["workers"] == 2
