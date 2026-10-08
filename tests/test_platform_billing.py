"""Billing: Stripe webhook signatures and idempotency, the subscription state machine, Checkout and the Customer
Portal against a fake Stripe (httpx.MockTransport), usage limits, and that review/approval/posting never stop."""
import json
import time
from datetime import timedelta
from urllib.parse import parse_qsl

import httpx
import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.billing import state, stripe_api, stripe_webhooks
from apps.billing.models import BillingAccount, StripeEvent
from apps.billing.usage import UsageLimitReached, check_intake, usage_for
from apps.core.models import AuditEvent, Membership
from apps.documents.models import Document
from apps.documents.services.ingest import ingest_bytes
from apps.shipments.models import Shipment

SECRET = "whsec_test_secret_1"


@pytest.fixture
def billing(settings):
    settings.BILLING_ENABLED = True
    settings.STRIPE_SECRET_KEY = "sk_test_123"
    settings.STRIPE_WEBHOOK_SECRETS = [SECRET]
    settings.STRIPE_WEBHOOK_TOLERANCE = 300
    settings.BILLING_TRIAL_DOCUMENTS = 5
    settings.BILLING_HARD_LIMIT_PERCENT = 120
    settings.SITE_URL = "https://ap.example.com"
    settings.BILLING_PLANS = {
        "starter": {"name": "Starter", "price_id": "price_starter", "price": "199", "documents": 10, "users": 3,
                    "features": ["exports"]},
        "growth": {"name": "Growth", "price_id": "price_growth", "price": "499", "documents": 100, "users": 10,
                   "features": ["exports", "api", "webhooks"]},
    }
    settings.BILLING_TRIAL_PLAN = "growth"
    return settings


@pytest.fixture
def account(org, billing):
    return BillingAccount.objects.create(organization=org, status=BillingAccount.Status.TRIALING, plan="growth",
                                         trial_ends_at=timezone.now() + timedelta(days=14))


def _sign(payload: bytes, secret=SECRET, ts=None) -> str:
    ts = int(time.time()) if ts is None else ts
    return f"t={ts},v1={stripe_webhooks.compute_signature(secret, ts, payload)}"


def _event(type_, obj, eid=None, created=None):
    return {"id": eid or f"evt_{type_.replace('.', '_')}_{int(time.time() * 1000)}", "object": "event",
            "type": type_, "created": created or int(time.time()), "livemode": False, "data": {"object": obj}}


def _post(client, event, secret=SECRET, ts=None, header=None):
    body = json.dumps(event).encode()
    return client.post(reverse("billing:stripe_webhook"), body, content_type="application/json",
                       HTTP_STRIPE_SIGNATURE=header if header is not None else _sign(body, secret, ts))


def _sub(sub_id="sub_A1b2c3d4", status="active", price="price_starter", customer="cus_ABC123", org=None, **kw):
    now = int(time.time())
    return {"id": sub_id, "object": "subscription", "status": status, "customer": customer,
            "metadata": {"org_id": str(org.pk)} if org else {},
            "items": {"data": [{"price": {"id": price}, "current_period_start": now - 86400,
                                "current_period_end": now + 29 * 86400}]},
            "cancel_at_period_end": False, **kw}


# ---------------------------------------------------------------- signature


def test_signature_valid_wrong_secret_stale_and_multiple_v1():
    body = b'{"id":"evt_1"}'
    now = 1_800_000_000
    good = stripe_webhooks.compute_signature(SECRET, now, body)
    assert stripe_webhooks.verify_signature(body, f"t={now},v1={good}", [SECRET], 300, now=now) == now
    # Several v1 values (Stripe sends one per active secret while rolling) and a v0 are fine.
    header = f"t={now},v1={'0' * 64},v1={good},v0=legacy"
    assert stripe_webhooks.verify_signature(body, header, [SECRET], 300, now=now + 10) == now
    # Several configured secrets (rotation on our side).
    assert stripe_webhooks.verify_signature(body, f"t={now},v1={good}", ["whsec_new", SECRET], 300, now=now)
    with pytest.raises(stripe_webhooks.SignatureError, match="doesn't match"):
        stripe_webhooks.verify_signature(body, f"t={now},v1={good}", ["whsec_other"], 300, now=now)
    with pytest.raises(stripe_webhooks.SignatureError, match="doesn't match"):
        stripe_webhooks.verify_signature(body + b" ", f"t={now},v1={good}", [SECRET], 300, now=now)
    with pytest.raises(stripe_webhooks.SignatureError, match="too old"):
        stripe_webhooks.verify_signature(body, f"t={now},v1={good}", [SECRET], 300, now=now + 301)
    with pytest.raises(stripe_webhooks.SignatureError, match="too old"):
        stripe_webhooks.verify_signature(body, f"t={now},v1={good}", [SECRET], 300, now=now - 600)
    for bad in ("", f"t={now}", f"v1={good}", f"t=abc,v1={good}"):
        with pytest.raises(stripe_webhooks.SignatureError):
            stripe_webhooks.verify_signature(body, bad, [SECRET], 300, now=now)
    with pytest.raises(stripe_webhooks.SignatureError, match="No webhook signing secret"):
        stripe_webhooks.verify_signature(body, f"t={now},v1={good}", [], 300, now=now)


