"""QA-043, QA-002 and QA-074: files that only look like PDFs, raw error text on pages, and the upload race.

* A file that merely began with "%PDF" was accepted, then failed later with a Python traceback and the
  server's file path written on the document page. Password-protected PDFs, which vendors send often, did the same.
* Two identical uploads at the same moment (a double-click, or an email and an upload together) both passed the
  "seen these bytes?" check; the second then hit the database's unique constraint and the user got HTTP 500."""
import io
import os
import zipfile

import pytest
from django.urls import reverse
from pypdf import PdfReader, PdfWriter

from apps.core.models import AuditEvent
from apps.documents.models import Document
from apps.documents.services import errors
from apps.documents.services.ingest import DuplicateFile, RejectedFile, ingest_bytes, store_unique
from apps.documents.services.pipeline import process_document
from apps.intake.services import formats


@pytest.fixture
def pdf_bytes(dataset):
    return (dataset / "pdf" / "S01_1_commercial_invoice.pdf").read_bytes()


def _protected(pdf_bytes, user_password):
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(pdf_bytes)).pages:
        writer.add_page(page)
    writer.encrypt(user_password=user_password, owner_password="owner-secret")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _stored_files(settings):
    return sorted(os.path.join(root, f) for root, _, files in os.walk(settings.MEDIA_ROOT) for f in files)


# ---------------------------------------------------------------- QA-043: what intake accepts


def test_a_real_pdf_is_accepted(pdf_bytes, org):
    assert formats.detect("invoice.pdf", pdf_bytes).kind == formats.PDF
    formats.check_pdf("invoice.pdf", pdf_bytes)   # returns quietly
    doc, created = ingest_bytes(org, "invoice.pdf", pdf_bytes, process="none")
    assert created and doc.source_format == "pdf"


@pytest.mark.parametrize("junk", [
    b"%PDF-1.4 hello",
    b"%PDF-1.7\n" + b"\x00" * 200,
    b"%PDF",
    b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n",   # a header and an object, but no pages
])
def test_a_file_that_only_starts_like_a_pdf_is_refused(junk):
    with pytest.raises(RejectedFile) as e:
        formats.check_pdf("invoice.pdf", junk)
    assert "isn't a readable PDF" in str(e.value) and "invoice.pdf" in str(e.value)


@pytest.mark.django_db
def test_ingest_refuses_it_and_stores_nothing(org):
    with pytest.raises(RejectedFile):
        ingest_bytes(org, "invoice.pdf", b"%PDF-1.4 hello", process="none")
    assert not Document.objects.filter(organization=org).exists()


@pytest.mark.django_db
def test_a_huge_junk_file_is_refused_for_its_size_without_being_parsed(org, settings, monkeypatch):
    settings.INTAKE_MAX_FILE_MB = 1
    parsed = []
    monkeypatch.setattr(formats, "check_pdf", lambda *a, **k: parsed.append(a))
    with pytest.raises(RejectedFile) as e:
        ingest_bytes(org, "huge.pdf", b"%PDF-1.4" + b"0" * 1_200_000, process="none")
    assert "larger than 1 MB" in str(e.value)
    assert parsed == []


def test_a_password_protected_pdf_is_refused_with_the_way_out(pdf_bytes):
    with pytest.raises(RejectedFile) as e:
        formats.check_pdf("locked.pdf", _protected(pdf_bytes, "secret"))
    assert "protected with a password" in str(e.value) and "Save as PDF" in str(e.value)


def test_a_pdf_that_opens_without_a_password_is_accepted(pdf_bytes):
    """Owner-password-only PDFs (copy/print restrictions) open freely and are very common."""
    formats.check_pdf("restricted.pdf", _protected(pdf_bytes, ""))   # does not raise


