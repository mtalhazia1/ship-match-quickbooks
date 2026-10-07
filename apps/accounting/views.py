"""Accounting connections (QuickBooks Online or Xero), managed by organization admins, and payment checks.

An organization posts to one system at a time: connecting one disconnects the other (only once the new one
is fully connected), so bills can never go to both.
"""
import secrets
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounting.models import PaymentSync, QBOConnection, VendorMapping, XeroConnection
from apps.accounting.services import quickbooks, xero
from apps.accounting.services.providers import active_connection, provider_for
from apps.core.permissions import require
from apps.core.utils import audit, current_org, orgs_for_user, use_org
from apps.shipments.models import Shipment

XERO_SESSION_MAX_AGE = 15 * 60   # a Xero sign-in must come back within 15 minutes


def _org(request, org_id):
    org = get_object_or_404(orgs_for_user(request.user), pk=org_id)
    use_org(request, org)
    require(request.user, org, "manage")
    return org


def _demo_blocked() -> bool:
    from apps.demo.mail import outbound_blocked

    return outbound_blocked()


# --------------------------------------------------------------------------- QuickBooks


@login_required
def connect(request, org_id):
    org = _org(request, org_id)
    if not (settings.QBO_CLIENT_ID and settings.QBO_CLIENT_SECRET and settings.QBO_REDIRECT_URI):
        messages.error(request, "QuickBooks isn't set up on this server yet. Add QBO_CLIENT_ID, QBO_CLIENT_SECRET "
                                "and QBO_REDIRECT_URI to the .env file (from your Intuit developer app), then restart.")
        return redirect("accounting:settings", org_id=org.pk)
    state = secrets.token_urlsafe(24)
    request.session["qbo_oauth"] = {"state": state, "org_id": org.pk}
    return redirect(quickbooks.authorize_url(state))


@login_required
def callback(request):
    saved = request.session.pop("qbo_oauth", None)
    if not saved or request.GET.get("state") != saved["state"]:
        messages.error(request, "The QuickBooks connection link expired or was opened twice. Start again from Settings.")
        return redirect("core:settings")
    org = _org(request, saved["org_id"])
    if request.GET.get("error"):
        messages.error(request, f"QuickBooks connection was cancelled ({request.GET['error']}).")
        return redirect("accounting:settings", org_id=org.pk)
    if not request.GET.get("code") or not request.GET.get("realmId"):
        messages.error(request, "QuickBooks didn't return a company. Start again and choose a company.")
        return redirect("accounting:settings", org_id=org.pk)
    try:
        conn = quickbooks.exchange_code(org, request.GET["code"], request.GET["realmId"])
        quickbooks.QBOClient(conn).sync_company()
    except quickbooks.QBOError as e:
        messages.error(request, f"Couldn't finish connecting QuickBooks: {e}")
        return redirect("accounting:settings", org_id=org.pk)
    audit(org, "qbo.connected", conn, actor=request.user, realm_id=conn.realm_id, company=conn.company_name)
    replaced = _drop_xero(org, request.user)
    messages.success(request, f"Connected to {conn.company_name or 'QuickBooks'}. "
                              + ("Bills now post to QuickBooks instead of Xero. " if replaced else "")
                              + "Choose the default expense account below.")
    return redirect("accounting:settings", org_id=org.pk)


@login_required
def reconnect(request):
    """Stable 'Reconnect URL' for the Intuit developer portal: opens this organization's accounting page."""
    org = current_org(request)
    return redirect("accounting:settings", org_id=org.pk)