@pytest.mark.django_db
def test_webhook_view_checks_signature_and_processes_each_event_once(client, org, account):
    account.stripe_customer_id = "cus_ABC123"
    account.save()
    event = _event("customer.subscription.created", _sub(org=org), eid="evt_once_1")
    assert _post(client, event, secret="whsec_wrong").status_code == 400
    assert _post(client, event, ts=int(time.time()) - 3600).status_code == 400    # stale: possible replay
    assert _post(client, event, header="").status_code == 400
    assert not StripeEvent.objects.exists()
    r = _post(client, event)
    assert r.status_code == 200
    account.refresh_from_db()
    assert account.status == "active" and account.plan == "starter" and account.stripe_subscription_id == "sub_A1b2c3d4"
    # Stripe resends the same event (a replay with a fresh signature): acknowledged, not applied twice.
    BillingAccount.objects.filter(pk=account.pk).update(status="past_due")
    r = _post(client, event)
    assert r.status_code == 200 and b"Already processed" in r.content
    account.refresh_from_db()
    assert account.status == "past_due"
    assert StripeEvent.objects.filter(event_id="evt_once_1").count() == 1
    assert AuditEvent.objects.filter(organization=org, action="billing.stripe_event").count() == 1


@pytest.mark.django_db
def test_webhook_is_off_when_billing_is_off(client, org, settings):
    settings.BILLING_ENABLED = False
    settings.STRIPE_WEBHOOK_SECRETS = [SECRET]
    assert _post(client, _event("invoice.paid", {})).status_code == 404