def test_a_truncated_pdf_never_crashes_intake(pdf_bytes):
    try:
        formats.check_pdf("cut.pdf", pdf_bytes[: len(pdf_bytes) // 3])
    except RejectedFile:
        pass   # refused with a message is fine; repaired and accepted is fine; an unhandled error is not


@pytest.mark.django_db
def test_the_upload_page_refuses_it_and_stores_nothing(client, user, org):
    client.force_login(user)
    r = client.post(reverse("review:upload"), {"files": io.BytesIO(b"%PDF-1.4 hello")}, follow=True)
    # the form field is posted as a file named by the test client; use a named upload
    from django.core.files.uploadedfile import SimpleUploadedFile

    r = client.post(reverse("review:upload"), {"files": SimpleUploadedFile("bad.pdf", b"%PDF-1.4 hello")},
                    follow=True)
    assert "isn't a readable PDF" in "".join(str(m) for m in r.context["messages"])
    assert not Document.objects.filter(organization=org).exists()


# ---------------------------------------------------------------- QA-002: what a page may say about a failure


TRACEBACK = (
    "No /Root object! - Is this really a PDF?\nTraceback (most recent call last):\n"
    '  File "C:\\Users\\Someone\\Documents\\shipmatch\\.venv\\Lib\\site-packages\\pdfplumber\\pdf.py", line 50, in '
    "__init__\n    self.doc = PDFDocument(PDFParser(stream), password=password or \"\")\n"
)


def test_stored_tracebacks_are_shown_in_plain_language():
    shown = errors.display(TRACEBACK)
    assert shown == errors.DAMAGED
    for leak in ("Traceback", "Users", "pdfplumber", ".venv", "File "):
        assert leak not in shown


@pytest.mark.parametrize("stored, expected", [
    ("", ""),
    ("Total doesn't match line items.", "Total doesn't match line items."),   # a short human sentence passes through
    ("Boom\nTraceback (most recent call last):\n  File \"/srv/app/x.py\", line 1", errors.GENERIC),
    ("PDFPasswordIncorrect\nTraceback (most recent call last):", errors.PASSWORD),
    ("file at /home/deploy/app/media/x.pdf could not be opened", errors.GENERIC),
])
def test_display(stored, expected):
    assert errors.display(stored) == expected


@pytest.mark.parametrize("exc, expected", [
    (RuntimeError("boom"), errors.GENERIC),
    (ValueError("PDF is encrypted, a password is needed"), errors.PASSWORD),
    (type("PDFSyntaxError", (Exception,), {})("bad xref"), errors.DAMAGED),
])
def test_friendly(exc, expected):
    assert errors.friendly(exc) == expected


def test_scrub_removes_server_paths():
    assert "Users" not in errors.scrub("cannot open C:\\Users\\Someone\\x\\a.pdf now")
    assert "/srv" not in errors.scrub("cannot open /srv/app/media/a.pdf now")
    assert errors.scrub("plain message") == "plain message"


@pytest.mark.django_db
def test_a_failed_document_stores_and_logs_no_internals(org, pdf_bytes, monkeypatch):
    doc, _ = ingest_bytes(org, "invoice.pdf", pdf_bytes, process="none")

    def explode(*args, **kwargs):
        raise RuntimeError("cannot read C:\\Users\\Someone\\secret\\invoice.pdf at line 50")

    monkeypatch.setattr("apps.documents.services.pipeline.extract", explode)
    doc = process_document(doc.pk)

    assert doc.status == Document.Status.ERROR
    assert doc.error == errors.GENERIC
    event = AuditEvent.objects.filter(action="document.error").latest("id")
    assert "Users" not in event.data["error"] and "<path>" in event.data["error"]


@pytest.mark.django_db
def test_pages_never_show_a_stored_traceback(client, user, org, pdf_bytes):
    doc, _ = ingest_bytes(org, "invoice.pdf", pdf_bytes, process="none")
    Document.objects.filter(pk=doc.pk).update(status=Document.Status.ERROR, error=TRACEBACK)
    client.force_login(user)

    for url in (reverse("review:document", args=[doc.pk]), reverse("review:documents") + "?tab=all&view=all"):
        html = client.get(url).content.decode()
        assert "Traceback" not in html and "Users" not in html and "pdfplumber" not in html
        assert "readable PDF" in html


# ---------------------------------------------------------------- QA-074: two identical uploads at once


@pytest.fixture
def lose_the_race(monkeypatch):
    """Make the 'have we seen these bytes?' check miss once, as it does when another request is mid-save."""
    real = Document.objects.filter
    state = {"calls": 0}

    def filter_(*args, **kwargs):
        state["calls"] += 1
        qs = real(*args, **kwargs)
        return qs.none() if state["calls"] == 1 else qs

    monkeypatch.setattr(Document.objects, "filter", filter_)
    return state


@pytest.mark.django_db
def test_losing_the_race_returns_the_first_document(org, pdf_bytes, settings, monkeypatch):
    first, created = ingest_bytes(org, "invoice.pdf", pdf_bytes, process="none")
    assert created
    before = _stored_files(settings)

    real = Document.objects.filter
    calls = {"n": 0}

    def filter_(*args, **kwargs):
        calls["n"] += 1
        qs = real(*args, **kwargs)
        return qs.none() if calls["n"] == 1 else qs

    monkeypatch.setattr(Document.objects, "filter", filter_)
    second, created = ingest_bytes(org, "copy-of-invoice.pdf", pdf_bytes, process="none")

    assert (second.pk, created) == (first.pk, False)
    assert Document.objects.filter(organization=org).count() == 1
    assert _stored_files(settings) == before   # the copy written to storage was removed again
    event = AuditEvent.objects.filter(action="document.duplicate_file").latest("id")
    assert event.data["race"] is True and event.data["filename"] == "copy-of-invoice.pdf"


@pytest.mark.django_db
def test_losing_the_race_with_a_zip_is_handled_too(org, pdf_bytes, settings, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.pdf", pdf_bytes)
    archive, created = ingest_bytes(org, "bundle.zip", buf.getvalue(), process="none")
    assert created
    before = _stored_files(settings)

    real = Document.objects.filter
    calls = {"n": 0}

    def filter_(*args, **kwargs):
        calls["n"] += 1
        qs = real(*args, **kwargs)
        return qs.none() if calls["n"] == 1 else qs

    monkeypatch.setattr(Document.objects, "filter", filter_)
    again, created = ingest_bytes(org, "bundle-again.zip", buf.getvalue(), process="none")

    assert (again.pk, created) == (archive.pk, False)
    assert len(_stored_files(settings)) == len(before)


@pytest.mark.django_db
def test_store_unique_raises_duplicate_for_the_same_bytes(org, pdf_bytes):
    first, _ = ingest_bytes(org, "invoice.pdf", pdf_bytes, process="none")
    clone = Document(organization=org, original_filename="x.pdf", sha256=first.sha256, source_format="pdf")

    with pytest.raises(DuplicateFile) as e:
        store_unique(clone)

    assert e.value.existing.pk == first.pk
    assert Document.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_the_upload_page_survives_the_race(client, user, org, pdf_bytes, lose_the_race):
    from django.core.files.uploadedfile import SimpleUploadedFile

    ingest_bytes(org, "invoice.pdf", pdf_bytes, process="none")
    lose_the_race["calls"] = 0   # the check inside the view is the first call that now misses
    client.force_login(user)

    r = client.post(reverse("review:upload"), {"files": SimpleUploadedFile("invoice.pdf", pdf_bytes)}, follow=True)

    assert r.status_code == 200
    assert "uploaded before" in "".join(str(m) for m in r.context["messages"])
