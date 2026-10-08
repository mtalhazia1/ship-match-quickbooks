"""Demo organization and users for local evaluation (idempotent; never run against production data).

Users created (change these passwords if the server is reachable by anyone else):
  admin     / admin                -> Admin of the demo organization (--superuser also makes it a platform superuser)
  reviewer  / reviewer-demo-pass   -> Reviewer: edits and accepts warnings, cannot approve
  approver  / approver-demo-pass   -> Approver with a 50,000 approval limit
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.core.models import Membership, Organization

DEMO_USERS = [
    # username, password, first, last, role, limit
    ("reviewer", "reviewer-demo-pass", "Sam", "Patel", Membership.Role.REVIEWER, None),
    ("approver", "approver-demo-pass", "Maria", "Lopez", Membership.Role.APPROVER, Decimal("50000.00")),
]


class Command(BaseCommand):
    help = "Create a demo organization, an admin and demo reviewer/approver users (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="demo")
        parser.add_argument("--name", default="Acme Imports LLC (demo)")
        parser.add_argument("--username", default="admin")
        parser.add_argument("--password", default="admin")
        parser.add_argument("--email", default="admin@example.com")
        parser.add_argument("--no-demo-users", action="store_true", help="Only create the admin")
        parser.add_argument("--superuser", action="store_true",
                            help="Also make the admin a platform superuser (Django admin site; still no access "
                                 "to organizations it isn't a member of)")

    def handle(self, *args, **opts):
        org, _ = Organization.objects.get_or_create(slug=opts["org"], defaults={"name": opts["name"]})
        if not org.fx_rates:  # demo rates so approval limits work on EUR invoices
            org.fx_rates = {"EUR": "1.08", "GBP": "1.27", "CNY": "0.14"}
            org.save(update_fields=["fx_rates"])
        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=opts["username"],
            defaults={"email": opts["email"], "is_staff": opts["superuser"], "is_superuser": opts["superuser"]})
        if created:
            user.set_password(opts["password"])
            user.save()
        Membership.objects.update_or_create(user=user, organization=org, defaults={"role": Membership.Role.ADMIN})
        made = [user.username]

        if not opts["no_demo_users"]:
            for username, password, first, last, role, limit in DEMO_USERS:
                u, new = User.objects.get_or_create(
                    username=username, defaults={"email": f"{username}@example.com", "first_name": first, "last_name": last})
                if new:
                    u.set_password(password)
                    u.save()
                Membership.objects.get_or_create(user=u, organization=org,
                                                 defaults={"role": role, "approval_limit": limit})
                made.append(username)
        self.stdout.write(self.style.SUCCESS(
            f"Organization '{org.slug}' ready with users: {', '.join(made)}. Passwords are listed in README.md."))