@pytest.mark.django_db
def test_failing_handler_answers_500_and_stripe_can_retry(client, org, account, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database hiccup")

    monkeypatch.setitem(stripe_webhooks.HANDLERS, "customer.subscription.updated", boom)
    event = _event("customer.subscription.updated", _sub(org=org), eid="evt_retry_me")
    assert _post(client, event).status_code == 500
    assert not StripeEvent.objects.filter(event_id="evt_retry_me").exists()   # rolled back: the retry is processed
    monkeypatch.undo()
    assert _post(client, event).status_code == 200
    assert StripeEvent.objects.filter(event_id="evt_retry_me").exists()


# ---------------------------------------------------------------- state machine


@pytest.mark.django_db
def test_subscription_state_machine(org, account):
    t0 = timezone.now()
    s = lambda **kw: _sub(org=org, **kw)  # noqa: E731
    assert state.apply_subscription(account, s(status="trialing", trial_end=int((t0 + timedelta(days=9)).timestamp())),
                                    t0) == "trialing to trialing" or account.status == "trialing"
    assert account.has_subscription and account.intake_open
    state.apply_subscription(account, s(status="active"), t0 + timedelta(minutes=1))
    assert account.status == "active"
    state.apply_subscription(account, s(status="past_due"), t0 + timedelta(minutes=2))
    assert account.status == "past_due" and account.intake_open      # Stripe is retrying; nothing stops yet
    state.apply_subscription(account, s(status="active"), t0 + timedelta(minutes=3))
    assert account.status == "active"
    # An older event arriving late changes nothing.
    assert "older" in state.apply_subscription(account, s(status="past_due"), t0 + timedelta(minutes=2, seconds=30))
    assert account.status == "active"
    # Unpaid counts as overdue; canceled is final for that subscription.
    state.apply_subscription(account, s(status="unpaid"), t0 + timedelta(minutes=4))
    assert account.status == "past_due"
    state.apply_subscription(account, s(status="canceled"), t0 + timedelta(minutes=5))
    assert account.status == "canceled" and not account.intake_open
    assert "ended" in state.apply_subscription(account, s(status="active"), t0 + timedelta(minutes=6))
    assert account.status == "canceled"
    # A new subscription brings it back; a late "deleted" for the old one is ignored.
    state.apply_subscription(account, s(sub_id="sub_NEW0001", status="active", price="price_growth"),
                             t0 + timedelta(minutes=7))
    assert account.status == "active" and account.plan == "growth" and account.stripe_subscription_id == "sub_NEW0001"
    assert "not the organization's current" in state.apply_subscription(
        account, s(status="canceled"), t0 + timedelta(minutes=8))
    assert account.status == "active"
    # Checkout still waiting for 3-D Secure on another subscription changes nothing.
    assert "not the organization's current" in state.apply_subscription(
        account, s(sub_id="sub_INCOMPLETE1", status="incomplete"), t0 + timedelta(minutes=9))
    assert account.stripe_subscription_id == "sub_NEW0001"
    # Period and cancel-at-period-end come from Stripe (old or new API shape).
    state.apply_subscription(account, s(sub_id="sub_NEW0001", status="active", price="price_growth",
                                        cancel_at_period_end=True), t0 + timedelta(minutes=10))
    assert account.cancel_at_period_end and account.current_period_end > timezone.now()
    assert account.status_label == "Active until the end of the period"


@pytest.mark.django_db
def test_trial_without_subscription_ends_and_pauses_intake(org, account):
    assert account.intake_open and account.trial_days_left == 14
    account.trial_ends_at = timezone.now() - timedelta(minutes=1)
    account.save()
    assert account.trial_over and not account.intake_open and account.status_label == "Trial ended"
    with pytest.raises(UsageLimitReached, match="free trial"):
        check_intake(org)


@pytest.mark.django_db
def test_checkout_completed_event_links_customer_and_reads_the_subscription(client, org, account):
    calls = []

    def stripe(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer sk_test_123"
        assert request.url.path == "/v1/subscriptions/sub_FromCheckout1"
        return httpx.Response(200, json=_sub(sub_id="sub_FromCheckout1", status="trialing", customer="cus_NEW123",
                                             price="price_growth", org=org))

    stripe_api._TRANSPORT = httpx.MockTransport(stripe)
    try:
        session = {"id": "cs_test_1", "object": "checkout.session", "mode": "subscription", "customer": "cus_NEW123",
                   "client_reference_id": str(org.pk), "subscription": "sub_FromCheckout1",
                   "metadata": {"org_id": str(org.pk), "plan": "growth"}}
        assert _post(client, _event("checkout.session.completed", session)).status_code == 200
    finally:
        stripe_api._TRANSPORT = None
    account.refresh_from_db()
    assert account.stripe_customer_id == "cus_NEW123" and account.stripe_subscription_id == "sub_FromCheckout1"
    assert account.status == "trialing" and account.plan == "growth" and len(calls) == 1


@pytest.mark.django_db
def test_payment_failed_emails_admins(client, org, account, admin_user, user,
                                      django_capture_on_commit_callbacks):
    account.stripe_customer_id = "cus_PAY1"
    account.save()
    invoice = {"id": "in_1", "object": "invoice", "customer": "cus_PAY1", "currency": "usd", "amount_due": 49900,
               "next_payment_attempt": int(time.time()) + 3 * 86400}
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert _post(client, _event("invoice.payment_failed", invoice)).status_code == 200
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [admin_user.email]
    assert "USD 499.00" in mail.outbox[0].body and "/settings/billing/" in mail.outbox[0].body


# ---------------------------------------------------------------- checkout and portal (fake Stripe)


class FakeStripe:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def form(self, i=-1) -> dict:
        return dict(parse_qsl(self.requests[i].content.decode()))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/customers":
            return httpx.Response(200, json={"id": "cus_FAKE001", "object": "customer"})
        if path == "/v1/checkout/sessions":
            return httpx.Response(200, json={"id": "cs_test_abc", "url": "https://checkout.stripe.com/c/pay/cs_test_abc"})
        if path == "/v1/billing_portal/sessions":
            return httpx.Response(200, json={"id": "bps_1", "url": "https://billing.stripe.com/p/session/test_1"})
        if path == "/v1/checkout/sessions/cs_test_abc":
            return httpx.Response(200, json={"id": "cs_test_abc", "client_reference_id": str(self.org_pk),
                                             "customer": "cus_FAKE001", "subscription": "sub_Checkout42"})
        if path == "/v1/subscriptions/sub_Checkout42":
            return httpx.Response(200, json=_sub(sub_id="sub_Checkout42", status="active", customer="cus_FAKE001",
                                                 price="price_growth"))
        return httpx.Response(404, json={"error": {"message": "No such resource", "type": "invalid_request_error"}})


@pytest.fixture
def fake_stripe(monkeypatch, org):
    fake = FakeStripe()
    fake.org_pk = org.pk
    monkeypatch.setattr(stripe_api, "_TRANSPORT", httpx.MockTransport(fake))
    return fake


@pytest.mark.django_db
def test_checkout_creates_customer_and_subscription_session(client, admin_user, org, account, fake_stripe):
    client.force_login(admin_user)
    r = client.post(reverse("billing:checkout"), {"plan": "starter"})
    assert r.status_code == 302 and r.url == "https://checkout.stripe.com/c/pay/cs_test_abc"
    customer, session = fake_stripe.requests[0], fake_stripe.requests[1]
    assert customer.url.path == "/v1/customers" and customer.headers["Idempotency-Key"]
    assert dict(parse_qsl(customer.content.decode()))["metadata[org_id]"] == str(org.pk)
    form = fake_stripe.form(1)
    assert session.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert form["mode"] == "subscription" and form["customer"] == "cus_FAKE001"
    assert form["line_items[0][price]"] == "price_starter" and form["line_items[0][quantity]"] == "1"
    assert form["client_reference_id"] == str(org.pk) and form["subscription_data[metadata][org_id]"] == str(org.pk)
    assert form["success_url"].endswith("/settings/billing/?checkout=done&session_id={CHECKOUT_SESSION_ID}")
    # Subscribing during the trial: the first payment waits until the trial ends.
    assert int(form["subscription_data[trial_end]"]) == int(account.trial_ends_at.timestamp())
    account.refresh_from_db()
    assert account.stripe_customer_id == "cus_FAKE001"
    assert AuditEvent.objects.filter(action="billing.checkout_started", organization=org).exists()

    # Coming back from Checkout reads the session and subscription right away.
    r = client.get(reverse("billing:settings"), {"checkout": "done", "session_id": "cs_test_abc"})
    assert r.status_code == 302
    account.refresh_from_db()
    assert account.status == "active" and account.plan == "growth" and account.stripe_subscription_id == "sub_Checkout42"


@pytest.mark.django_db
def test_checkout_return_for_another_org_is_refused(client, admin_user, org, account, fake_stripe):
    fake_stripe.org_pk = org.pk + 999
    client.force_login(admin_user)
    r = client.get(reverse("billing:settings"), {"checkout": "done", "session_id": "cs_test_abc"}, follow=True)
    assert b"belongs to another organization" in r.content
    account.refresh_from_db()
    assert not account.stripe_subscription_id
    # A session id that isn't Stripe's shape never reaches Stripe.
    n = len(fake_stripe.requests)
    client.get(reverse("billing:settings"), {"checkout": "done", "session_id": "../customers/cus_X"})
    assert len(fake_stripe.requests) == n


@pytest.mark.django_db
def test_portal_and_plan_change_go_through_the_portal(client, admin_user, org, account, fake_stripe):
    client.force_login(admin_user)
    r = client.post(reverse("billing:portal"))
    assert r.status_code == 302 and r.url == reverse("billing:settings")   # no customer yet
    account.stripe_customer_id, account.stripe_subscription_id, account.stripe_status = "cus_FAKE001", "sub_X1234567", "active"
    account.status = "active"
    account.save()
    r = client.post(reverse("billing:portal"))
    assert r.url == "https://billing.stripe.com/p/session/test_1"
    assert fake_stripe.form()["customer"] == "cus_FAKE001"
    assert fake_stripe.form()["return_url"].endswith("/settings/billing/")
    # With a live subscription, choosing another plan opens the portal instead of a second subscription.
    r = client.post(reverse("billing:checkout"), {"plan": "growth"})
    assert r.url == "https://billing.stripe.com/p/session/test_1"
    assert not any(req.url.path == "/v1/checkout/sessions" for req in fake_stripe.requests)


@pytest.mark.django_db
def test_stripe_errors_are_shown_plainly(client, admin_user, org, account, monkeypatch):
    monkeypatch.setattr(stripe_api, "_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(
        400, json={"error": {"message": "No such price: 'price_starter'", "type": "invalid_request_error"}})))
    client.force_login(admin_user)
    r = client.post(reverse("billing:checkout"), {"plan": "starter"}, follow=True)
    assert b"Stripe refused the request: No such price" in r.content