@login_required
def qbo_settings(request, org_id):
    """Settings > Accounting: QuickBooks and Xero, whichever is connected, plus vendor account rules."""
    org = _org(request, org_id)
    conn = QBOConnection.objects.filter(organization=org).first()
    xero_conn = XeroConnection.objects.filter(organization=org).first()
    if request.method == "POST":   # the QuickBooks default expense account
        if not conn:
            messages.error(request, "QuickBooks isn't connected, so there is no default account to save.")
            return redirect("accounting:settings", org_id=org.pk)
        chosen = request.POST.get("default_expense_account_id", "").strip()
        if chosen:
            if not chosen.isdigit() or len(chosen) > 40:
                messages.error(request, "Choose one of the accounts in the list.")
                return redirect("accounting:settings", org_id=org.pk)
            active_now = active_connection(org)
            if active_now is not None and active_now.pk == conn.pk and active_now.system == conn.system:
                try:
                    known = {str(a["id"]) for a in provider_for(org).expense_accounts()}
                except Exception:   # the account list can't be read right now, so the choice can't be checked
                    messages.error(request, "Couldn't check the account with QuickBooks just now, so nothing was "
                                            "saved. Try again in a moment.")
                    return redirect("accounting:settings", org_id=org.pk)
                if chosen not in known:
                    messages.error(request, "That account isn't one of your QuickBooks expense accounts. "
                                            "Choose one from the list.")
                    return redirect("accounting:settings", org_id=org.pk)
        conn.default_expense_account_id = chosen
        conn.save(update_fields=["default_expense_account_id"])
        audit(org, "qbo.default_account", conn, actor=request.user, account=conn.default_expense_account_id)
        messages.success(request, "Default expense account saved.")
        return redirect("accounting:settings", org_id=org.pk)
    active = active_connection(org)
    accounts, error = [], ""
    if active and not active.needs_reconnect:
        try:
            provider = provider_for(org)
            if not active.company_name or not active.home_currency:
                if active.system == "xero":
                    provider.client.sync_organisation()
                else:
                    provider.client.sync_company()
            accounts = provider.expense_accounts()
        except Exception as e:  # network or token problems are shown, not raised
            error = str(e) or type(e).__name__
            active.refresh_from_db()
    return render(request, "settings/accounting.html", {
        "conn": conn, "xero_conn": xero_conn, "active": active, "accounts": accounts, "error": error,
        "environment": settings.QBO_ENVIRONMENT,
        "configured": bool(settings.QBO_CLIENT_ID and settings.QBO_CLIENT_SECRET),
        "redirect_uri": settings.QBO_REDIRECT_URI,
        "reconnect_url": request.build_absolute_uri("/accounting/qbo/reconnect/"),
        "xero_configured": xero.configured(), "xero_pkce": xero.uses_pkce(),
        "xero_redirect_uri": settings.XERO_REDIRECT_URI, "xero_scopes": settings.XERO_SCOPES,
        "bill_statuses": XeroConnection.BillStatus.choices,
        "mappings": VendorMapping.objects.filter(organization=org).order_by("display_name"),
        "sync": PaymentSync.objects.filter(organization=org).first(), "sync_hours": settings.PAYMENT_SYNC_HOURS,
        "demo_blocked": _demo_blocked(),
    })


@login_required
@require_POST
def disconnect(request, org_id):
    org = _org(request, org_id)
    conn = QBOConnection.objects.filter(organization=org).first()
    revoked = bool(conn) and quickbooks.revoke(conn)
    QBOConnection.objects.filter(organization=org).delete()
    audit(org, "qbo.disconnected", org, actor=request.user, revoked_at_intuit=revoked)
    messages.info(request, "QuickBooks disconnected. Approved shipments can't be posted until you connect again.")
    return redirect("accounting:settings", org_id=org.pk)


# --------------------------------------------------------------------------- Xero


def _drop_xero(org, user) -> bool:
    """Connecting QuickBooks replaces Xero as the system bills post to."""
    conn = XeroConnection.objects.filter(organization=org).first()
    if conn is None:
        return False
    revoked = xero.revoke(conn)
    company = conn.tenant_name
    conn.delete()
    audit(org, "xero.disconnected", org, actor=user, revoked_at_xero=revoked, company=company, replaced_by="QuickBooks")
    return bool(company)


def _drop_quickbooks(org, user) -> bool:
    """Connecting Xero replaces QuickBooks as the system bills post to."""
    conn = QBOConnection.objects.filter(organization=org).first()
    if conn is None:
        return False
    revoked = quickbooks.revoke(conn)
    conn.delete()
    audit(org, "qbo.disconnected", org, actor=user, revoked_at_intuit=revoked, replaced_by="Xero")
    return True


@login_required
def xero_connect(request, org_id):
    org = _org(request, org_id)
    if _demo_blocked():
        messages.warning(request, xero.DEMO_BLOCKED)
        return redirect("accounting:settings", org_id=org.pk)
    if not xero.configured():
        messages.error(request, "Xero isn't set up on this server yet. Add XERO_CLIENT_ID, XERO_CLIENT_SECRET and "
                                "XERO_REDIRECT_URI to the .env file (from your app at developer.xero.com), then restart.")
        return redirect("accounting:settings", org_id=org.pk)
    state = secrets.token_urlsafe(24)
    verifier, challenge = xero.new_pkce() if xero.uses_pkce() else ("", None)
    request.session["xero_oauth"] = {"state": state, "org_id": org.pk, "verifier": verifier, "at": int(time.time())}
    return redirect(xero.authorize_url(state, challenge))


