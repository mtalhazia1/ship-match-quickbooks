"""Which webhook addresses ShipMatch will call (guard against server-side request forgery).

Only https URLs on the hosts Slack and Microsoft publish for incoming webhooks are accepted, on the
default port, without user names or passwords. The check runs when a channel is saved and again
before every request; redirects are never followed.
"""
from __future__ import annotations

from urllib.parse import urlsplit

SLACK_HOSTS = ("hooks.slack.com",)
TEAMS_SUFFIXES = (".webhook.office.com", ".logic.azure.com", ".environment.api.powerplatform.com")

HELP = {
    "slack": "Use a Slack incoming webhook address. It starts with https://hooks.slack.com/services/ "
             "(Slack: Apps > Incoming Webhooks > Add New Webhook to Workspace).",
    "teams": "Use the address of a Teams workflow (Power Automate: “When a Teams webhook request is received”). "
             "It ends in .logic.azure.com, .environment.api.powerplatform.com or .webhook.office.com.",
}


class WebhookURLError(ValueError):
    pass


def host_allowed(kind: str, host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if kind == "slack":
        return host in SLACK_HOSTS
    if kind == "teams":
        return any(host.endswith(s) and len(host) > len(s) for s in TEAMS_SUFFIXES)
    return False


def check_webhook_url(kind: str, raw: str) -> str:
    """Return the URL if ShipMatch may call it, or raise WebhookURLError saying how to fix it."""
    url = (raw or "").strip()
    if kind not in HELP:
        raise WebhookURLError("Only Slack and Microsoft Teams channels use a webhook address.")
    if not url:
        raise WebhookURLError(f"Paste the webhook address. {HELP[kind]}")
    if len(url) > 2000 or any(c.isspace() or ord(c) < 32 or c == "\\" for c in url):
        raise WebhookURLError(f"That address has spaces or characters a webhook address never has. {HELP[kind]}")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise WebhookURLError(f"That isn't a valid web address. {HELP[kind]}")
    if parts.scheme.lower() != "https":
        raise WebhookURLError(f"The address must start with https:// so messages are encrypted. {HELP[kind]}")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise WebhookURLError("The address must not contain a user name or password.")
    if port not in (None, 443):
        raise WebhookURLError(f"The address must use the standard https port. {HELP[kind]}")
    host = (parts.hostname or "").lower()
    if not host_allowed(kind, host):
        raise WebhookURLError(f"{host or 'That address'} is not a {'Slack' if kind == 'slack' else 'Teams'} "
                              f"webhook address, so ShipMatch won't send to it. {HELP[kind]}")
    if len(parts.path.strip("/")) < 4:
        raise WebhookURLError(f"The address is missing the webhook part after the host name. {HELP[kind]}")
    return url