@pytest.mark.django_db
def test_nothing_goes_to_stripe_on_a_public_demo(client, admin_user, org, account, fake_stripe, settings):
    settings.DEMO_MODE, settings.DEMO_SEND_OUTSIDE = True, False
    client.force_login(admin_user)
    r = client.post(reverse("billing:checkout"), {"plan": "starter"}, follow=True)
    assert b"public demo" in r.content and fake_stripe.requests == []


@pytest.mark.django_db
def test_billing_page_is_for_admins(client, user, admin_user, org, account):
    client.force_login(user)
    assert client.get(reverse("billing:settings")).status_code == 403
    assert client.post(reverse("billing:checkout"), {"plan": "starter"}).status_code == 403
    client.force_login(admin_user)
    html = client.get(reverse("billing:settings")).content.decode()
    assert "Documents during the trial" in html and "Choose Starter" in html and "Free trial" in html
    assert reverse("billing:settings") in client.get(reverse("core:settings")).content.decode()   # in the sub-nav


# ---------------------------------------------------------------- usage limits


def _docs(org, n, start=0):
    for i in range(start, start + n):
        Document.objects.create(organization=org, original_filename=f"inv-{i}.pdf", sha256=f"{i:064d}",
                                status=Document.Status.EXTRACTED)


@pytest.mark.django_db
def test_usage_counts_documents_not_containers_or_duplicates(org, account):
    _docs(org, 3)
    zip_doc = Document.objects.create(organization=org, original_filename="batch.zip", sha256="z" * 64,
                                      status=Document.Status.ARCHIVE)
    Document.objects.create(organization=org, original_filename="a.pdf", sha256="y" * 64, parent=zip_doc,
                            status=Document.Status.MATCHED)
    usage = usage_for(org)
    assert usage.used == 4 and usage.allowance == 5 and usage.hard_limit == 6 and usage.trial


