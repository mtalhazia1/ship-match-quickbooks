"""Outgoing webhooks: address guard (private networks, DNS answers, redirects), signatures a receiver can check,
events from what happens in ShipMatch, retries with backoff, turning a failing endpoint off, replay, tests,
secret rotation, permissions and the plan."""
import hashlib
import hmac
import json
import socket
from datetime import timedelta

import httpx
import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import BillingAccount
from apps.core.models import AuditEvent
from apps.documents.services.ingest import ingest_bytes
from apps.integrations import delivery as delivery_mod
from apps.integrations import dispatch, signing, urlguard
from apps.integrations.models import WebhookDelivery, WebhookEndpoint, WebhookEvent
from apps.integrations.tasks import retry_due_deliveries
from apps.shipments.models import Shipment
from apps.shipments.services.validation import validate_shipment

PUBLIC_IP = "93.184.215.14"
URL = "https://erp.example.com/hooks/shipmatch?token=abc"


class Receiver:
    """A fake receiving system: queued answers (default 200), every request kept."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.responses: list = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            answer = self.responses.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return httpx.Response(200, text="ok")

    def json(self, i=-1) -> dict:
        return json.loads(self.requests[i].content)


@pytest.fixture
def dns(monkeypatch):
    """Host name -> IP addresses, instead of real DNS."""
    table = {"erp.example.com": [PUBLIC_IP], "other.example.com": ["93.184.215.15"]}

    def fake(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror("Name or service not known")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in table[host]]

    monkeypatch.setattr(urlguard, "_getaddrinfo", fake)
    return table


@pytest.fixture
def receiver(monkeypatch, settings, dns):
    settings.SITE_URL = "https://ap.example.com"
    r = Receiver()
    monkeypatch.setattr(delivery_mod, "_TRANSPORT", httpx.MockTransport(r))
    return r


@pytest.fixture
def publish(django_capture_on_commit_callbacks):
    """dispatch.publish, then what runs after the transaction commits (queueing the deliveries)."""
    def run(*args, **kwargs):
        with django_capture_on_commit_callbacks(execute=True):
            return dispatch.publish(*args, **kwargs)
    return run


def _endpoint(org, events=("shipment.approved",), url=URL, **kw):
    return WebhookEndpoint.objects.create(organization=org, url=url, events=list(events), secret="whsec_testsecret",
                                          **kw)


@pytest.fixture
def ready(org, dataset):
    for f in ["S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    s = Shipment.objects.get(organization=org)
    assert s.status == Shipment.Status.READY
    return s


def _approve(client, approver, shipment, capture):
    client.force_login(approver)
    with capture(execute=True):
        client.post(reverse("review:approve", args=[shipment.pk]))
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.APPROVED


# ---------------------------------------------------------------- address guard


@pytest.mark.parametrize("url,why", [
    ("http://erp.example.com/hook", "https://"),
    ("https://user:pw@erp.example.com/hook", "user name or password"),
    ("https://erp.example.com:22/hook", "port"),
    ("https://localhost/hook", "local name"),
    ("https://billing.internal/hook", "local name"),
    ("https://intranet/hook", "local name"),
    ("https://10.0.0.5/hook", "private"),
    ("https://127.0.0.1/hook", "private"),
    ("https://169.254.169.254/latest/meta-data", "private"),
    ("https://100.64.1.1/hook", "private"),
    ("https://[::1]/hook", "private"),
    ("https://[::ffff:127.0.0.1]/hook", "private"),
    ("https://[fd00::1]/hook", "private"),
    ("https://0.0.0.0/hook", "private"),
    ("https://erp.example.com/ho ok", "spaces"),
    ("", "Enter the address"),
])
def test_guard_refuses_unsafe_addresses(url, why):
    with pytest.raises(urlguard.URLRejected, match=why):
        urlguard.check_url(url)


def test_guard_accepts_public_https(dns):
    assert urlguard.check_url(URL) == URL
    assert urlguard.check_url("https://erp.example.com:8443/x#frag") == "https://erp.example.com:8443/x"
    target = urlguard.check_and_resolve("https://erp.example.com:8443/x?y=1")
    assert target.url == f"https://{PUBLIC_IP}:8443/x?y=1" and target.host_header == "erp.example.com:8443"
    assert target.sni_host == "erp.example.com"
    assert urlguard.check_and_resolve("https://93.184.215.14/hook").ip == PUBLIC_IP


def test_guard_checks_every_dns_answer(dns):
    dns["sneaky.example.com"] = [PUBLIC_IP, "10.1.2.3"]          # one private answer is enough to refuse
    with pytest.raises(urlguard.URLRejected, match="private or local network"):
        urlguard.check_and_resolve("https://sneaky.example.com/hook")
    dns["mapped.example.com"] = ["::ffff:192.168.0.1"]
    with pytest.raises(urlguard.URLRejected, match="private"):
        urlguard.check_and_resolve("https://mapped.example.com/hook")
    with pytest.raises(urlguard.URLRejected, match="couldn't find") as e:
        urlguard.check_and_resolve("https://typo.example.com/hook")
    assert e.value.permanent is False


# ---------------------------------------------------------------- settings pages


@pytest.mark.django_db
def test_add_endpoint_checks_the_address_and_shows_the_secret_once(client, admin_user, org, receiver, dns):
    client.force_login(admin_user)
    dns["evil.example.com"] = ["192.168.1.10"]
    r = client.post(reverse("integrations:webhooks"), {"url": "https://evil.example.com/x",
                                                       "events": ["shipment.approved"]}, follow=True)
    assert b"private or local network" in r.content and not WebhookEndpoint.objects.exists()
    r = client.post(reverse("integrations:webhooks"), {"url": URL, "events": []}, follow=True)
    assert b"Choose at least one event" in r.content and not WebhookEndpoint.objects.exists()
    r = client.post(reverse("integrations:webhooks"), {"url": URL, "description": "NetSuite",
                                                       "events": ["shipment.approved", "bill.posted", "bogus"]})
    ep = WebhookEndpoint.objects.get()
    assert r.url == reverse("integrations:webhook_edit", args=[ep.pk])
    assert ep.events == ["shipment.approved", "bill.posted"] and ep.secret.startswith("whsec_")
    page = client.get(r.url).content.decode()
    assert ep.secret in page and "Copy the signing secret now" in page
    assert ep.secret not in client.get(r.url).content.decode()            # shown once
    event = AuditEvent.objects.get(action="webhook.created")
    assert event.data["host"] == "erp.example.com" and ep.secret not in json.dumps(event.data)
    assert "token=abc" not in client.get(reverse("integrations:webhooks")).content.decode()   # list hides the query


@pytest.mark.django_db
def test_webhook_pages_are_for_admins_of_the_organization(client, user, admin_user, org, receiver):
    from apps.core.models import Organization

    other = Organization.objects.create(name="Other", slug="other")
    theirs = _endpoint(other)
    client.force_login(user)
    assert client.get(reverse("integrations:webhooks")).status_code == 403
    client.force_login(admin_user)
    assert client.get(reverse("integrations:webhooks")).status_code == 200
    assert client.get(reverse("integrations:webhook_edit", args=[theirs.pk])).status_code == 404
    assert client.post(reverse("integrations:webhook_delete", args=[theirs.pk])).status_code == 404
    assert WebhookEndpoint.objects.filter(pk=theirs.pk).exists()


# ---------------------------------------------------------------- delivery and signature


@pytest.mark.django_db
def test_approval_sends_a_signed_event_a_receiver_can_verify(client, approver, org, ready, receiver,
                                                             django_capture_on_commit_callbacks):
    ep = _endpoint(org)
    _approve(client, approver, ready, django_capture_on_commit_callbacks)
    assert len(receiver.requests) == 1
    req = receiver.requests[0]
    # Sent to the address that was checked, with the real host name for TLS and Host.
    assert req.url.host == PUBLIC_IP and req.url.path == "/hooks/shipmatch" and req.url.params["token"] == "abc"
    assert req.headers["Host"] == "erp.example.com"
    assert req.headers["ShipMatch-Event-Type"] == "shipment.approved"
    body = json.loads(req.content)
    assert body["type"] == "shipment.approved" and body["organization"] == org.slug and body["id"].startswith("evt_")
    assert req.headers["ShipMatch-Event-Id"] == body["id"]
    obj = body["data"]["object"]
    assert obj["reference"] == ready.reference and obj["status"] == "approved" and obj["approved_by"]
    assert obj["url"] == f"https://ap.example.com/review/shipments/{ready.pk}/" and obj["totals"]
    assert "text" not in obj and "documents" not in obj    # a summary, never document contents
    # What a receiver does with only its secret, the raw body and two headers:
    ts = req.headers["ShipMatch-Timestamp"]
    expected = hmac.new(b"whsec_testsecret", f"{ts}.".encode() + req.content, hashlib.sha256).hexdigest()
    assert req.headers["ShipMatch-Signature"] == f"v1={expected}"
    assert signing.verify("whsec_testsecret", req.content, ts, req.headers["ShipMatch-Signature"])
    assert not signing.verify("whsec_other", req.content, ts, req.headers["ShipMatch-Signature"])
    assert not signing.verify("whsec_testsecret", req.content + b" ", ts, req.headers["ShipMatch-Signature"])
    assert not signing.verify("whsec_testsecret", req.content, str(int(ts) - 3600),
                              req.headers["ShipMatch-Signature"])
    d = WebhookDelivery.objects.get()
    assert d.status == "succeeded" and d.response_status == 200 and d.response_body == "ok" and d.attempts == 1
    ep.refresh_from_db()
    assert ep.last_success_at and ep.consecutive_failures == 0


@pytest.mark.django_db
def test_events_only_go_to_subscribed_enabled_endpoints(client, approver, org, ready, receiver,
                                                        django_capture_on_commit_callbacks):
    _endpoint(org, events=["bill.posted"])
    _endpoint(org, enabled=False)
    _approve(client, approver, ready, django_capture_on_commit_callbacks)
    assert receiver.requests == [] and not WebhookEvent.objects.exists()


@pytest.mark.django_db
def test_redirects_are_not_followed_and_failures_are_retried_with_backoff(org, receiver, settings, publish):
    ep = _endpoint(org, events=["document.received"])
    receiver.responses = [httpx.Response(302, headers={"Location": "https://10.0.0.1/steal"}),
                          httpx.Response(503, text="maintenance", headers={"Retry-After": "120"}),
                          httpx.Response(204)]
    event = publish(org, "document.received", "Document", 1, {"object": "document", "id": 1}, [ep])
    d = WebhookDelivery.objects.get(event=event)
    d.refresh_from_db()
    assert len(receiver.requests) == 1                          # the redirect was not followed
    assert d.status == "retrying" and d.response_status == 302 and "redirect" in d.error
    assert 25 <= (d.next_attempt_at - timezone.now()).total_seconds() <= 31
    assert retry_due_deliveries() == 0                          # not due yet
    WebhookDelivery.objects.filter(pk=d.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
    assert retry_due_deliveries() == 1
    d.refresh_from_db()
    assert d.status == "retrying" and d.attempts == 2 and d.response_body == "maintenance"
    assert (d.next_attempt_at - timezone.now()).total_seconds() > 100   # Retry-After respected
    WebhookDelivery.objects.filter(pk=d.pk).update(next_attempt_at=timezone.now())
    retry_due_deliveries()
    d.refresh_from_db()
    assert d.status == "succeeded" and d.attempts == 3 and len(receiver.requests) == 3
    # Every attempt carries the same event id and a fresh signature.
    assert len({r.headers["ShipMatch-Event-Id"] for r in receiver.requests}) == 1


@pytest.mark.django_db
def test_gives_up_after_max_attempts(org, receiver, settings, publish):
    settings.WEBHOOK_MAX_ATTEMPTS = 2
    ep = _endpoint(org, events=["document.received"])
    receiver.responses = [httpx.Response(500), httpx.ConnectError("refused")]
    publish(org, "document.received", "Document", 1, {"object": "document"}, [ep])
    WebhookDelivery.objects.update(next_attempt_at=timezone.now())
    retry_due_deliveries()
    d = WebhookDelivery.objects.get()
    assert d.status == "failed" and d.attempts == 2 and "Gave up after 2 attempts" in d.error
    assert "Couldn't connect" in d.error


@pytest.mark.django_db
def test_endpoint_is_turned_off_after_repeated_failures_and_admins_are_told(org, receiver, settings, admin_user,
                                                                            publish):
    settings.WEBHOOK_DISABLE_AFTER_FAILURES = 3
    ep = _endpoint(org, events=["document.received"])
    receiver.responses = [httpx.Response(500)] * 10
    mail.outbox.clear()
    for n in range(3):
        publish(org, "document.received", "Document", n, {"object": "document"}, [ep])
    ep.refresh_from_db()
    assert not ep.enabled and ep.consecutive_failures == 3 and "3 failed attempts" in ep.disabled_reason
    assert set(WebhookDelivery.objects.values_list("status", flat=True)) == {"failed"}
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [admin_user.email] and "turned off" in mail.outbox[0].subject
    assert AuditEvent.objects.filter(action="webhook.disabled", organization=org).exists()
    # Nothing more is sent while it is off.
    n = len(receiver.requests)
    assert publish(org, "document.received", "Document", 9, {"object": "document"}) is None
    assert len(receiver.requests) == n


@pytest.mark.django_db
def test_a_success_resets_the_failure_count(org, receiver, settings, publish):
    settings.WEBHOOK_DISABLE_AFTER_FAILURES = 3
    ep = _endpoint(org, events=["document.received"])
    receiver.responses = [httpx.Response(500), httpx.Response(500), httpx.Response(200), httpx.Response(500)]
    for n in range(2):
        publish(org, "document.received", "Document", n, {"object": "document"}, [ep])
    WebhookDelivery.objects.filter(status="retrying").update(next_attempt_at=timezone.now())
    retry_due_deliveries()
    ep.refresh_from_db()
    assert ep.enabled and ep.consecutive_failures == 1    # 500, 500, then 200 (reset), then 500


@pytest.mark.django_db
def test_dns_change_to_a_private_address_is_caught_at_send_time(org, receiver, dns, publish):
    ep = _endpoint(org, events=["document.received"])
    dns["erp.example.com"] = ["127.0.0.1"]                     # DNS rebinding after the endpoint was saved
    publish(org, "document.received", "Document", 1, {"object": "document"}, [ep])
    d = WebhookDelivery.objects.get()
    assert receiver.requests == [] and d.status == "failed" and "private" in d.error


@pytest.mark.django_db
def test_nothing_leaves_a_public_demo(org, receiver, settings, publish):
    settings.DEMO_MODE, settings.DEMO_SEND_OUTSIDE = True, False
    ep = _endpoint(org, events=["document.received"])
    publish(org, "document.received", "Document", 1, {"object": "document"}, [ep])
    d = WebhookDelivery.objects.get()
    assert receiver.requests == [] and d.status == "failed" and "public demo" in d.error
    ep.refresh_from_db()
    assert ep.consecutive_failures == 0


# ---------------------------------------------------------------- replay, test, rotation


@pytest.mark.django_db
def test_replay_sends_the_same_event_again(client, admin_user, org, receiver, publish,
                                           django_capture_on_commit_callbacks):
    ep = _endpoint(org, events=["document.received"])
    event = publish(org, "document.received", "Document", 1, {"object": "document", "id": 1}, [ep])
    original = WebhookDelivery.objects.get()
    client.force_login(admin_user)
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(reverse("integrations:webhook_replay", args=[original.pk]))
    assert r.status_code == 302
    replay = WebhookDelivery.objects.exclude(pk=original.pk).get()
    assert replay.replay_of == original and replay.event == event and replay.status == "succeeded"
    assert receiver.json(0) == receiver.json(1)
    assert receiver.requests[0].headers["ShipMatch-Event-Id"] == receiver.requests[1].headers["ShipMatch-Event-Id"]
    assert AuditEvent.objects.filter(action="webhook.replayed").exists()
    # Not while the endpoint is off.
    WebhookEndpoint.objects.filter(pk=ep.pk).update(enabled=False)
    r = client.post(reverse("integrations:webhook_replay", args=[original.pk]), follow=True)
    assert b"Turn it on first" in r.content and WebhookDelivery.objects.count() == 2


@pytest.mark.django_db
def test_send_test_event(client, admin_user, org, receiver):
    ep = _endpoint(org)
    client.force_login(admin_user)
    r = client.post(reverse("integrations:webhook_test", args=[ep.pk]), follow=True)
    assert b"Test event delivered" in r.content
    assert receiver.json()["type"] == "test.ping" and receiver.json()["data"]["object"]["object"] == "test"
    receiver.responses = [httpx.Response(401, text="bad signature")]
    r = client.post(reverse("integrations:webhook_test", args=[ep.pk]), follow=True)
    assert b"Test event not delivered" in r.content and b"HTTP 401" in r.content
    d = WebhookDelivery.objects.filter(is_test=True).first()
    assert d.status == "failed" and d.attempts == 1                  # tests are never retried
    ep.refresh_from_db()
    assert ep.consecutive_failures == 0                              # nor counted against the endpoint


@pytest.mark.django_db
def test_rotating_the_secret_signs_with_both_for_a_while(client, admin_user, org, receiver, settings, publish):
    ep = _endpoint(org, events=["document.received"])
    client.force_login(admin_user)
    client.post(reverse("integrations:webhook_rotate", args=[ep.pk]))
    ep.refresh_from_db()
    assert ep.secret != "whsec_testsecret" and ep.previous_secret == "whsec_testsecret"
    page = client.get(reverse("integrations:webhook_edit", args=[ep.pk])).content.decode()
    assert ep.secret in page
    publish(org, "document.received", "Document", 1, {"object": "document"}, [ep])
    req = receiver.requests[-1]
    header = req.headers["ShipMatch-Signature"]
    assert header.count("v1=") == 2
    ts = req.headers["ShipMatch-Timestamp"]
    assert signing.verify(ep.secret, req.content, ts, header)
    assert signing.verify("whsec_testsecret", req.content, ts, header)
    # After the overlap only the new secret signs.
    WebhookEndpoint.objects.filter(pk=ep.pk).update(previous_secret_expires_at=timezone.now() - timedelta(minutes=1))
    publish(org, "document.received", "Document", 2, {"object": "document"}, [ep])
    assert receiver.requests[-1].headers["ShipMatch-Signature"].count("v1=") == 1


@pytest.mark.django_db
def test_turning_an_endpoint_back_on_resets_it(client, admin_user, org, receiver):
    ep = _endpoint(org, enabled=False, consecutive_failures=20, disabled_reason="Turned off after 20 failed attempts")
    client.force_login(admin_user)
    client.post(reverse("integrations:webhook_edit", args=[ep.pk]),
                {"url": URL, "events": ["shipment.approved"], "enabled": "on"})
    ep.refresh_from_db()
    assert ep.enabled and ep.consecutive_failures == 0 and ep.disabled_reason == ""


# ---------------------------------------------------------------- events from ShipMatch


@pytest.mark.django_db
def test_issue_created_is_sent_once_per_issue(org, dataset, receiver, django_capture_on_commit_callbacks):
    _endpoint(org, events=["issue.created"])
    with django_capture_on_commit_callbacks(execute=True):
        for f in ["S03_1_commercial_invoice.pdf", "S03_2_bill_of_lading.pdf", "S03_3_freight_invoice.pdf"]:
            ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    shipment = Shipment.objects.get(organization=org)
    sent = [receiver.json(i)["data"]["object"] for i in range(len(receiver.requests))]
    codes = sorted(o["code"] for o in sent)
    open_codes = sorted(shipment.issues.filter(resolved=False).values_list("code", flat=True))
    assert "total_mismatch" in codes and codes == open_codes
    mismatch = next(o for o in sent if o["code"] == "total_mismatch")
    assert mismatch["severity"] == "error" and mismatch["shipment"]["reference"] == shipment.reference
    n = len(receiver.requests)
    with django_capture_on_commit_callbacks(execute=True):
        validate_shipment(shipment)                             # issues re-created: nothing new to announce
    assert len(receiver.requests) == n


@pytest.mark.django_db
def test_shipment_ready_and_document_events(org, dataset, receiver, django_capture_on_commit_callbacks):
    _endpoint(org, events=["shipment.ready", "document.received", "document.extracted"])
    with django_capture_on_commit_callbacks(execute=True):
        for f in ["S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf"]:
            ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    types = [receiver.json(i)["type"] for i in range(len(receiver.requests))]
    assert types.count("document.received") == 3 and types.count("document.extracted") == 3
    assert types.count("shipment.ready") == 1
    extracted = next(receiver.json(i) for i, t in enumerate(types) if t == "document.extracted")
    obj = extracted["data"]["object"]
    assert obj["object"] == "document" and obj["type"] in ("commercial_invoice", "bill_of_lading", "freight_invoice")
    assert "text" not in obj and "fields" in obj


@pytest.mark.django_db
def test_bill_events_from_posting(org, receiver, ready, django_capture_on_commit_callbacks):
    from apps.core.utils import audit

    _endpoint(org, events=["bill.posted", "bill.failed"])
    doc = ready.documents.filter(doc_type="freight_invoice").first()
    with django_capture_on_commit_callbacks(execute=True):
        audit(org, "bill.posted", doc, qbo_bill_id="501")
        audit(org, "vendor_credit.failed", doc, error="Vendor inactive")
    posted, failed = receiver.json(0), receiver.json(1)
    assert posted["type"] == "bill.posted" and posted["data"]["object"]["accounting_id"] == "501"
    assert failed["type"] == "bill.failed" and failed["data"]["object"]["kind"] == "vendor_credit"
    assert failed["data"]["object"]["error"] == "Vendor inactive"


@pytest.mark.django_db
def test_webhooks_follow_the_plan(client, admin_user, org, receiver, settings, publish):
    settings.BILLING_ENABLED = True
    settings.BILLING_PLANS = {"starter": {"name": "Starter", "price_id": "p1", "price": "1", "documents": 10,
                                          "users": 0, "features": ["exports"]}}
    BillingAccount.objects.create(organization=org, status="active", plan="starter",
                                  stripe_subscription_id="sub_plan9999", stripe_status="active")
    ep = _endpoint(org, events=["document.received"])
    assert publish(org, "document.received", "Document", 1, {"object": "document"}, [ep]) is None
    client.force_login(admin_user)
    assert b"Growth and Scale plans" in client.get(reverse("integrations:webhooks")).content
    r = client.post(reverse("integrations:webhooks"), {"url": URL, "events": ["document.received"]}, follow=True)
    assert b"aren&#x27;t part of your plan" in r.content or b"aren't part of your plan" in r.content
    assert WebhookEndpoint.objects.count() == 1
