"""Plain-language text for email intake: audit log lines and setup help."""

AUDIT_ACTIONS = {
    "email.received": "received an email from {sender}",
    "email.blocked": "ignored an email from {sender} because the sender isn't on the allowed list",
    "email.failed": "could not process an email from {sender}",
    "mailbox.connected": "connected the mailbox {name}",
    "mailbox.updated": "changed the settings of the mailbox {name}",
    "mailbox.removed": "removed the mailbox {name}",
    "mailbox.enabled": "turned on the mailbox {name}",
    "mailbox.disabled": "paused the mailbox {name}",
    "mailbox.checked": "checked the mailbox {name} for new email",
    "mailbox.tested": "tested the connection to {name}",
    "mailbox.needs_reconnect": "lost the connection to the mailbox {name}",
    "mailbox.cursor_reset": "started reading {name} from the start again because the mail server renumbered its emails",
    "inbound_address.regenerated": "created a new forwarding address",
}

# Common IMAP servers, offered as suggestions in the host field.
IMAP_HOSTS = [
    ("imap.gmail.com", "Gmail and Google Workspace (needs an app password)"),
    ("imap.mail.yahoo.com", "Yahoo Mail (needs an app password)"),
    ("imap.mail.me.com", "iCloud Mail (needs an app-specific password)"),
    ("imap.zoho.com", "Zoho Mail"),
    ("imap.fastmail.com", "Fastmail"),
    ("imap.secureserver.net", "GoDaddy Workspace Email"),
    ("imap.ionos.com", "IONOS"),
]
