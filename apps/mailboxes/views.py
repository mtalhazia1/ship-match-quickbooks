"""Settings > Email intake: the forwarding address, connected mailboxes and recent emails (admins only)."""
from __future__ import annotations

import hmac
import logging
import secrets
import time
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.core.permissions import require
from apps.core.utils import audit, current_org, orgs_for_user, use_org
from apps.documents.services import gmail as gmail_api

from .forms import GmailForm, ImapForm, InboundForm, MicrosoftForm
from .labels import IMAP_HOSTS
from .models import Mailbox, MailboxMessage
from .services import gmail, imap, inbound, microsoft
from .services.errors import MailboxAuthError, MailboxError
from .services.polling import poll_mailbox, summary

log = logging.getLogger(__name__)
OAUTH_SESSION_KEY = "ms_oauth"
OAUTH_MAX_AGE_SECONDS = 15 * 60
RECENT_EMAILS = 30


def _manage_org(request):
    org = current_org(request)
    require(request.user, org, "manage")
    return org


def _mailbox_for(request, pk) -> Mailbox:
    mailbox = get_object_or_404(Mailbox.objects.filter(organization__in=orgs_for_user(request.user))
                                .select_related("organization"), pk=pk)
    use_org(request, mailbox.organization)
    require(request.user, mailbox.organization, "manage")
    return mailbox


def _back(request, default: str) -> str:
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return default


# ---------------------------------------------------------------- overview


@login_required
def index(request):
    org = _manage_org(request)
    forwarding = inbound.ensure_inbound(org, request.user)
    gmail.sync(org)
    mailboxes = list(Mailbox.objects.filter(organization=org).exclude(kind=Mailbox.Kind.INBOUND)
                     .order_by("kind", "display_name", "id"))
    recent = (MailboxMessage.objects.filter(organization=org).select_related("email", "mailbox")
              .prefetch_related("attachments__document")[:RECENT_EMAILS])
    return render(request, "mailboxes/settings.html", {
        "forwarding": forwarding,
        "mailboxes": mailboxes,
        "gmail_mailbox": next((m for m in mailboxes if m.kind == Mailbox.Kind.GMAIL), None),
        "recent": recent,
        "domain_configured": bool(settings.INBOUND_EMAIL_DOMAIN),
        "ms_configured": microsoft.configured(),
        "gmail_label": settings.GMAIL_LABEL,
        "gmail_credentials_present": _gmail_credentials_present(),
        "poll_minutes": settings.MAILBOX_POLL_MINUTES,
        "webhooks": {
            "postmark": request.build_absolute_uri(reverse("inbound:postmark")),
            "mailgun": request.build_absolute_uri(reverse("inbound:mailgun")),
        },
        "ms_redirect_uri": settings.MS_REDIRECT_URI,
        "postmark_configured": bool(settings.POSTMARK_INBOUND_USER and settings.POSTMARK_INBOUND_PASSWORD),
        "mailgun_configured": bool(settings.MAILGUN_SIGNING_KEY),
    })


def _gmail_credentials_present() -> bool:
    from pathlib import Path

    try:
        return Path(settings.GMAIL_CREDENTIALS_FILE).exists()
    except OSError:
        return False


@login_required
@require_POST
def regenerate_address(request):
    org = _manage_org(request)
    forwarding = inbound.ensure_inbound(org, request.user)
    old = forwarding.inbound_address or forwarding.inbound_local
    inbound.regenerate(forwarding)
    audit(org, "inbound_address.regenerated", forwarding, actor=request.user, old=old,
          new=forwarding.inbound_address or forwarding.inbound_local)
    messages.success(request, "New forwarding address created. The old address no longer accepts email, so update "
                              "your forwarding rules with the new one.")
    return redirect("mailboxes:index")


# ---------------------------------------------------------------- add and edit


def _apply_imap(mailbox: Mailbox, data: dict) -> list[str]:
    """Copy form values onto the mailbox; returns the names of connection fields that changed."""
    changed = []
    for name in ("host", "port", "security", "username", "folder"):
        if getattr(mailbox, name) != data[name]:
            changed.append(name)
            setattr(mailbox, name, data[name])
    if data.get("password"):
        if data["password"] != mailbox.password:
            changed.append("password")
        mailbox.password = data["password"]
    mailbox.mark_seen = data["mark_seen"]
    mailbox.allowed_senders = data["allowed_senders"]
    mailbox.address = data["username"] if "@" in data["username"] else mailbox.address
    mailbox.display_name = data["display_name"] or mailbox.display_name or data["username"]
    return changed


