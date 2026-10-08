"""IMAP mailboxes: any provider that offers IMAP with a username and (app) password.

Reads by UID: the cursor is the last UID imported together with the folder's UIDVALIDITY. When the server
changes UIDVALIDITY (the folder was rebuilt or restored), the cursor is reset and the last week is read
again; Message-ID de-duplication stops anything being imported twice. Messages are fetched with
BODY.PEEK[] so reading never marks them as read; with "mark as read" on, imported ones get \\Seen.
"""
from __future__ import annotations

import imaplib
import ipaddress
import logging
import re
import socket
import ssl
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.core.utils import audit
from apps.mailboxes.models import Mailbox

from . import intake
from .errors import MailboxAuthError, MailboxError
from .mime import imap_quote, imap_utf7_decode, parse_message

log = logging.getLogger(__name__)

IMAP4_SSL = imaplib.IMAP4_SSL     # replaced by a fake in tests
IMAP4 = imaplib.IMAP4
TIMEOUT_SECONDS = 30
FIRST_CHECK_DAYS = 7
MAX_MESSAGES_PER_CHECK = 100
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$')


class ImapError(MailboxError):
    pass


class ImapAuthError(MailboxAuthError, ImapError):
    pass


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def check_host(host: str) -> None:
    """Refuse hosts that resolve to private, loopback or link-local addresses (protects the server's network)."""
    if settings.MAILBOX_ALLOW_PRIVATE_HOSTS:
        return
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ImapError(f"Couldn't find the server “{host}”. Check the spelling of the host name.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        # Only public internet addresses: is_global also excludes shared carrier space (100.64.0.0/10),
        # benchmarking and documentation ranges, which is_private misses.
        if not ip.is_global or ip.is_multicast:
            raise ImapError(f"“{host}” is an address on a private network, which ShipMatch doesn't connect to. "
                            "Use the mail provider's public IMAP server.")


def connect(mailbox: Mailbox):
    host, port = (mailbox.host or "").strip(), int(mailbox.port or (993 if mailbox.security == "ssl" else 143))
    if not host or not mailbox.username:
        raise ImapError("This mailbox has no server or username. Edit it and fill them in.")
    check_host(host)
    context = ssl.create_default_context()
    mode = "SSL/TLS" if mailbox.security == Mailbox.Security.SSL else "STARTTLS"
    try:
        if mailbox.security == Mailbox.Security.SSL:
            client = IMAP4_SSL(host, port, ssl_context=context, timeout=TIMEOUT_SECONDS)
        else:
            client = IMAP4(host, port, timeout=TIMEOUT_SECONDS)
            client.starttls(ssl_context=context)
    except socket.gaierror:
        raise ImapError(f"Couldn't find the server “{host}”. Check the spelling of the host name.")
    except ssl.SSLCertVerificationError as e:
        raise ImapError(f"The server's security certificate isn't valid for “{host}” ({e.verify_message}). "
                        "Use the host name your mail provider publishes for IMAP.")
    except ssl.SSLError as e:
        raise ImapError(f"The secure connection to {host} failed ({e.reason or e}). Check that port {port} uses "
                        f"{mode}: usually 993 for SSL/TLS and 143 for STARTTLS.")
    except TimeoutError:
        raise ImapError(f"{host} didn't answer on port {port} within {TIMEOUT_SECONDS} seconds. Check the host, the "
                        "port and that the provider allows IMAP.")
    except ConnectionRefusedError:
        raise ImapError(f"{host} refused the connection on port {port}. Check the port (993 for SSL/TLS, 143 for "
                        "STARTTLS) and that IMAP is turned on for this account.")
    except imaplib.IMAP4.error as e:
        raise ImapError(f"{host} doesn't support {mode} on port {port} ({_text(e)}). Try the other security option.")
    except OSError as e:
        raise ImapError(f"Couldn't reach {host} on port {port} ({e.strerror or e}). Check the host and port.")
    try:
        client.login(mailbox.username, mailbox.password or "")
    except imaplib.IMAP4.error as e:
        _logout(client)
        detail = _text(e).strip()[:160]
        raise ImapAuthError(
            f"{host} rejected the username or password{f' ({detail})' if detail else ''}. Gmail, Yahoo and iCloud "
            "need an app password instead of the normal one. Microsoft 365 no longer allows IMAP passwords: use "
            "Connect Microsoft 365 instead.")
    return client


def _logout(client) -> None:
    try:
        client.logout()
    except Exception:   # the connection may already be gone
        pass


def list_folders(client) -> list[str]:
    try:
        typ, data = client.list()
    except imaplib.IMAP4.error:
        return []
    names = []
    for line in data or []:
        if isinstance(line, tuple):
            line = line[0] + b" " + line[1]
        if not isinstance(line, bytes):
            continue
        m = _LIST_RE.match(line.strip())
        if not m or b"\\Noselect" in m.group("flags"):
            continue
        name = m.group("name").strip()
        if name.startswith(b'"') and name.endswith(b'"'):
            name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
        names.append(imap_utf7_decode(name.decode("utf-8", "replace")))
    return names


def select(client, folder: str, readonly: bool) -> tuple[str, int]:
    """Open the folder; returns its UIDVALIDITY and number of emails."""
    folder = folder or "INBOX"
    try:
        typ, data = client.select(imap_quote(folder), readonly=readonly)
    except imaplib.IMAP4.error as e:
        typ, data = "NO", [str(e).encode()]
    if typ != "OK":
        available = [f for f in list_folders(client) if f][:12]
        hint = f" Folders on this account: {', '.join(available)}." if available else ""
        raise ImapError(f"Signed in, but there's no folder named “{folder}”.{hint}")
    try:
        count = int(_text((data or [b"0"])[0] or b"0").strip() or 0)
    except ValueError:
        count = 0
    try:
        _, values = client.response("UIDVALIDITY")
        validity = _text((values or [b""])[0] or "").strip()
    except Exception:
        validity = ""
    return validity, count


def test_connection(mailbox: Mailbox) -> str:
    client = connect(mailbox)
    try:
        _, count = select(client, mailbox.folder or "INBOX", readonly=True)
    finally:
        _logout(client)
    return (f"Connected to {mailbox.host} as {mailbox.username}. The folder “{mailbox.folder or 'INBOX'}” has "
            f"{count} email{'s' if count != 1 else ''}.")


def _imap_date(d) -> str:
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def _search(client, *criteria) -> list[int]:
    typ, data = client.uid("SEARCH", None, *criteria)
    if typ != "OK":
        raise ImapError(f"The server refused to search the folder ({_text((data or [b''])[0])[:120]}).")
    return sorted({int(x) for x in b" ".join(d for d in (data or []) if d).split() if x.isdigit()})


def _fetch(client, uid: int) -> bytes | None:
    typ, data = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if typ != "OK":
        raise ImapError(f"The server refused to send email UID {uid}.")
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and b"BODY[" in item[0].upper():
            return item[1]
    return None   # deleted between the search and the fetch


def poll(mailbox: Mailbox, process: str = "async") -> dict:
    stats = {"emails": 0, "documents": 0, "already": 0, "skipped_attachments": 0, "without_attachments": 0,
             "warnings": []}
    client = connect(mailbox)
    try:
        validity, _ = select(client, mailbox.folder or "INBOX", readonly=not mailbox.mark_seen)
        last_uid = int(mailbox.cursor) if mailbox.cursor.isdigit() else None
        if last_uid is not None and validity != mailbox.cursor_validity:
            audit(mailbox.organization, "mailbox.cursor_reset", mailbox, name=mailbox.label,
                  old=mailbox.cursor_validity, new=validity)
            stats["reset"] = True
            last_uid = None
            mailbox.cursor, mailbox.cursor_validity = "", validity
            Mailbox.objects.filter(pk=mailbox.pk).update(cursor="", cursor_validity=validity)
        if last_uid is None:
            uids = _search(client, "SINCE", _imap_date(timezone.now() - timedelta(days=FIRST_CHECK_DAYS)))
        else:
            # "n:*" also returns the newest message when n is past the end, so filter again.
            uids = [u for u in _search(client, "UID", f"{last_uid + 1}:*") if u > last_uid]
        if len(uids) > MAX_MESSAGES_PER_CHECK:
            uids, stats["more"] = uids[:MAX_MESSAGES_PER_CHECK], True
        for uid in uids:
            raw = _fetch(client, uid)
            if raw is not None:
                incoming = parse_message(raw, "imap", mailbox.host, mailbox.username, mailbox.folder, validity, uid)
                incoming.provider_ref = f"UID {uid}"
                incoming.recipient = mailbox.address or mailbox.username
                if not intake.has_documents(incoming):
                    stats["without_attachments"] += 1
                elif intake.already_received(mailbox.organization, incoming.message_id):
                    stats["already"] += 1
                else:
                    result = intake.receive_or_give_up(mailbox, incoming, process=process)
                    if result.created:
                        stats["emails"] += 1
                        stats["documents"] += len(result.new_documents)
                        stats["skipped_attachments"] += result.skipped
                        if mailbox.mark_seen:
                            client.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
            mailbox.cursor, mailbox.cursor_validity = str(uid), validity
            Mailbox.objects.filter(pk=mailbox.pk).update(cursor=mailbox.cursor, cursor_validity=validity)
    except (imaplib.IMAP4.abort, OSError) as e:
        raise ImapError(f"The connection to {mailbox.host} dropped while reading email ({_text(e)[:120]}). "
                        "ShipMatch will continue at the next check.")
    finally:
        _logout(client)
    return stats
