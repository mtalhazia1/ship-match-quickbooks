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