@pytest.mark.django_db
def test_soft_limit_banner_and_one_email_then_hard_stop(client, org, account, admin_user, user, dataset,
                                                        django_capture_on_commit_callbacks):
    _docs(org, 4)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        _docs(org, 1, start=10)                        # 5 of 5: soft limit
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [admin_user.email]
    assert "free trial's documents" in mail.outbox[0].subject
    with django_capture_on_commit_callbacks(execute=True):
        _docs(org, 1, start=20)                        # 6 of 5: no second email this month
    assert len(mail.outbox) == 1
    client.force_login(user)
    html = client.get(reverse("core:dashboard")).content.decode()
    assert "the most the free trial includes" in html and "new documents are paused" in html

    # 6 = 120%: the next upload is refused, with what to do.
    pdf = (dataset / "pdf" / "S02_1_commercial_invoice.pdf").read_bytes()
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(UsageLimitReached, match="choose a plan in Settings, Billing"):
            ingest_bytes(org, "new.pdf", pdf, process="none")
    assert any("paused" in m.subject for m in mail.outbox)
    assert AuditEvent.objects.filter(organization=org, action="billing.intake_paused").count() == 1
    r = client.post(reverse("review:upload"), {"files": [_upload("new.pdf", pdf)]}, follow=True)
    assert b"New documents are paused" in r.content
    assert Document.objects.filter(organization=org).count() == 6


def _upload(name, content):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, content, content_type="application/pdf")


