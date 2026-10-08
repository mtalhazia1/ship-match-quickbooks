"""ZIP archives: unpacked into child documents, with limits against harmful archives."""
import os
import struct
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.core.models import AuditEvent
from apps.documents.models import Document
from apps.documents.services.ingest import RejectedFile, ingest_bytes
from synthetic import extra


@pytest.fixture
def files(dataset):
    pdf = dataset / "pdf"
    return {
        "ci": (pdf / "S01_1_commercial_invoice.pdf").read_bytes(),
        "bl": (pdf / "S01_2_bill_of_lading.pdf").read_bytes(),
        "fi": (pdf / "S01_3_freight_invoice.pdf").read_bytes(),
    }


def _mixed_zip(files) -> bytes:
    return extra.zip_of({
        "March/S01_1_commercial_invoice.pdf": files["ci"],
        "March/S01_2_bill_of_lading.png": extra.photo(files["bl"], "PNG"),
        "March/rates.xlsx": extra.invoice_xlsx("OSLN1234567890", ["OSLU1234565"])[0],
        "__MACOSX/March/._rates.xlsx": b"\x00\x05\x16\x07",
        "March/.DS_Store": b"\x00\x00\x00\x01Bud1",
        "March/terms.docx": b"PK\x03\x04 not really",
        "../../etc/evil.pdf": b"%PDF-1.4 evil",
        "older.zip": extra.zip_of({"S01_3_freight_invoice.pdf": files["fi"]}),
    })


@pytest.mark.django_db
def test_zip_becomes_one_document_per_file(org, files):
    archive, created = ingest_bytes(org, "march-docs.zip", _mixed_zip(files), process="sync")
    assert created and archive.status == Document.Status.ARCHIVE and archive.source_format == "archive"
    status = {m["name"]: (m["status"], m["reason"]) for m in archive.intake["members"]}
    assert status["March/S01_1_commercial_invoice.pdf"][0] == "added"
    assert status["March/S01_2_bill_of_lading.png"][0] == "added"
    assert status["March/rates.xlsx"][0] == "added"
    assert status["__MACOSX/March/._rates.xlsx"][0] == "ignored"
    assert status["March/.DS_Store"][0] == "ignored"
    assert status["March/terms.docx"] == ("skipped", "Word document; save it as PDF")
    assert status["../../etc/evil.pdf"] == ("skipped", "unsafe file path inside the ZIP")
    assert status["older.zip"][0] == "added"  # one level of nesting is opened

    children = {c.original_filename: c for c in archive.children.all()}
    assert set(children) == {"S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.png", "rates.xlsx", "older.zip"}
    assert children["S01_1_commercial_invoice.pdf"].status == Document.Status.MATCHED  # processed like any upload
    nested = children["older.zip"]
    assert nested.status == Document.Status.ARCHIVE
    assert nested.children.get().original_filename == "S01_3_freight_invoice.pdf"
    assert nested.children.get().status == Document.Status.MATCHED
    with archive.file.open("rb") as fh:  # the archive's PDF copy lists its contents
        assert fh.read(5) == b"%PDF-"
    assert AuditEvent.objects.filter(action="document.archive_unpacked", object_id=str(archive.pk)).exists()
    # The ZIP itself is never read or matched as a document.
    from apps.documents.services.pipeline import process_document

    assert process_document(archive.pk).status == Document.Status.ARCHIVE


@pytest.mark.django_db
def test_file_received_before_is_not_added_twice(org, files):
    first, _ = ingest_bytes(org, "invoice.pdf", files["ci"], process="none")
    archive, _ = ingest_bytes(org, "again.zip", extra.zip_of({"copy.pdf": files["ci"], "bl.pdf": files["bl"]}),
                              process="none")
    members = {m["name"]: m for m in archive.intake["members"]}
    assert members["copy.pdf"]["status"] == "duplicate" and members["copy.pdf"]["document"] == first.pk
    assert archive.children.count() == 1
    same, created = ingest_bytes(org, "again-renamed.zip", extra.zip_of({"copy.pdf": files["ci"], "bl.pdf": files["bl"]}),
                                 process="none")
    assert not created and same.pk == archive.pk


@pytest.mark.django_db
def test_zip_bomb_is_refused_and_nothing_is_kept(org, files, settings):
    bomb = extra.zip_of({"invoice.pdf": files["ci"], "huge.pdf": b"%PDF" + b"\x00" * (6 * 1024 * 1024)})
    with pytest.raises(RejectedFile, match="zip bombs"):
        ingest_bytes(org, "bomb.zip", bomb)
    assert not Document.objects.filter(organization=org).exists()
    media = settings.MEDIA_ROOT
    assert not os.path.exists(media) or not any(f for _, _, f in os.walk(media))


@pytest.mark.django_db
def test_limits_on_count_and_total_size(org, settings):
    settings.INTAKE_ZIP_MAX_FILES = 3
    many = extra.zip_of({f"inv{n}.pdf": b"%PDF-1.4 " + bytes([n]) * 20 for n in range(5)})
    with pytest.raises(RejectedFile, match="more than 3 files inside"):
        ingest_bytes(org, "many.zip", many)
    settings.INTAKE_ZIP_MAX_FILES, settings.INTAKE_ZIP_MAX_TOTAL_MB = 200, 1
    large = extra.zip_of({f"scan{n}.pdf": b"%PDF" + os.urandom(600 * 1024) for n in range(2)}, zipfile.ZIP_STORED)
    with pytest.raises(RejectedFile, match="add up to more than 1 MB"):
        ingest_bytes(org, "large.zip", large)


