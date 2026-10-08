"""Alerts: webhook address guard, Slack and Teams payloads, which actions raise which alerts,
delivery with retry and backoff, email channels, the daily summary and the settings page."""
import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from django.urls import reverse

from apps.accounting.models import PostedBill
from apps.core.models import AuditEvent
from apps.core.utils import audit
from apps.documents.services.ingest import ingest_bytes
from apps.notifications import delivery as delivery_mod
from apps.notifications import digest, events
from apps.notifications.dispatch import notify
from apps.notifications.events import Message
from apps.notifications.models import Channel, Delivery, NotificationSettings
from apps.notifications.render import slack_payload, teams_payload
from apps.notifications.webhooks import WebhookURLError, check_webhook_url
from apps.shipments.models import Shipment
from apps.shipments.services.validation import update_status

SLACK_URL = "https://hooks.slack.com/services/T0000/B0000/abcdefghijklmnop"
TEAMS_URL = "https://prod-12.westus.logic.azure.com:443/workflows/0a1b2c/triggers/manual/paths/invoke?sig=xyz"


class Hooks:
    """Fake Slack/Teams: answers with the queued responses (default 200) and records each request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if self.responses else httpx.Response(200, text="ok")

    def json(self, i=-1) -> dict:
        return json.loads(self.requests[i].content)


@pytest.fixture
def hooks(monkeypatch, settings):
    settings.SITE_URL = "https://shipmatch.example.com"
    h = Hooks()
    monkeypatch.setattr(delivery_mod, "_TRANSPORT", httpx.MockTransport(h))
    return h


def _channel(org, kind="slack", events_=None, **kw):
    defaults = {"slack": {"webhook_url": SLACK_URL}, "teams": {"webhook_url": TEAMS_URL},
                "email": {"email_recipients": "controller@test.example, ap@test.example"}}[kind]
    return Channel.objects.create(organization=org, kind=kind, name=f"{kind} channel",
                                  events=events_ if events_ is not None else [e for e, _, _ in events.EVENTS],
                                  **{**defaults, **kw})


@pytest.fixture
def loaded(org, dataset):
    for f in ["S03_1_commercial_invoice.pdf", "S03_2_bill_of_lading.pdf", "S03_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    return Shipment.objects.get(organization=org)


# ---------------------------------------------------------------- webhook address guard


@pytest.mark.parametrize("kind,url", [
    ("slack", SLACK_URL),
    ("teams", TEAMS_URL),
    ("teams", "https://contoso.webhook.office.com/webhookb2/abc@def/IncomingWebhook/123/456"),
    ("teams", "https://default1234.56.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/x"),
])
def test_webhook_guard_accepts_slack_and_teams_hosts(kind, url):
    assert check_webhook_url(kind, url) == url


@pytest.mark.parametrize("kind,url,words", [
    ("slack", "http://hooks.slack.com/services/T/B/x", "https://"),
    ("slack", "https://hooks.slack.com.evil.example/services/T/B/x", "not a Slack webhook"),
    ("slack", "https://evil.example/hooks.slack.com/services/T/B/x", "not a Slack webhook"),
    ("slack", "https://user:pw@hooks.slack.com/services/T/B/x", "user name or password"),
    ("slack", "https://hooks.slack.com:8443/services/T/B/x", "standard https port"),
    ("slack", "https://169.254.169.254/latest/meta-data/", "not a Slack webhook"),
    ("slack", "https://localhost/services/x", "not a Slack webhook"),
    ("slack", "https://hooks.slack.com/", "missing the webhook part"),
    ("slack", "https://hooks.slack.com\\@evil.example/x", "characters"),
    ("slack", TEAMS_URL, "not a Slack webhook"),
    ("teams", "https://logic.azure.com/workflows/x", "not a Teams webhook"),
    ("teams", "https://evil-logic.azure.com.attacker.example/workflows/x", "not a Teams webhook"),
    ("teams", "https://10.0.0.5/workflows/x", "not a Teams webhook"),
    ("teams", "ftp://prod-1.westus.logic.azure.com/workflows/x", "https://"),
    ("email", SLACK_URL, "Only Slack and Microsoft Teams"),
])
def test_webhook_guard_rejects_other_addresses_with_a_clear_message(kind, url, words):
    with pytest.raises(WebhookURLError, match=words):
        check_webhook_url(kind, url)


@pytest.mark.django_db
def test_rejected_address_is_never_called_even_if_stored(org, hooks):
    ch = _channel(org, webhook_url="https://internal.example/hook")
    d = Delivery.objects.create(organization=org, channel=ch, event=events.BILL_FAILED, title="x",
                                message=Message(events.BILL_FAILED, "t", "x").as_dict())
    delivery_mod.attempt(d)
    assert d.status == Delivery.Status.FAILED and "not a Slack webhook" in d.error
    assert hooks.requests == []


# ---------------------------------------------------------------- payloads


def test_slack_block_kit_payload():
    msg = Message(events.BILL_FAILED, "A bill couldn't be posted", "Vendor <Acme & Co> is inactive",
                  [["Shipment", "SHP-000001"], ["Amount", "USD 1,200.00"]], "https://sm.example/review/shipments/1/",
                  "Open the shipment", "error", "Test Imports")
    p = slack_payload(msg)
    assert p["text"].startswith("A bill couldn't be posted")
    header, section, fields, actions, context = p["blocks"]
    assert header == {"type": "header", "text": {"type": "plain_text", "text": "A bill couldn't be posted", "emoji": False}}
    assert section["text"]["text"] == "Vendor &lt;Acme &amp; Co&gt; is inactive"
    assert fields["fields"][0] == {"type": "mrkdwn", "text": "*Shipment*\nSHP-000001"}
    button = actions["elements"][0]
    assert button["url"] == "https://sm.example/review/shipments/1/" and button["text"]["text"] == "Open the shipment"
    assert "Test Imports" in context["elements"][0]["text"]
    long = slack_payload(Message("x", "T" * 300, "y"))
    assert len(long["blocks"][0]["text"]["text"]) == 150


def test_teams_adaptive_card_payload():
    msg = Message(events.DISPUTE_OVERDUE, "DSP-000001: no answer", "Follow up", [["Vendor", "Atlas"]],
                  "https://sm.example/disputes/1/", "Open the dispute", "warning", "Test Imports")
    p = teams_payload(msg)
    assert p["type"] == "message"
    att = p["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = att["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    assert card["body"][0]["text"] == "DSP-000001: no answer" and card["body"][0]["color"] == "Warning"
    assert card["body"][2] == {"type": "FactSet", "facts": [{"title": "Vendor", "value": "Atlas"}]}
    assert card["actions"] == [{"type": "Action.OpenUrl", "title": "Open the dispute", "url": "https://sm.example/disputes/1/"}]


# ---------------------------------------------------------------- event mapping


@pytest.mark.django_db
def test_bill_failed_audit_event_alerts_subscribed_channels_after_commit(org, loaded, hooks,
                                                                         django_capture_on_commit_callbacks):
    slack = _channel(org, events_=[events.BILL_FAILED])
    teams = _channel(org, "teams", events_=[events.BILL_FAILED])
    _channel(org, events_=[events.READY])                      # not subscribed
    _channel(org, events_=[events.BILL_FAILED], enabled=False)  # turned off
    doc = loaded.documents.filter(doc_type="freight_invoice").first()
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        audit(org, "bill.failed", doc, error="The vendor 'Atlas' is inactive in QuickBooks.")
        assert hooks.requests == []  # nothing is sent before the action commits
    assert len(callbacks) == 1
    assert sorted(d.channel_id for d in Delivery.objects.filter(status="sent")) == sorted([slack.pk, teams.pk])
    slack_body = next(json.loads(r.content) for r in hooks.requests if r.url.host == "hooks.slack.com")
    assert slack_body["blocks"][0]["text"]["text"] == "A bill couldn't be posted to QuickBooks"
    assert "inactive in QuickBooks" in slack_body["blocks"][1]["text"]["text"]
    url = slack_body["blocks"][-2]["elements"][0]["url"]
    assert url == f"https://shipmatch.example.com{reverse('review:shipment', args=[loaded.pk])}#doc-{doc.pk}"
    teams_body = next(json.loads(r.content) for r in hooks.requests if r.url.host.endswith("logic.azure.com"))
    assert teams_body["attachments"][0]["content"]["body"][0]["text"] == "A bill couldn't be posted to QuickBooks"


@pytest.mark.django_db
def test_qbo_reconnect_and_unmapped_actions(org, hooks, django_capture_on_commit_callbacks):
    _channel(org, events_=[events.QBO_RECONNECT, events.BILL_FAILED])
    with django_capture_on_commit_callbacks(execute=True):
        audit(org, "qbo.needs_reconnect", org, error="invalid_grant")
        audit(org, "field.corrected", org, field="total_amount")  # not an alert
    assert len(hooks.requests) == 1
    body = hooks.json()
    assert body["blocks"][0]["text"]["text"] == "QuickBooks needs to be connected again"
    assert body["blocks"][-2]["elements"][0]["url"].endswith(reverse("accounting:settings", args=[org.pk]))


@pytest.mark.django_db
def test_shipment_status_changes_alert_once_per_change(org, loaded, hooks, django_capture_on_commit_callbacks):
    _channel(org, events_=[events.NEEDS_REVIEW, events.READY])
    assert loaded.status == Shipment.Status.NEEDS_REVIEW
    with django_capture_on_commit_callbacks(execute=True):
        update_status(loaded)  # unchanged status: no alert
    assert hooks.requests == []
    with django_capture_on_commit_callbacks(execute=True):
        loaded.issues.update(resolved=True)
        update_status(loaded)
    loaded.refresh_from_db()
    assert loaded.status == Shipment.Status.READY
    assert hooks.json()["blocks"][0]["text"]["text"] == f"{loaded.reference} is ready for approval"
    with django_capture_on_commit_callbacks(execute=True):
        loaded.issues.filter(code="total_mismatch").update(resolved=False)
        update_status(loaded)
    body = hooks.json()
    assert body["blocks"][0]["text"]["text"] == f"{loaded.reference} needs review"
    assert "1 error" in body["blocks"][1]["text"]["text"]
    facts = {f["text"].split("\n")[0]: f["text"].split("\n")[1] for f in body["blocks"][2]["fields"]}
    assert facts["*Money at risk*"] == "USD 100.00"
    assert len(hooks.requests) == 2


@pytest.mark.django_db
def test_credit_received_on_a_dispute_alerts_the_team(client, org, loaded, user, approver, hooks, mailoutbox,
                                                      django_capture_on_commit_callbacks):
    from apps.disputes.services import sending, workflow

    _channel(org, events_=[events.CREDIT_RECEIVED])
    issue = loaded.issues.get(code="total_mismatch")
    dispute, _ = workflow.create_draft(loaded, issue.document, [issue.pk], user, use_ai=False)
    dispute.vendor_email = "billing@atlas.example"
    dispute.save()
    sending.send(dispute, approver)
    client.force_login(approver)
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(reverse("disputes:credit", args=[dispute.pk]), {"amount": "100.00", "settles": "on"})
    assert r.status_code == 302
    body = hooks.json()
    assert body["blocks"][0]["text"]["text"] == f"Credit received: USD 100.00 from {dispute.vendor_name}"
    assert body["blocks"][-2]["elements"][0]["url"].endswith(reverse("disputes:detail", args=[dispute.pk]))


@pytest.mark.django_db
def test_overdue_dispute_alert_goes_to_teams(org, loaded, user, approver, hooks, django_capture_on_commit_callbacks):
    from datetime import timedelta

    from django.utils import timezone

    from apps.disputes.models import Dispute
    from apps.disputes.services import sending, workflow
    from apps.disputes.tasks import flag_overdue

    _channel(org, "teams", events_=[events.DISPUTE_OVERDUE])
    issue = loaded.issues.get(code="total_mismatch")
    dispute, _ = workflow.create_draft(loaded, issue.document, [issue.pk], user, use_ai=False)
    dispute.vendor_email = "billing@atlas.example"
    dispute.save()
    sending.send(dispute, approver)
    Dispute.objects.filter(pk=dispute.pk).update(follow_up_on=timezone.localdate() - timedelta(days=3))
    with django_capture_on_commit_callbacks(execute=True):
        flag_overdue()
    card = hooks.json()["attachments"][0]["content"]
    assert card["body"][0]["text"] == f"{dispute.reference}: no answer from {dispute.vendor_name}"
    assert "3 days late" in card["body"][1]["text"]


# ---------------------------------------------------------------- delivery, retry, failure isolation


def _delivery(org, channel, title="Hello"):
    msg = Message(events.BILL_FAILED, title, "text", [], "https://sm.example/x")
    return Delivery.objects.create(organization=org, channel=channel, event=msg.event, title=msg.title,
                                   message=msg.as_dict())


@pytest.mark.django_db
def test_retries_on_rate_limit_then_delivers(org, hooks):
    from apps.notifications.tasks import send_delivery

    ch = _channel(org)
    hooks.responses = [httpx.Response(429, headers={"Retry-After": "7"}, text="rate_limited"),
                       httpx.Response(503, text="busy")]
    d = _delivery(org, ch)
    send_delivery.delay(d.pk)  # eager in tests: retries run inline
    d.refresh_from_db()
    assert d.status == Delivery.Status.SENT and d.attempts == 3 and d.http_status == 200
    assert len(hooks.requests) == 3


def test_backoff_grows_and_respects_retry_after():
    assert [delivery_mod.backoff(n) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert delivery_mod.backoff(1, retry_after=90) == 90
    assert delivery_mod.backoff(1, retry_after=99999) == delivery_mod.RETRY_AFTER_MAX
    assert delivery_mod.backoff(20) == delivery_mod.BACKOFF_MAX


@pytest.mark.django_db
def test_gives_up_after_max_attempts_and_records_why(org, hooks):
    from apps.notifications.tasks import send_delivery

    ch = _channel(org, "teams")
    hooks.responses = [httpx.Response(502) for _ in range(10)]
    d = _delivery(org, ch)
    send_delivery.delay(d.pk)
    d.refresh_from_db()
    assert d.status == Delivery.Status.FAILED and d.attempts == delivery_mod.MAX_ATTEMPTS
    assert d.http_status == 502 and "Gave up after 5 attempts" in d.error
    assert len(hooks.requests) == delivery_mod.MAX_ATTEMPTS


@pytest.mark.django_db
def test_single_attempt_mode_schedules_retry_with_backoff(org, hooks):
    ch = _channel(org)
    hooks.responses = [httpx.Response(429, headers={"Retry-After": "120"})]
    d = _delivery(org, ch)
    outcome = delivery_mod.attempt(d)
    assert outcome.retry_in == 120 and d.status == Delivery.Status.RETRYING and d.next_attempt_at


@pytest.mark.django_db
def test_permanent_errors_fail_without_retry(org, hooks):
    ch = _channel(org)
    hooks.responses = [httpx.Response(404, text="no_service")]
    d = _delivery(org, ch)
    delivery_mod.attempt(d)
    assert d.status == Delivery.Status.FAILED and d.attempts == 1 and "no longer exists" in d.error
    hooks.responses = [httpx.Response(302, headers={"Location": "https://evil.example/"})]
    d2 = _delivery(org, ch)
    delivery_mod.attempt(d2)
    assert d2.status == Delivery.Status.FAILED and "redirect" in d2.error


@pytest.mark.django_db
def test_network_errors_are_retried(org, monkeypatch):
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(delivery_mod, "_TRANSPORT", httpx.MockTransport(down))
    d = _delivery(org, _channel(org))
    out = delivery_mod.attempt(d)
    assert out.retry_in == 30 and d.status == Delivery.Status.RETRYING and "connect" in d.error.lower()


@pytest.mark.django_db
def test_alert_failure_never_breaks_the_action(client, org, loaded, approver, monkeypatch,
                                               django_capture_on_commit_callbacks):
    _channel(org, events_=[events.NEEDS_REVIEW, events.READY, events.BILL_FAILED])

    def explode(*a, **k):
        raise RuntimeError("Slack client exploded")

    monkeypatch.setattr(delivery_mod, "attempt", explode)
    monkeypatch.setattr("apps.notifications.events.shipment_message", explode)
    issue = loaded.issues.get(code="total_mismatch")
    client.force_login(approver)
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(reverse("review:resolve_issue", args=[issue.pk]), {"note": "Checked with the forwarder."})
        audit(org, "bill.failed", issue.document, error="x")
    assert r.status_code == 302
    issue.refresh_from_db()
    assert issue.resolved


@pytest.mark.django_db
def test_email_channel_sends_html_and_text(org, mailoutbox, settings):
    settings.SITE_URL = "https://shipmatch.example.com"
    ch = _channel(org, "email")
    d = _delivery(org, ch, title="Daily summary for Test Imports")
    delivery_mod.attempt(d)
    assert d.status == Delivery.Status.SENT
    m = mailoutbox[0]
    assert m.to == ["controller@test.example", "ap@test.example"]
    assert m.subject == "[ShipMatch] Daily summary for Test Imports"
    html, mimetype = m.alternatives[0]
    assert mimetype == "text/html" and "Daily summary for Test Imports" in html and "https://sm.example/x" in html
    assert "https://sm.example/x" in m.body


@pytest.mark.django_db
def test_stalled_deliveries_are_picked_up(org, hooks):
    from datetime import timedelta

    from django.utils import timezone

    from apps.notifications.tasks import retry_stalled_deliveries

    d = _delivery(org, _channel(org))
    Delivery.objects.filter(pk=d.pk).update(created_at=timezone.now() - timedelta(hours=1))
    fresh = _delivery(org, _channel(org))
    assert retry_stalled_deliveries() == 1
    d.refresh_from_db()
    fresh.refresh_from_db()
    assert d.status == Delivery.Status.SENT and fresh.status == Delivery.Status.PENDING


# ---------------------------------------------------------------- daily summary


@pytest.mark.django_db
def test_daily_summary_at_local_hour_once_a_day(org, loaded, hooks):
    org.timezone = "Asia/Karachi"  # UTC+5
    org.save()
    NotificationSettings.objects.create(organization=org, digest_hour=8)
    _channel(org, events_=[events.DIGEST])
    _channel(org, events_=[events.READY])  # not subscribed to the summary
    doc = loaded.documents.filter(doc_type="freight_invoice").first()
    PostedBill.objects.create(organization=org, document=doc, shipment=loaded, request_id="r-1", status="failed")
    utc = ZoneInfo("UTC")
    assert digest.send_due_digests(now=datetime(2026, 10, 3, 2, 30, tzinfo=utc)) == 0   # 07:30 in Karachi
    assert hooks.requests == []
    assert digest.send_due_digests(now=datetime(2026, 10, 3, 3, 10, tzinfo=utc)) == 1   # 08:10
    assert digest.send_due_digests(now=datetime(2026, 10, 3, 4, 10, tzinfo=utc)) == 0   # same day: once only
    body = hooks.json()
    assert body["blocks"][0]["text"]["text"] == "Daily summary for Test Imports"
    facts = dict(f["text"].replace("*", "").split("\n") for f in body["blocks"][2]["fields"])
    assert facts["Shipments needing review"].startswith("1")
    assert facts["Bills that failed to post"] == "1"
    assert facts["Money at risk on open issues"] == "USD 100.00"
    assert facts["Disputes past follow-up"] == "0"
    assert len(hooks.requests) == 1
    assert NotificationSettings.objects.get(organization=org).last_digest_on.isoformat() == "2026-10-03"
    # Next day, inside the catch-up window.
    assert digest.send_due_digests(now=datetime(2026, 10, 4, 5, 0, tzinfo=utc)) == 1     # 10:00
    assert len(hooks.requests) == 2


@pytest.mark.django_db
def test_daily_summary_skipped_when_nothing_to_report(org, hooks, mailoutbox):
    NotificationSettings.objects.create(organization=org, digest_hour=9)
    _channel(org, "email", events_=[events.DIGEST])
    d = digest.build(org)
    assert d.empty
    assert digest.send_due_digests(now=datetime(2026, 10, 3, 9, 5, tzinfo=ZoneInfo("UTC"))) == 0
    assert mailoutbox == [] and not Delivery.objects.exists()


@pytest.mark.django_db
def test_daily_summary_mixed_currencies_without_rate(org, loaded):
    issue = loaded.issues.get(code="total_mismatch")
    issue.currency = "EUR"
    issue.save()
    d = digest.build(org)
    assert d.at_risk == {"EUR": Decimal("100.00")} and d.at_risk_home is None
    facts = dict(digest.message(org, d).facts)
    assert facts["Money at risk on open issues"] == "EUR 100.00"


# ---------------------------------------------------------------- settings page


@pytest.mark.django_db
def test_alert_settings_are_for_admins(client, user, approver):
    for u in (user, approver):
        client.force_login(u)
        assert client.get(reverse("notifications:settings")).status_code == 403
        assert client.post(reverse("notifications:channel_new"), {"kind": "slack"}).status_code == 403


@pytest.mark.django_db
def test_admin_adds_slack_channel_and_sends_test(client, org, admin_user, hooks):
    client.force_login(admin_user)
    assert client.get(reverse("notifications:settings")).status_code == 200
    assert client.get(reverse("notifications:channel_new") + "?kind=teams").status_code == 200
    r = client.post(reverse("notifications:channel_new") + "?kind=slack", {
        "kind": "slack", "name": "AP team", "webhook_url": SLACK_URL, "enabled": "on",
        "events": [events.BILL_FAILED, events.DISPUTE_OVERDUE, "made.up"]})
    assert r.status_code == 302
    ch = Channel.objects.get()
    assert ch.webhook_url == SLACK_URL and ch.events == [events.BILL_FAILED, events.DISPUTE_OVERDUE]
    from django.db import connection
    with connection.cursor() as cur:
        cur.execute("SELECT webhook_url FROM notifications_channel WHERE id = %s", [ch.pk])
        assert "hooks.slack.com" not in cur.fetchone()[0]  # encrypted at rest
    created = AuditEvent.objects.get(action="notification_channel.created")
    assert "hooks.slack.com" not in json.dumps(created.data)

    r = client.post(reverse("notifications:channel_test", args=[ch.pk]), follow=True)
    assert "Test message delivered" in r.content.decode()
    assert hooks.json()["blocks"][0]["text"]["text"] == "Test message from ShipMatch"
    d = Delivery.objects.get(is_test=True)
    assert d.status == "sent" and d.http_status == 200
    page = client.get(reverse("notifications:settings")).content.decode()
    assert "Test message from ShipMatch" in page and "HTTP 200" in page and SLACK_URL not in page

    # Editing without a new address keeps the saved one.
    client.post(reverse("notifications:channel_edit", args=[ch.pk]), {"name": "AP team", "webhook_url": "",
                                                                       "events": [events.READY]})
    ch.refresh_from_db()
    assert ch.webhook_url == SLACK_URL and ch.events == [events.READY] and not ch.enabled

    hooks.responses = [httpx.Response(410, text="channel_is_archived")]
    r = client.post(reverse("notifications:channel_test", args=[ch.pk]), follow=True)
    assert "archived" in r.content.decode()

    client.post(reverse("notifications:channel_delete", args=[ch.pk]))
    assert not Channel.objects.exists()


@pytest.mark.django_db
def test_channel_form_rejects_unsafe_webhook_and_bad_emails(client, org, admin_user, hooks):
    client.force_login(admin_user)
    r = client.post(reverse("notifications:channel_new"), {
        "kind": "teams", "name": "Ops", "webhook_url": "https://169.254.169.254/latest/meta-data",
        "events": [events.READY]})
    assert r.status_code == 400 and "is not a Teams webhook address" in r.content.decode()
    r = client.post(reverse("notifications:channel_new"), {
        "kind": "email", "name": "Finance", "email_recipients": "cfo@test.example, nope", "events": [events.DIGEST]})
    assert r.status_code == 400 and "not a valid email address" in r.content.decode()
    r = client.post(reverse("notifications:channel_new"), {"kind": "email", "name": "Finance",
                                                           "email_recipients": "cfo@test.example"})
    assert r.status_code == 400 and "at least one alert" in r.content.decode()
    assert not Channel.objects.exists() and hooks.requests == []


@pytest.mark.django_db
def test_digest_hour_setting(client, org, admin_user):
    client.force_login(admin_user)
    client.post(reverse("notifications:settings"), {"digest_hour": "25"})
    assert NotificationSettings.for_org(org).digest_hour == 8
    client.post(reverse("notifications:settings"), {"digest_hour": "6"})
    assert NotificationSettings.for_org(org).digest_hour == 6


@pytest.mark.django_db
def test_notify_without_channels_writes_nothing(org):
    assert notify(org, events.READY, lambda: Message(events.READY, "t", "x")) == []
    assert not Delivery.objects.exists()


@pytest.mark.django_db
def test_eager_mode_does_not_wait_on_timeouts_inline(org, monkeypatch):
    from apps.notifications.tasks import send_delivery

    calls = []

    def slow(request):
        calls.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(delivery_mod, "_TRANSPORT", httpx.MockTransport(slow))
    d = _delivery(org, _channel(org))
    send_delivery.delay(d.pk)
    d.refresh_from_db()
    assert len(calls) == 1 and d.status == Delivery.Status.RETRYING and d.next_attempt_at


@pytest.mark.django_db
def test_other_apps_can_register_alerts(org):
    from apps.core.utils import audit
    from apps.notifications import events

    def build(e):
        return events.Message("x.happened", "Something happened", e.data.get("what", ""), url="https://example.com")

    events.register_event("x.happened", "Something happened", "For tests.", audit_actions=["x.done"], builder=build)
    try:
        assert "x.happened" in events.EVENT_KEYS and "x.happened" in events.DEFAULT_EVENTS["slack"]
        e = audit(org, "x.done", org, what="it did")
        assert events.audit_message(events.AUDIT_EVENTS[e.action], e).text == "it did"
    finally:
        events.EVENTS[:] = [t for t in events.EVENTS if t[0] != "x.happened"]
        events.EVENT_LABELS.pop("x.happened", None)
        events.EVENT_KEYS.remove("x.happened")
        events.AUDIT_EVENTS.pop("x.done", None)
        events.EXTRA_BUILDERS.pop("x.happened", None)
        for kinds in events.DEFAULT_EVENTS.values():
            if "x.happened" in kinds:
                kinds.remove("x.happened")
