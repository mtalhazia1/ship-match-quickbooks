"""Nightly reset of a public demo: wipe the demo organizations and build them again from scratch."""
from __future__ import annotations

import contextlib
import io
import logging
import tempfile
from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command, get_commands
from django.db import transaction
from django.db.models import ProtectedError

from apps.accounting.models import PostedBill
from apps.core.models import Membership, Organization
from apps.core.utils import audit
from apps.documents.models import Document

from .accounts import ADMIN_EMAIL, ADMIN_PASSWORD, ADMIN_USERNAME, demo_accounts, demo_usernames

log = logging.getLogger(__name__)


@contextlib.contextmanager
def offline_reading():
    """Build the demo with the free rule reader, so a nightly reset never spends AI credits."""
    old = settings.EXTRACTION_PROVIDER, settings.OCR_PROVIDER
    settings.EXTRACTION_PROVIDER, settings.OCR_PROVIDER = "rules", "text"
    try:
        yield
    finally:
        settings.EXTRACTION_PROVIDER, settings.OCR_PROVIDER = old


def wipe_org(slug: str) -> bool:
    """Delete one organization with its files and every record in it. False if it didn't exist."""
    org = Organization.objects.filter(slug=slug).first()
    if org is None:
        return False
    for doc in Document.objects.filter(organization=org).only("id", "file"):
        try:
            if doc.file:
                doc.file.delete(save=False)
        except Exception:  # noqa: BLE001 - a missing file must not stop the reset
            log.warning("Could not delete file of demo document %s", doc.pk)
    with transaction.atomic():
        PostedBill.objects.filter(organization=org).delete()  # protects documents and shipments
        org.delete()
    return True


def remove_visitor_users() -> int:
    """Users a visitor invited into the demo: no membership left, not staff, not a demo account."""
    removed = 0
    keep = demo_usernames()
    leftover = (get_user_model().objects.filter(is_staff=False, is_superuser=False)
                .exclude(username__in=keep).exclude(pk__in=Membership.objects.values("user_id")))
    for user in leftover:
        try:
            user.delete()
            removed += 1
        except ProtectedError:  # signed decisions elsewhere: keep the record, block the account
            user.is_active = False
            user.save(update_fields=["is_active"])
    return removed


def reset_demo_accounts() -> None:
    """Known passwords, active, no two-factor; never platform admins on a public demo."""
    from apps.accounts.services import mfa

    User = get_user_model()
    for account in demo_accounts():
        user = User.objects.filter(username=account["username"]).first()
        if user is None:
            continue
        user.set_password(account["password"])
        user.is_active = True
        user.email = user.email or account["email"]
        if settings.DEMO_MODE:
            user.is_superuser = user.is_staff = False
        user.save()
        mfa.disable(user)


def seed_rates(slug: str) -> bool:
    """Exchange rates from the money feature, when that command is installed."""
    if "seed_rates" not in get_commands():
        return False
    try:
        call_command("seed_rates", org=slug)
    except TypeError:  # the command takes no --org option
        call_command("seed_rates")
    return True


def _add_customs(folder: str, seed: int) -> None:
    """Customs entries (some with duty errors) and arrival notices dated around today, when installed."""
    try:
        from synthetic.customs import add_to_dataset
    except ImportError:
        return
    try:
        add_to_dataset(folder, seed=seed)
    except Exception:  # the demo still works without them
        import logging

        logging.getLogger(__name__).exception("Couldn't add customs documents to the demo")


def seed_extras(slug: str, stdout=None) -> list[str]:
    """Landed cost with a shared invoice, and month-end data, from the commands that are installed."""
    done = []
    commands = get_commands()
    for name in ("seed_landed", "close_demo"):
        if name not in commands:
            continue
        try:
            call_command(name, org=slug, stdout=stdout or io.StringIO())
            done.append(name)
        except Exception:  # one missing sample must not stop the reset
            import logging

            logging.getLogger(__name__).exception("Demo reset: %s failed", name)
    return done


def learning_example(org: Organization) -> int:
    """Two invoices from a customs broker: a reviewer corrects the first, the second is read right.
    Shows vendor learning without anyone having to set it up. Returns the documents added."""
    from apps.documents.services.corrections import after_correction, correct_field
    from apps.documents.services.ingest import ingest_bytes
    from apps.learning.samples import broker_invoice_pdf
    from apps.shipments.models import Shipment

    reviewer = get_user_model().objects.filter(username="reviewer").first()
    shipments = list(Shipment.objects.filter(organization=org).exclude(bl_number="").order_by("id")[:2])
    if reviewer is None or len(shipments) < 2:
        return 0
    days = [date(2026, 8, 3), date(2026, 9, 4)]  # day <= 12: ambiguous until the vendor's order is learned
    docs = []
    for i, (ref, day, s) in enumerate(zip(["CCB/24117", "CCB/24206"], days, shipments)):
        pdf = broker_invoice_pdf(ref, day, bl_number=s.bl_number, containers=list(s.container_numbers),
                                 po_numbers=list(s.po_numbers))
        doc, _ = ingest_bytes(org, f"coastline-{ref.split('/')[1]}.pdf", pdf, source=Document.Source.EMAIL,
                              process="sync")
        docs.append(doc)
        if i == 0:
            for name, value in (("invoice_number", ref), ("invoice_date", day.isoformat())):
                if correct_field(doc, name, value, reviewer):
                    after_correction(doc, name)
    return len(docs)


def reset_demo(shipments: int = 12, seed: int = 42, documents: bool = True, use_ai: bool = False,
               stdout=None) -> dict:
    from synthetic.generator import generate

    slugs = list(settings.DEMO_ORGS) or ["demo"]
    summary = {"orgs": slugs, "wiped": [], "users_removed": 0, "documents": 0, "rates": False, "learning": 0}
    reading = contextlib.nullcontext() if use_ai else offline_reading()
    with reading:
        for slug in slugs:
            if wipe_org(slug):
                summary["wiped"].append(slug)
        if settings.DEMO_MODE:
            summary["users_removed"] = remove_visitor_users()
        for slug in slugs:
            call_command("seed_demo", org=slug, username=ADMIN_USERNAME, password=ADMIN_PASSWORD, email=ADMIN_EMAIL,
                         stdout=stdout)
        reset_demo_accounts()
        if documents:
            with tempfile.TemporaryDirectory(prefix="shipmatch-demo-") as tmp:
                generate(tmp, n_shipments=shipments, seed=seed, scanned=1)
                _add_customs(tmp, seed)
                for slug in slugs:
                    call_command("ingest_folder", tmp, org=slug, stdout=stdout)
            for slug in slugs:
                org = Organization.objects.get(slug=slug)
                summary["learning"] += learning_example(org)
                summary["documents"] += Document.objects.filter(organization=org).count()
        for slug in slugs:
            summary["rates"] = seed_rates(slug) or summary["rates"]
            if documents:
                summary["extras"] = seed_extras(slug, stdout=stdout)
    audit(None, "demo.reset", "demo", **summary)
    return summary
