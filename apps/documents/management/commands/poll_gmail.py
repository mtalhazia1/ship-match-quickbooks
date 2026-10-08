from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Organization
from apps.mailboxes.services import gmail
from apps.mailboxes.services.polling import poll_mailbox


class Command(BaseCommand):
    help = "Import new attachments from the organization's Gmail label now (same path as the scheduled check)."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="demo")
        parser.add_argument("--async", dest="use_async", action="store_true")

    def handle(self, *args, **opts):
        org = Organization.objects.get(slug=opts["org"])
        mailbox = gmail.sync(org)
        if mailbox is None:
            raise CommandError(f"Gmail isn't signed in for {org.slug}. Run: python manage.py gmail_auth --org {org.slug}")
        stats = poll_mailbox(mailbox, process="async" if opts["use_async"] else "sync")
        self.stdout.write((self.style.SUCCESS if stats.get("status") == "ok" else self.style.ERROR)(str(stats)))
