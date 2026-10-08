from django.core.management.base import BaseCommand

from apps.mailboxes.models import Mailbox
from apps.mailboxes.services import gmail
from apps.mailboxes.services.polling import due_mailboxes, poll_mailbox


class Command(BaseCommand):
    help = "Check connected mailboxes (Microsoft 365, IMAP, Gmail) for new emails now."

    def add_arguments(self, parser):
        parser.add_argument("--org", help="Only this organization (slug)")
        parser.add_argument("--mailbox", type=int, help="Only this mailbox (ID from the admin)")
        parser.add_argument("--sync", action="store_true", help="Read documents now instead of queueing them")

    def handle(self, *args, **opts):
        if opts["mailbox"]:
            mailboxes = Mailbox.objects.select_related("organization").filter(pk=opts["mailbox"])
        else:
            mailboxes = due_mailboxes()
            if opts["org"]:
                mailboxes = mailboxes.filter(organization__slug=opts["org"])
        for org in {m.organization for m in mailboxes}:
            gmail.sync(org)
        if not mailboxes:
            self.stdout.write("No enabled mailboxes to check.")
        for mailbox in mailboxes:
            stats = poll_mailbox(mailbox, process="sync" if opts["sync"] else "async")
            style = self.style.SUCCESS if stats.get("status") == "ok" else self.style.ERROR
            self.stdout.write(style(f"{mailbox.organization.slug} / {mailbox.label} ({mailbox.get_kind_display()}): {stats}"))