@login_required
def xero_callback(request):
    saved = request.session.pop("xero_oauth", None)
    if (not saved or not request.GET.get("state") or request.GET.get("state") != saved.get("state")
            or time.time() - saved.get("at", 0) > XERO_SESSION_MAX_AGE):
        messages.error(request, "The Xero connection link expired or was opened twice. Start again from "
                                "Settings > Accounting.")
        return redirect("core:settings")
    org = _org(request, saved["org_id"])
    back = redirect("accounting:settings", org_id=org.pk)
    error = request.GET.get("error", "")
    if error == "invalid_scope":
        messages.error(request, "Xero refused the permissions ShipMatch asked for. Xero apps created from 2 March "
                                "2026 need the newer scopes: set XERO_SCOPES in .env as the README's Xero section "
                                "shows, restart, and connect again.")
        return back
    if error:
        messages.error(request, "Xero connection was cancelled" + ("" if error == "access_denied" else f" ({error[:60]})")
                       + ". Nothing was changed.")
        return back
    if not request.GET.get("code"):
        messages.error(request, "Xero didn't return a sign-in code. Start again from Settings > Accounting.")
        return back
    try:
        tok = xero.exchange_code(request.GET["code"], saved.get("verifier") or None)
        event = xero.auth_event_id(tok["access_token"])
        tenants = xero.connections(tok["access_token"], event_id=event) if event else []
        tenants = tenants or xero.connections(tok["access_token"])
    except xero.XeroError as e:
        messages.error(request, f"Couldn't finish connecting Xero: {e}")
        return back
    if not tenants:
        messages.error(request, "Xero didn't share any organisation with ShipMatch. Start again and choose the "
                                "organisation to connect.")
        return back
    conn = XeroConnection.objects.filter(organization=org).first() or XeroConnection(organization=org)
    previous = conn.tenant_id
    xero.store_tokens(conn, tok, save=False)
    conn.tenants = tenants
    ids = {t["tenantId"]: t for t in tenants}
    if previous in ids:
        chosen = ids[previous]
    elif len(tenants) == 1:
        chosen = tenants[0]
    else:
        conn.previous_tenant_id = previous or conn.previous_tenant_id
        conn.tenant_id = ""   # nothing posts until the admin picks one
        conn.save()
        request.session["xero_pick"] = {"org_id": org.pk, "previous": previous}
        messages.info(request, "Your Xero sign-in can reach several organisations. Choose the one ShipMatch should "
                               "post bills to.")
        return redirect("accounting:xero_tenant", org_id=org.pk)
    conn.save()
    _choose_tenant(request, org, conn, chosen, previous)
    return back


def _choose_tenant(request, org, conn: XeroConnection, tenant: dict, previous: str) -> None:
    if previous and previous != tenant["tenantId"]:
        # Another Xero organisation: contacts and account codes from the old one mean nothing here.
        VendorMapping.objects.filter(organization=org).update(xero_contact_id="", xero_account_code="",
                                                              xero_account_name="")
        conn.default_account_code, conn.home_currency, conn.currencies, conn.short_code = "", "", [], ""
    conn.tenant_id, conn.connection_id = tenant["tenantId"], tenant.get("id", "")
    conn.previous_tenant_id = ""
    conn.tenant_name = tenant.get("tenantName", "")[:200]
    conn.save()
    try:
        xero.XeroClient(conn).sync_organisation()
    except xero.XeroError as e:
        messages.warning(request, f"Connected, but Xero's organisation settings couldn't be read yet: {e}")
    audit(org, "xero.connected", conn, actor=request.user, tenant_id=conn.tenant_id, company=conn.tenant_name)
    replaced = _drop_quickbooks(org, request.user)
    messages.success(request, f"Connected to {conn.tenant_name or 'Xero'}. "
                              + ("Bills now post to Xero instead of QuickBooks. " if replaced else "")
                              + "Choose the default expense account below.")


@login_required
def xero_tenant(request, org_id):
    """Pick which Xero organisation bills post to, when one sign-in reaches several."""
    org = _org(request, org_id)
    conn = XeroConnection.objects.filter(organization=org).first()
    if conn is None or not conn.tenants:
        messages.error(request, "Connect Xero first.")
        return redirect("accounting:settings", org_id=org.pk)
    if request.method == "POST":
        tenant = next((t for t in conn.tenants if t["tenantId"] == request.POST.get("tenant_id")), None)
        if tenant is None:
            messages.error(request, "Choose one of the Xero organisations listed.")
            return redirect("accounting:xero_tenant", org_id=org.pk)
        pick = request.session.pop("xero_pick", None) or {}
        previous = pick.get("previous", "") if pick.get("org_id") == org.pk else ""
        _choose_tenant(request, org, conn, tenant, previous or conn.previous_tenant_id or conn.tenant_id)
        return redirect("accounting:settings", org_id=org.pk)
    return render(request, "accounting/xero_tenant.html", {"xero_conn": conn})


