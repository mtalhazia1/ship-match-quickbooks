"""Check the QuickBooks connection end to end, and optionally post one approved shipment.

    python manage.py qbo_check --org demo
    python manage.py qbo_check --org demo --post SHP-000010
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounting.models import PostedBill, QBOConnection
from apps.accounting.services.posting import PostingBlocked, post_shipment
from apps.accounting.services.quickbooks import QBOClient, QBOError
from apps.core.models import Organization
from apps.shipments.models import Shipment


class Command(BaseCommand):
    help = "Check the QuickBooks connection (company, currency, accounts) and optionally post a shipment."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="demo")
        parser.add_argument("--post", metavar="SHIPMENT", help="Reference of an approved shipment to post, e.g. SHP-000010")

    def handle(self, *args, **o):
        ok, bad = self.style.SUCCESS, self.style.ERROR
        org = Organization.objects.filter(slug=o["org"]).first()
        if not org:
            raise CommandError(f"No organization '{o['org']}'")
        self.stdout.write(f"Environment: {settings.QBO_ENVIRONMENT}, minor version {settings.QBO_MINOR_VERSION}")
        if not (settings.QBO_CLIENT_ID and settings.QBO_CLIENT_SECRET):
            raise CommandError("QBO_CLIENT_ID and QBO_CLIENT_SECRET are not set in .env")
        conn = QBOConnection.objects.filter(organization=org).first()
        if not conn:
            raise CommandError(f"{org.name} is not connected. Open Settings > Accounting and click Connect QuickBooks.")
        if conn.needs_reconnect:
            raise CommandError("Intuit rejected the stored connection. Connect again in Settings > Accounting.")
        client = QBOClient(conn)
        try:
            client.sync_company()
            accounts = client.expense_accounts()
        except QBOError as e:
            raise CommandError(f"QuickBooks call failed: {e} (Intuit reference {e.intuit_tid or 'none'})") from e
        self.stdout.write(ok(f"Connected to {conn.company_name} (company {conn.realm_id})"))
        self.stdout.write(f"Home currency {conn.home_currency}, multicurrency {'on' if conn.multicurrency else 'off'}"
                          f"{'' if conn.home_currency == org.home_currency else bad(f'  <-- ShipMatch uses {org.home_currency}')}")
        names = {a["id"]: a["name"] for a in accounts}
        self.stdout.write(f"{len(accounts)} expense accounts. Default: "
                          + (names.get(conn.default_expense_account_id, bad("not set (choose one in Settings > Accounting)"))))
        self.stdout.write(f"Access token valid until {conn.access_expires_at:%Y-%m-%d %H:%M} UTC"
                          + (f", connection valid until {conn.refresh_expires_at:%Y-%m-%d}" if conn.refresh_expires_at else ""))

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
                line += f", bill {pb.qbo_bill_id}, attachment {pb.qbo_attachable_id or '-'}" if pb.qbo_bill_id else ""
                self.stdout.write((ok if pb.status == "posted" else bad)(line + (f"  {pb.error}" if pb.error else "")))
