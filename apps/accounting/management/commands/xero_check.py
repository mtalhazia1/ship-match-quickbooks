"""Check the Xero connection end to end, and optionally post one approved shipment or read payment status.

    python manage.py xero_check --org demo
    python manage.py xero_check --org demo --post SHP-000010
    python manage.py xero_check --org demo --payments
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounting.models import PostedBill, XeroConnection
from apps.accounting.services import payments
from apps.accounting.services.posting import PostingBlocked, post_shipment
from apps.accounting.services.providers import XeroProvider
from apps.accounting.services.xero import XeroClient, XeroError, configured, uses_pkce
from apps.core.models import Organization
from apps.shipments.models import Shipment


class Command(BaseCommand):
    help = "Check the Xero connection (organisation, currencies, accounts) and optionally post a shipment."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="demo")
        parser.add_argument("--post", metavar="SHIPMENT", help="Reference of an approved shipment to post, e.g. SHP-000010")
        parser.add_argument("--payments", action="store_true", help="Read the payment status of posted Xero bills now")

    def handle(self, *args, **o):
        ok, bad = self.style.SUCCESS, self.style.ERROR
        org = Organization.objects.filter(slug=o["org"]).first()
        if not org:
            raise CommandError(f"No organization '{o['org']}'")
        if not configured():
            raise CommandError("XERO_CLIENT_ID and XERO_REDIRECT_URI are not set in .env")
        self.stdout.write(f"Sign-in: {'PKCE (no client secret)' if uses_pkce() else 'web app with client secret'}, "
                          f"scopes: {settings.XERO_SCOPES}")
        conn = XeroConnection.objects.filter(organization=org).first()
        if not conn:
            raise CommandError(f"{org.name} is not connected. Open Settings > Accounting and click Connect Xero.")
        if not conn.tenant_id:
            raise CommandError("Xero is signed in but no organisation is chosen yet. Choose one in Settings > Accounting.")
        if conn.needs_reconnect:
            raise CommandError("Xero rejected the stored connection. Connect again in Settings > Accounting.")
        client = XeroClient(conn)
        try:
            client.sync_organisation()
            accounts = client.expense_accounts()
        except XeroError as e:
            raise CommandError(f"Xero call failed: {e}") from e
        self.stdout.write(ok(f"Connected to {conn.tenant_name} (organisation {conn.tenant_id})"))
        others = ", ".join(conn.other_currencies) or "none"
        self.stdout.write(f"Base currency {conn.home_currency}, other currencies: {others}"
                          f"{'' if conn.home_currency == org.home_currency else bad(f'  <-- ShipMatch uses {org.home_currency}')}")
        names = {a["id"]: a["name"] for a in accounts}
        self.stdout.write(f"{len(accounts)} expense accounts with a code. Default: "
                          + (names.get(conn.default_account_code, bad("not set (choose one in Settings > Accounting)"))))
        self.stdout.write(f"New bills are created as {conn.get_bill_status_display().lower()}")
        self.stdout.write(f"Access token valid until {conn.access_expires_at:%Y-%m-%d %H:%M} UTC"
                          + (f", sign-in valid until {conn.refresh_expires_at:%Y-%m-%d} unless used again"
                             if conn.refresh_expires_at else ""))
        if client.day_remaining is not None:
            self.stdout.write(f"Xero API calls left today for this organisation: {client.day_remaining}")

        if o["post"]:
            shipment = Shipment.objects.filter(organization=org, reference=o["post"]).first()
            if not shipment:
                raise CommandError(f"No shipment {o['post']} in {org.name}")
            try:
                summary = post_shipment(shipment, client=client)
            except PostingBlocked as e:
                raise CommandError(str(e)) from e
            self.stdout.write(f"Posting {shipment.reference}: {summary}")
            for pb in PostedBill.objects.filter(shipment=shipment).select_related("document"):
                line = f"  {pb.document.original_filename}: {pb.get_status_display()}"
                line += (f", {pb.get_kind_display().lower()} {pb.display_number} ({pb.qbo_bill_id}), "
                         f"attachment {pb.qbo_attachable_id or '-'}") if pb.qbo_bill_id else ""
                self.stdout.write((ok if pb.status == "posted" else bad)(line + (f"  {pb.error}" if pb.error else "")))

        if o["payments"]:
            summary = payments.sync(org, provider=XeroProvider(conn, client))
            if summary["error"]:
                raise CommandError(f"Payment check failed: {summary['error']}")
            self.stdout.write(ok(f"Payments: {summary['checked']} checked, {summary['changed']} changed"
                                 + (f" ({summary['skipped']})" if summary["skipped"] else "")))