def _test(request, mailbox: Mailbox) -> bool:
    """Run the connection test, record the result on the mailbox and flash it. True when it worked."""
    testers = {Mailbox.Kind.IMAP: imap.test_connection, Mailbox.Kind.MICROSOFT: microsoft.test_connection,
               Mailbox.Kind.GMAIL: gmail.test_connection}
    try:
        text = testers[mailbox.kind](mailbox)
    except MailboxAuthError as e:
        Mailbox.objects.filter(pk=mailbox.pk).update(needs_reconnect=True, last_error=str(e)[:1000])
        messages.error(request, f"Connection test failed: {e}")
        ok = False
    except MailboxError as e:
        Mailbox.objects.filter(pk=mailbox.pk).update(last_error=str(e)[:1000])
        messages.error(request, f"Connection test failed: {e}")
        ok = False
    except Exception as e:
        log.exception("Connection test for mailbox %s failed", mailbox.pk)
        messages.error(request, f"Connection test failed with an unexpected problem ({type(e).__name__}). "
                                "Check the settings and try again.")
        ok = False
    else:
        Mailbox.objects.filter(pk=mailbox.pk).update(needs_reconnect=False, last_error="")
        messages.success(request, text)
        ok = True
    mailbox.refresh_from_db()
    audit(mailbox.organization, "mailbox.tested", mailbox, actor=request.user, name=mailbox.label, ok=ok)
    return ok


def _host_is_usable(form: ImapForm) -> bool:
    """Refuse a server ShipMatch would never connect to (private network, name that doesn't exist) before
    anything is saved, so a wrong host leaves no half-made mailbox holding the password. A wrong password or
    username is different: that mailbox is kept so the person can correct it without retyping everything."""
    try:
        imap.check_host(form.cleaned_data["host"])
    except imap.ImapError as e:
        form.add_error("host", str(e))
        return False
    return True


@login_required
def imap_new(request):
    org = _manage_org(request)
    form = ImapForm(request.POST or None, require_password=True)
    if request.method == "POST" and form.is_valid() and _host_is_usable(form):
        mailbox = Mailbox(organization=org, kind=Mailbox.Kind.IMAP, created_by=request.user)
        _apply_imap(mailbox, form.cleaned_data)
        mailbox.folder_name = mailbox.folder
        mailbox.save()
        audit(org, "mailbox.connected", mailbox, actor=request.user, name=mailbox.label, kind=mailbox.kind,
              host=mailbox.host)
        if _test(request, mailbox):
            messages.info(request, f"ShipMatch checks this mailbox every {settings.MAILBOX_POLL_MINUTES} minutes. "
                                   "Use Check now to import waiting emails straight away.")
            return redirect("mailboxes:index")
        return redirect("mailboxes:edit", pk=mailbox.pk)
    return render(request, "mailboxes/edit.html", {"form": form, "mailbox": None, "kind": "imap",
                                                   "imap_hosts": IMAP_HOSTS})


def _form_for(mailbox: Mailbox, data=None):
    if mailbox.kind == Mailbox.Kind.IMAP:
        initial = {k: getattr(mailbox, k) for k in ("display_name", "host", "port", "security", "username", "folder",
                                                    "mark_seen", "allowed_senders")}
        return ImapForm(data, initial=initial, require_password=not mailbox.password)
    if mailbox.kind == Mailbox.Kind.MICROSOFT:
        initial = {k: getattr(mailbox, k) for k in ("display_name", "folder", "after_import", "processed_folder",
                                                    "allowed_senders")}
        return MicrosoftForm(data, initial=initial, mailbox=mailbox)
    if mailbox.kind == Mailbox.Kind.GMAIL:
        return GmailForm(data, initial={"display_name": mailbox.display_name, "folder": mailbox.folder,
                                        "allowed_senders": mailbox.allowed_senders})
    return InboundForm(data, initial={"allowed_senders": mailbox.allowed_senders})


