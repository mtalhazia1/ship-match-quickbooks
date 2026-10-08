"""The shared demo accounts (from seed_demo) that every visitor of a DEMO_MODE server signs in with."""
from __future__ import annotations

from django.conf import settings

ADMIN_USERNAME, ADMIN_PASSWORD, ADMIN_EMAIL = "admin", "admin", "admin@example.com"

TIPS = {
    "admin": "Team, settings, QuickBooks, vendor learning and the audit log",
    "reviewer": "Correct values, move documents and accept warnings",
    "approver": "Override errors with a reason, approve up to USD 50,000",
}


def demo_accounts() -> list[dict]:
    from apps.core.management.commands.seed_demo import DEMO_USERS

    accounts = [{"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "email": ADMIN_EMAIL, "name": "Admin",
                 "role": "Admin", "tip": TIPS["admin"]}]
    for username, password, first, last, role, _limit in DEMO_USERS:
        accounts.append({"username": username, "password": password, "email": f"{username}@example.com",
                         "name": f"{first} {last}".strip(), "role": str(role.label), "tip": TIPS.get(role.value, "")})
    return accounts


def demo_usernames() -> set[str]:
    return {a["username"] for a in demo_accounts()}


def demo_emails() -> set[str]:
    return {a["email"].lower() for a in demo_accounts()}


def is_demo_user(user) -> bool:
    """A shared demo account on a DEMO_MODE server."""
    return (settings.DEMO_MODE and getattr(user, "is_authenticated", False)
            and user.get_username() in demo_usernames())