@pytest.mark.django_db
def test_hard_limit_never_blocks_review_approval_or_posting(client, org, billing, dataset, user, approver,
                                                            approver2, monkeypatch):
    from apps.accounting.models import QBOConnection
    from apps.accounting.services.posting import post_shipment
    from tests.test_quickbooks import FakeQBO, _client

    for f in ["S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    # The plan is now far over its limit (and the trial is over, too).
    BillingAccount.objects.create(organization=org, status=BillingAccount.Status.TRIALING, plan="growth",
                                  trial_ends_at=timezone.now() - timedelta(days=1))
    with pytest.raises(UsageLimitReached):
        check_intake(org)
    shipment = Shipment.objects.get(organization=org)
    doc = shipment.documents.filter(doc_type="freight_invoice").first()
    # Reviewer corrects a value and accepts warnings.
    client.force_login(user)
    r = client.post(reverse("review:update_field", args=[doc.pk]), {"name": "invoice_number", "value": "FI-CHANGED-1"})
    assert r.status_code == 302
    assert doc.fields.get(name="invoice_number").value == "FI-CHANGED-1"
    for issue in shipment.issues.filter(resolved=False, severity="warning"):
        client.post(reverse("review:resolve_issue", args=[issue.pk]))
    client.force_login(approver)
    for issue in shipment.issues.filter(resolved=False, severity="error"):
        client.post(reverse("review:resolve_issue", args=[issue.pk]), {"note": "Checked with the vendor by phone."})
    client.force_login(approver2)
    client.post(reverse("review:approve", args=[shipment.pk]))
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.APPROVED
    QBOConnection.objects.create(organization=org, realm_id="123", access_token="tok", refresh_token="ref",
                                 access_expires_at=timezone.now() + timedelta(hours=1), default_expense_account_id="77")
    summary = post_shipment(shipment, client=_client(org, FakeQBO()))
    assert summary["posted"] == 2 and summary["failed"] == 0


@pytest.mark.django_db
def test_a_zip_that_does_not_fit_is_refused_whole(org, account, dataset):
    import io
    import zipfile

    _docs(org, 5)          # at the soft limit, one below the hard limit (6)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in ["S04_1_commercial_invoice.pdf", "S04_2_bill_of_lading.pdf", "S04_3_freight_invoice.pdf"]:
            z.writestr(f, (dataset / "pdf" / f).read_bytes())
    # Three documents don't fit in the room for one: nothing is added, so no ZIP is stopped halfway.
    with pytest.raises(UsageLimitReached, match="holds 3 documents"):
        ingest_bytes(org, "batch.zip", buf.getvalue(), process="none")
    assert not Document.objects.filter(organization=org, original_filename__startswith="S04_").exists()


@pytest.mark.django_db
def test_api_upload_gets_402_with_the_reason(client, org, account, dataset):
    from apps.accounts.services.apikeys import create_key

    account.trial_ends_at = timezone.now() - timedelta(hours=1)
    account.save()
    _key, token = create_key(org, "ERP", "reviewer", None, None)
    pdf = (dataset / "pdf" / "S01_1_commercial_invoice.pdf").read_bytes()
    r = client.post(f"/api/{org.slug}/documents", {"file": _upload("a.pdf", pdf)},
                    HTTP_AUTHORIZATION=f"Bearer {token}")
    assert r.status_code == 402 and "free trial" in r.json()["detail"]


@pytest.mark.django_db
def test_email_attachments_over_the_limit_are_listed_with_the_reason(org, account, dataset):
    from apps.mailboxes.models import EmailAttachment
    from apps.mailboxes.services.inbound import ensure_inbound
    from apps.mailboxes.services.intake import IncomingAttachment, IncomingEmail, receive

    account.trial_ends_at = timezone.now() - timedelta(hours=1)
    account.save()
    pdf = (dataset / "pdf" / "S01_1_commercial_invoice.pdf").read_bytes()
    incoming = IncomingEmail(message_id="<limit-1@vendor.example>", sender="ap@vendor.example", subject="Invoice",
                             attachments=[IncomingAttachment(filename="inv.pdf", content=pdf,
                                                             content_type="application/pdf")])
    receive(ensure_inbound(org), incoming, process="none")
    row = EmailAttachment.objects.get(filename="inv.pdf")
    assert row.outcome == EmailAttachment.Outcome.SKIPPED and "free trial" in row.reason
    assert not Document.objects.filter(organization=org).exists()


@pytest.mark.django_db
def test_no_limits_when_billing_is_off_or_org_is_billed_by_agreement(org, settings, billing):
    _docs(org, 50)
    check_intake(org)                                   # no account: billed outside ShipMatch
    BillingAccount.objects.create(organization=org, status=BillingAccount.Status.CANCELED)   # opened Checkout only
    check_intake(org)
    settings.BILLING_ENABLED = False
    BillingAccount.objects.filter(organization=org).update(status="trialing",
                                                           trial_ends_at=timezone.now() - timedelta(days=3))
    check_intake(org)


@pytest.mark.django_db
def test_seat_limit_on_invites(client, admin_user, org, billing, user, viewer):
    BillingAccount.objects.create(organization=org, status="active", plan="starter",
                                  stripe_subscription_id="sub_seats001", stripe_status="active")
    client.force_login(admin_user)
    r = client.post(reverse("core:invite"), {"email": "new@test.example", "role": "reviewer"}, follow=True)
    assert b"The Starter plan includes 3 users" in r.content
    assert not Membership.objects.filter(user__email="new@test.example").exists()
    Membership.objects.filter(user=viewer).delete()
    client.post(reverse("core:invite"), {"email": "new@test.example", "role": "reviewer"})
    assert Membership.objects.filter(user__email="new@test.example", organization=org).exists()


@pytest.mark.django_db
def test_plan_limits_follow_the_plan(org, billing):
    from apps.billing.plans import feature_allowed, limits_for

    acc = BillingAccount.objects.create(organization=org, status="active", plan="starter",
                                        stripe_subscription_id="sub_plan0001", stripe_status="active")
    limits = limits_for(org)
    assert limits.documents == 10 and limits.users == 3 and not limits.allows("webhooks")
    assert not feature_allowed(org, "webhooks")
    acc.plan = "growth"
    acc.save()
    assert feature_allowed(org, "webhooks") and limits_for(org).documents == 100
    assert usage_for(org).hard_limit == 120 and usage_for(org).period_start <= timezone.now()


@pytest.mark.django_db
def test_paid_plan_month_uses_the_stripe_period(client, org, billing, admin_user, user,
                                                django_capture_on_commit_callbacks):
    start = timezone.now() - timedelta(days=3)
    end = start + timedelta(days=30)
    BillingAccount.objects.create(organization=org, status="active", plan="starter", stripe_status="active",
                                  stripe_subscription_id="sub_paid0001", current_period_start=start,
                                  current_period_end=end)
    old = Document.objects.create(organization=org, original_filename="last-month.pdf", sha256="o" * 64)
    Document.objects.filter(pk=old.pk).update(received_at=start - timedelta(days=1))   # before this period
    _docs(org, 9)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        _docs(org, 1, start=50)                               # 10 of 10
    assert len(mail.outbox) == 1 and "this month's documents" in mail.outbox[0].subject
    usage = usage_for(org)
    assert usage.used == 10 and usage.period_start == start and usage.period_end == end and not usage.trial
    client.force_login(user)
    html = client.get(reverse("core:dashboard")).content.decode()
    assert "10 of 10 documents used this billing month" in html and "Choose a bigger plan" not in html  # not an admin
    _docs(org, 2, start=60)                                   # 12 = 120%
    with pytest.raises(UsageLimitReached) as e:
        check_intake(org)
    local_end = timezone.localtime(end)
    assert f"Intake starts again on {local_end.day} {local_end:%b %Y}" in str(e.value)
    client.force_login(admin_user)
    html = client.get(reverse("billing:settings")).content.decode()
    assert "is-stopped" in html and "12 stop" in html


@pytest.mark.django_db
def test_past_due_banner_asks_admins_to_update_the_card(client, org, billing, admin_user):
    BillingAccount.objects.create(organization=org, status="past_due", plan="starter", stripe_status="past_due",
                                  stripe_subscription_id="sub_late0001", stripe_customer_id="cus_late")
    client.force_login(admin_user)
    html = client.get(reverse("review:queue")).content.decode()
    assert "The last payment didn" in html and "go through" in html and "Update payment" in html
    check_intake(org)                                         # nothing paused while Stripe retries