@login_required
def edit(request, pk):
    mailbox = _mailbox_for(request, pk)
    org = mailbox.organization
    form = _form_for(mailbox, request.POST or None)
    host_changed = mailbox.kind == Mailbox.Kind.IMAP and form.is_bound and form.is_valid() \
        and form.cleaned_data["host"] != mailbox.host
    if request.method == "POST" and form.is_valid() and (not host_changed or _host_is_usable(form)):
        data = form.cleaned_data
        changed, retest = [], False
        if mailbox.kind == Mailbox.Kind.IMAP:
            changed = _apply_imap(mailbox, data)
            if "folder" in changed:
                mailbox.cursor, mailbox.cursor_validity, mailbox.folder_name = "", "", mailbox.folder
            retest = bool(changed)
            if retest:
                mailbox.needs_reconnect, mailbox.last_error = False, ""
        elif mailbox.kind == Mailbox.Kind.MICROSOFT:
            if data["folder"] != mailbox.folder:
                changed.append("folder")
                mailbox.cursor = (timezone.now() - timedelta(days=microsoft.FIRST_CHECK_DAYS)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
            mailbox.folder, mailbox.folder_name = data["folder"], form.names.get(data["folder"], "")
            mailbox.after_import = data["after_import"]
            mailbox.processed_folder = data["processed_folder"] or ""
            mailbox.processed_folder_name = form.names.get(mailbox.processed_folder, "")
            mailbox.display_name = data["display_name"] or mailbox.display_name
            mailbox.allowed_senders = data["allowed_senders"]
        elif mailbox.kind == Mailbox.Kind.GMAIL:
            if data["folder"] != mailbox.folder:
                changed.append("folder")
                mailbox.cursor = ""
            mailbox.folder = mailbox.folder_name = data["folder"]
            mailbox.display_name = data["display_name"] or mailbox.display_name
            mailbox.allowed_senders = data["allowed_senders"]
        else:
            mailbox.allowed_senders = data["allowed_senders"]
        mailbox.save()
        audit(org, "mailbox.updated", mailbox, actor=request.user, name=mailbox.label, changed=changed,
              allowed_senders=len(mailbox.sender_rules()))
        if retest:
            if not _test(request, mailbox):
                return redirect("mailboxes:edit", pk=mailbox.pk)
        else:
            messages.success(request, f"Saved the settings of {mailbox.label}.")
        return redirect("mailboxes:index")
    return render(request, "mailboxes/edit.html", {"form": form, "mailbox": mailbox, "kind": mailbox.kind,
                                                   "imap_hosts": IMAP_HOSTS})


# ---------------------------------------------------------------- actions


@login_required
@require_POST
def test(request, pk):
    mailbox = _mailbox_for(request, pk)
    if not mailbox.is_polled:
        messages.info(request, "Forwarding addresses don't need a connection test. Send an email to it and it shows "
                               "under Recent emails.")
    else:
        _test(request, mailbox)
    return redirect(_back(request, reverse("mailboxes:index")))


@login_required
@require_POST
def check(request, pk):
    mailbox = _mailbox_for(request, pk)
    if not mailbox.is_polled:
        messages.info(request, "Forwarding addresses receive email as it arrives; there is nothing to check.")
        return redirect(_back(request, reverse("mailboxes:index")))
    if not mailbox.enabled:
        messages.error(request, f"{mailbox.label} is paused. Turn it on first.")
        return redirect(_back(request, reverse("mailboxes:index")))
    stats = poll_mailbox(mailbox)
    audit(mailbox.organization, "mailbox.checked", mailbox, actor=request.user, name=mailbox.label,
          status=stats.get("status"), emails=stats.get("emails", 0), documents=stats.get("documents", 0))
    level = messages.success if stats.get("status") == "ok" and not stats.get("warning") else (
        messages.info if stats.get("status") == "busy" else messages.error if stats.get("status") != "ok"
        else messages.warning)
    level(request, summary(stats))
    return redirect(_back(request, reverse("mailboxes:index")))


@login_required
@require_POST
def toggle(request, pk):
    mailbox = _mailbox_for(request, pk)
    mailbox.enabled = not mailbox.enabled
    mailbox.save(update_fields=["enabled", "updated_at"])
    audit(mailbox.organization, "mailbox.enabled" if mailbox.enabled else "mailbox.disabled", mailbox,
          actor=request.user, name=mailbox.label)
    if mailbox.enabled:
        messages.success(request, f"{mailbox.label} is on again.")
    elif mailbox.kind == Mailbox.Kind.INBOUND:
        messages.info(request, "The forwarding address is paused. Email sent to it is refused until you turn it on.")
    else:
        messages.info(request, f"{mailbox.label} is paused. ShipMatch won't check it until you turn it on.")
    return redirect(_back(request, reverse("mailboxes:index")))


@login_required
@require_POST
def remove(request, pk):
    mailbox = _mailbox_for(request, pk)
    if mailbox.kind == Mailbox.Kind.INBOUND:
        messages.error(request, "The forwarding address can't be removed. Pause it, or create a new address instead.")
        return redirect("mailboxes:index")
    name, org = mailbox.label, mailbox.organization
    if mailbox.kind == Mailbox.Kind.GMAIL:
        try:
            gmail_api.token_path(org).unlink(missing_ok=True)
        except OSError:
            log.warning("Couldn't delete the Gmail token for %s", org.slug, exc_info=True)
    audit(org, "mailbox.removed", mailbox, actor=request.user, name=name, kind=mailbox.kind)
    mailbox.delete()
    messages.info(request, f"Removed {name}. Emails and documents it already brought in are kept.")
    return redirect("mailboxes:index")


@login_required
@require_POST
def refresh_folders(request, pk):
    mailbox = _mailbox_for(request, pk)
    if mailbox.kind != Mailbox.Kind.MICROSOFT:
        return redirect("mailboxes:edit", pk=mailbox.pk)
    try:
        folders = microsoft.refresh_folders(mailbox)
        messages.success(request, f"Loaded {len(folders)} folders from Microsoft 365.")
    except MailboxError as e:
        messages.error(request, f"Couldn't load the folders: {e}")
    return redirect("mailboxes:edit", pk=mailbox.pk)


# ---------------------------------------------------------------- Microsoft 365 OAuth


@login_required
def microsoft_connect(request):
    org = _manage_org(request)
    if not microsoft.configured():
        messages.error(request, "Microsoft 365 isn't set up on this server yet. Add MS_CLIENT_ID, MS_CLIENT_SECRET and "
                                "MS_REDIRECT_URI to the .env file (from your Azure app registration), then restart.")
        return redirect("mailboxes:index")
    existing = None
    if request.GET.get("mailbox"):
        if not request.GET["mailbox"].isdigit():
            return redirect("mailboxes:index")
        existing = get_object_or_404(Mailbox, pk=request.GET["mailbox"], organization=org, kind=Mailbox.Kind.MICROSOFT)
    verifier, challenge = microsoft.new_pkce()
    state = secrets.token_urlsafe(32)
    request.session[OAUTH_SESSION_KEY] = {"state": state, "org_id": org.pk, "verifier": verifier,
                                          "mailbox_id": existing.pk if existing else None, "at": int(time.time())}
    return redirect(microsoft.authorize_url(state, challenge, login_hint=existing.address if existing else ""))


MS_ERRORS = {
    "access_denied": "You (or your Microsoft 365 admin) declined access, so nothing was connected.",
    "consent_required": "Your Microsoft 365 admin has to approve ShipMatch before mailboxes can be connected.",
    "interaction_required": "Microsoft needs you to sign in again. Start again from Email intake.",
}


@login_required
def microsoft_callback(request):
    saved = request.session.pop(OAUTH_SESSION_KEY, None)
    state = request.GET.get("state", "")
    if (not saved or not state or not hmac.compare_digest(state, saved.get("state", ""))
            or time.time() - saved.get("at", 0) > OAUTH_MAX_AGE_SECONDS):
        messages.error(request, "The Microsoft sign-in link expired or was opened twice. Start again from Email intake.")
        return redirect("mailboxes:index")
    org = get_object_or_404(orgs_for_user(request.user), pk=saved["org_id"])
    use_org(request, org)
    require(request.user, org, "manage")
    if request.GET.get("error"):
        error = request.GET["error"]
        detail = (request.GET.get("error_description") or "").split("\r\n")[0].split("\n")[0][:200]
        messages.error(request, MS_ERRORS.get(error, f"Microsoft didn't connect the mailbox ({error}). {detail}".strip()))
        return redirect("mailboxes:index")
    if not request.GET.get("code"):
        messages.error(request, "Microsoft didn't send a sign-in code. Start again from Email intake.")
        return redirect("mailboxes:index")
    existing = None
    if saved.get("mailbox_id"):
        existing = Mailbox.objects.filter(pk=saved["mailbox_id"], organization=org, kind=Mailbox.Kind.MICROSOFT).first()
    try:
        mailbox, created = microsoft.connect_mailbox(org, request.GET["code"], saved["verifier"], actor=request.user,
                                                     existing=existing)
    except MailboxError as e:
        messages.error(request, f"Couldn't connect the mailbox: {e}")
        return redirect("mailboxes:index")
    audit(org, "mailbox.connected", mailbox, actor=request.user, name=mailbox.label, kind=mailbox.kind,
          address=mailbox.address, reconnected=not created)
    if created:
        messages.success(request, f"Connected {mailbox.address}. Choose the folder ShipMatch should read, then save.")
        return redirect("mailboxes:edit", pk=mailbox.pk)
    messages.success(request, f"Reconnected {mailbox.address}. ShipMatch carries on where it stopped.")
    return redirect("mailboxes:index")