def _lying_zip(real: bytes, claimed: int) -> bytes:
    """A ZIP whose headers say the file is smaller than it really is."""
    raw = bytearray(extra.zip_of({"invoice.pdf": real}))
    for sig, offset in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        pos = raw.find(sig)
        raw[pos + offset:pos + offset + 4] = struct.pack("<I", claimed)
    return bytes(raw)


@pytest.mark.django_db
def test_sizes_are_checked_while_reading(org, files):
    with pytest.raises(RejectedFile, match="damaged"):  # zipfile stops at the declared size; the CRC then fails
        ingest_bytes(org, "liar.zip", _lying_zip(files["ci"], 1000))
    assert not Document.objects.filter(organization=org).exists()


@pytest.mark.django_db
def test_nesting_deeper_than_one_level_is_skipped(org, files):
    inner = extra.zip_of({"deep.pdf": files["fi"]})
    middle = extra.zip_of({"inner.zip": inner, "bl.pdf": files["bl"]})
    archive, _ = ingest_bytes(org, "outer.zip", extra.zip_of({"middle.zip": middle}), process="none")
    middle_doc = archive.children.get()
    reasons = {m["name"]: m for m in middle_doc.intake["members"]}
    assert reasons["inner.zip"]["status"] == "skipped" and "unpack it first" in reasons["inner.zip"]["reason"]
    assert reasons["bl.pdf"]["status"] == "added"


@pytest.mark.django_db
def test_password_protected_and_damaged_archives(org, files):
    raw = bytearray(extra.zip_of({"locked.pdf": files["ci"], "open.pdf": files["bl"]}))
    for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):  # set the 'encrypted' flag of the first file
        pos = raw.find(sig)
        raw[pos + offset] |= 0x1
    archive, _ = ingest_bytes(org, "mixed.zip", bytes(raw), process="none")
    locked = next(m for m in archive.intake["members"] if m["name"] == "locked.pdf")
    assert locked["status"] == "skipped" and "password" in locked["reason"]
    with pytest.raises(RejectedFile, match="damaged"):
        ingest_bytes(org, "broken.zip", b"PK\x03\x04" + b"\x00" * 40)
    with pytest.raises(RejectedFile, match="no PDF, image or spreadsheet files could be added .*a.docx: "):
        ingest_bytes(org, "words.zip", extra.zip_of({"a.docx": b"x", "b.txt": b"y"}))


@pytest.mark.django_db
def test_upload_reports_what_was_added_and_skipped(client, user, org, files):
    client.force_login(user)
    r = client.post(reverse("review:upload"),
                    {"files": [SimpleUploadedFile("march-docs.zip", _mixed_zip(files), "application/zip")]}, follow=True)
    text = r.content.decode()
    assert "Unpacked march-docs.zip: 4 documents added" in text
    assert "2 system or hidden files were ignored" in text
    assert "Not added from march-docs.zip: terms.docx: Word document; save it as PDF" in text


@pytest.mark.django_db
def test_archive_page_lists_children_and_children_point_back(client, viewer, org, files):
    archive, _ = ingest_bytes(org, "march-docs.zip", _mixed_zip(files), process="sync")
    client.force_login(viewer)
    page = client.get(reverse("review:document", args=[archive.pk])).content.decode()
    assert "This ZIP held 8 files." in page and "Documents from this file" in page
    assert "S01_2_bill_of_lading.png" in page and "Word document; save it as PDF" in page
    assert "Keep as one document" not in page
    child = archive.children.get(original_filename="rates.xlsx")
    page = client.get(reverse("review:document", args=[child.pk]), follow=True).content.decode()
    assert "From archive" in page and "march-docs.zip" in page
    listing = client.get(reverse("review:documents") + "?view=all").content.decode()
    assert "Archive unpacked" in listing and "From archive march-docs.zip" in listing
    r = client.get(reverse("intake:original", args=[archive.pk]))
    assert r["Content-Type"] == "application/zip"


@pytest.mark.django_db
def test_api_zip_upload_lists_children(client, admin_user, org, files):
    from apps.accounts.models import ApiKey
    from apps.accounts.services.apikeys import create_key

    _, token = create_key(org, "ERP", ApiKey.Role.REVIEWER, admin_user, None)
    zipped = extra.zip_of({"ci.pdf": files["ci"], "notes.txt": b"x"})
    r = client.post(f"/api/{org.slug}/documents", {"file": SimpleUploadedFile("batch.zip", zipped)},
                    HTTP_AUTHORIZATION=f"Bearer {token}")
    body = r.json()
    assert r.status_code == 201 and body["source_format"] == "archive" and body["status"] == "archive"
    assert len(body["children"]) == 1
    assert body["not_added"] == [{"name": "notes.txt", "reason": "text file"}]
    r = client.post(f"/api/{org.slug}/documents", {"file": SimpleUploadedFile("x.zip", b"PK\x03\x04junk")},
                    HTTP_AUTHORIZATION=f"Bearer {token}")
    assert r.status_code == 400 and "damaged" in r.json()["detail"]