@login_required
@require_POST
def xero_settings(request, org_id):
    org = _org(request, org_id)
    conn = XeroConnection.objects.filter(organization=org).exclude(tenant_id="").first()
    if conn is None:
        messages.error(request, "Xero isn't connected, so there is nothing to save.")
        return redirect("accounting:settings", org_id=org.pk)
    code = request.POST.get("default_account_code", "").strip()[:20]
    status = request.POST.get("bill_status", conn.bill_status)
    if status not in XeroConnection.BillStatus.values:
        messages.error(request, "Choose how bills are created in Xero.")
        return redirect("accounting:settings", org_id=org.pk)
    before = {"account": conn.default_account_code, "bill_status": conn.bill_status}
    conn.default_account_code, conn.bill_status = code, status
    conn.save(update_fields=["default_account_code", "bill_status"])
    after = {"account": conn.default_account_code, "bill_status": conn.bill_status}
    audit(org, "xero.settings_updated", conn, actor=request.user, account=code or "none",
          bill_status=conn.get_bill_status_display(), changes={k: [before[k], after[k]] for k in before if before[k] != after[k]})
    messages.success(request, "Xero settings saved.")
    return redirect("accounting:settings", org_id=org.pk)


@login_required
@require_POST
def xero_disconnect(request, org_id):
    org = _org(request, org_id)
    conn = XeroConnection.objects.filter(organization=org).first()
    revoked = bool(conn) and xero.revoke(conn)
    company = conn.tenant_name if conn else ""
    XeroConnection.objects.filter(organization=org).delete()
    audit(org, "xero.disconnected", org, actor=request.user, revoked_at_xero=revoked, company=company)
    messages.info(request, "Xero disconnected. Approved shipments can't be posted until you connect an accounting "
                           "system again. Bills already in Xero stay there.")
    return redirect("accounting:settings", org_id=org.pk)


# --------------------------------------------------------------------------- payments


def _back(request, default: str) -> str:
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return default


@login_required
@require_POST
def check_payments(request):
    """"Check payments now": read payment status from the accounting system (whole organization or one shipment)."""
    from apps.accounting.services import payments
    from apps.accounting.tasks import sync_org_payments

    org = current_org(request)
    require(request.user, org, "post")
    shipment = None
    if request.POST.get("shipment", "").isdigit():
        shipment = get_object_or_404(Shipment, organization=org, pk=int(request.POST["shipment"]))
    back = _back(request, reverse("review:shipment", args=[shipment.pk]) if shipment else reverse("core:dashboard"))
    conn = active_connection(org)
    if conn is None:
        messages.error(request, "Connect QuickBooks or Xero in Settings > Accounting first, then check payments.")
        return redirect(back)
    if conn.needs_reconnect:
        messages.error(request, f"{conn.system_name} needs to be connected again before payments can be checked. "
                                "An admin can do it in Settings > Accounting.")
        return redirect(back)
    if not settings.CELERY_TASK_ALWAYS_EAGER:
        sync_org_payments.delay(org.pk, automatic=False, user_id=request.user.pk,
                                shipment_id=shipment.pk if shipment else None)
        audit(org, "accounting.payments_checked", shipment or org, actor=request.user, system=conn.system_name,
              queued=True)
        messages.info(request, f"Checking payments in {conn.system_name}. Refresh in a minute to see the result.")
        return redirect(back)
    summary = payments.sync(org, actor=request.user, shipment=shipment)
    level, text = _sync_message(summary, conn.system_name)
    if not summary.get("skipped"):
        audit(org, "accounting.payments_checked", shipment or org, actor=request.user, system=conn.system_name,
              checked=summary["checked"], changed=summary["changed"], error=summary["error"][:200])
    messages.add_message(request, level, text)
    return redirect(back)


def _sync_message(summary: dict, system: str) -> tuple[int, str]:
    skipped, error = summary.get("skipped"), summary.get("error")
    if skipped == "just_checked":
        return messages.INFO, "Payments were checked less than a minute ago. The statuses shown are current."
    if skipped == "running":
        return messages.INFO, "A payment check is already running. Refresh in a minute to see the result."
    if error:
        return messages.ERROR, f"Couldn't check payments in {system}: {error}"
    if not summary["checked"]:
        return messages.INFO, f"Nothing to check: there are no unpaid bills posted to {system}."
    n, changed = summary["checked"], summary["changed"]
    return messages.SUCCESS, (f"Checked {n} bill{'s' if n != 1 else ''} in {system}. "
                              + (f"{changed} changed status." if changed else "No changes since the last check."))
