"""Team workflow: bulk actions through the single-approval rules, keyboard shortcuts, assignment and its
rules, comments with @mentions, the notification bell, approval links in alerts, and the firm view."""
import json
import re
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.urls import reverse
from django.utils import timezone

from apps.accounting.models import QBOConnection
from apps.accounts.services import mfa
from apps.core.models import AuditEvent, Membership, Organization
from apps.core.utils import audit
from apps.disputes.models import Dispute
from apps.documents.services.ingest import ingest_bytes
from apps.notifications import delivery as delivery_mod
from apps.notifications import events
from apps.notifications.models import Channel
from apps.shipments.models import Approval, Shipment, ValidationIssue
from apps.shipments.services.approval import approval_blockers
from apps.shipments.services.validation import update_status
from apps.workflow.models import Assignment, AssignmentRules, Comment, Notification, Preference, VendorRule
from apps.workflow.services import assignment, comments, decisions, links, notify
from tests.conftest import PASSWORD

SLACK_URL = "https://hooks.slack.com/services/T0000/B0000/abcdefghijklmnop"


def _ready(org, n=1, **kw):
    return [Shipment.objects.create(organization=org, status=Shipment.Status.READY, **kw) for _ in range(n)]


def _member(org, username, role, limit=None):
    u = get_user_model().objects.filter(username=username).first() or get_user_model().objects.create_user(
        username, email=f"{username}@example.com", password=PASSWORD, first_name=username.capitalize())
    Membership.objects.create(user=u, organization=org, role=role, approval_limit=limit)
    return u


def _batch_events(batch_id):
    return list(AuditEvent.objects.filter(data__batch_id=batch_id).order_by("id"))


def _batch_id(response):
    return re.search(r"/work/batches/([0-9a-f]{32})/", response["Location"]).group(1)


class Hooks:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(200, text="ok")

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def hooks(monkeypatch, settings):
    settings.SITE_URL = "https://shipmatch.example.com"
    h = Hooks()
    monkeypatch.setattr(delivery_mod, "_TRANSPORT", httpx.MockTransport(h))
    return h


@pytest.fixture
def loaded(org, dataset):
    for f in ["S03_1_commercial_invoice.pdf", "S03_2_bill_of_lading.pdf", "S03_3_freight_invoice.pdf",
              "S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    return org


@pytest.fixture
def other_org(db):
    return Organization.objects.create(name="Other Client", slug="other")


# ================================================================== 1. bulk actions


@pytest.mark.django_db
def test_bulk_approve_confirms_first_then_writes_one_audit_row_per_shipment(client, org, approver):
    ships = _ready(org, 3)
    client.force_login(approver)
    ids = [str(s.pk) for s in ships]
    r = client.post(reverse("workflow:bulk"), {"action": "approve", "ids": ids, "next": "/review/?status=ready"})
    assert r.status_code == 200
    assert "Approve 3 shipments?" in r.content.decode() and "Will be approved" in r.content.decode()
    assert not Approval.objects.exists()
    assert Shipment.objects.filter(status="approved").count() == 0

    r = client.post(reverse("workflow:bulk"), {"action": "approve", "ids": ids, "confirm": "1", "note": "Weekly run",
                                               "next": "/review/?status=ready"})
    assert r.status_code == 302
    batch_id = _batch_id(r)
    assert Shipment.objects.filter(pk__in=ids, status="approved").count() == 3
    rows = _batch_events(batch_id)
    assert [e.action for e in rows] == ["shipment.approved"] * 3
    assert sorted(e.object_id for e in rows) == sorted(ids)
    assert all(e.data["via"] == "bulk" and e.data["note"] == "Weekly run" and e.actor == approver for e in rows)
    page = client.get(r["Location"])
    assert page.status_code == 200 and page.content.decode().count("Approved") >= 3


@pytest.mark.django_db
def test_bulk_approve_applies_every_single_approval_rule(client, org, loaded, user, approver, approver2):
    clean = Shipment.objects.exclude(issues__code="total_mismatch").get(organization=org)
    assert clean.status == Shipment.Status.READY
    # Approval limit: the clean shipment (with real invoices) is above approver's limit.
    Membership.objects.filter(user=approver, organization=org).update(approval_limit=Decimal("1.00"))
    # Maker-checker: approver resolved an issue on this one.
    made, held, errored, ok = _ready(org, 4)
    issue = ValidationIssue.objects.create(organization=org, shipment=made, code="missing_bl", severity="warning",
                                           message="x", fingerprint="f", resolved=True, resolved_by=approver)
    audit(org, "issue.resolved", issue, actor=approver, code="missing_bl", severity="warning")
    # Dispute hold: a sent dispute waits for the vendor.
    Dispute.objects.create(organization=org, vendor_name="V", vendor_key="v", status=Dispute.Status.SENT,
                           shipment=held, shipment_reference=held.reference)
    # Open error.
    ValidationIssue.objects.create(organization=org, shipment=errored, code="total_mismatch", severity="error",
                                   message="x", fingerprint="g")
    selected = [clean, made, held, errored, ok]
    expected = {s.pk: approval_blockers(s, approver) for s in selected}   # what the shipment page would say
    assert all(expected[s.pk] for s in selected if s != ok) and expected[ok.pk] == []

    client.force_login(approver)
    r = client.post(reverse("workflow:bulk"), {"action": "approve", "ids": [s.pk for s in selected]})
    body = r.content.decode()
    assert "1 will be approved" in body and "4 will be skipped" in body
    assert "above your approval limit" in body and "maker-checker" in body and "waiting for the vendor" in body
    r = client.post(reverse("workflow:bulk"), {"action": "approve", "ids": [s.pk for s in selected], "confirm": "1"})
    batch_id = _batch_id(r)
    for s in selected:
        s.refresh_from_db()
    assert ok.status == "approved"
    assert {s.status for s in (clean, made, held, errored)} == {"ready"}
    rows = _batch_events(batch_id)
    assert len(rows) == 5 and len({e.object_id for e in rows}) == 5          # one row per shipment
    skipped = {int(e.object_id): e.data["reasons"] for e in rows if e.action == "shipment.bulk_skipped"}
    assert skipped == {pk: reasons for pk, reasons in expected.items() if reasons}
    page = client.get(r["Location"]).content.decode()
    assert "Skipped" in page and "approval limit" in page

    # The same shipments through the single-approval button give the same answer.
    client.post(reverse("review:approve", args=[made.pk]))
    made.refresh_from_db()
    assert made.status == "ready"


@pytest.mark.django_db
def test_bulk_actions_check_role_and_organization(client, org, other_org, user, viewer, approver):
    mine = _ready(org, 1)[0]
    theirs = _ready(other_org, 1)[0]
    client.force_login(user)  # reviewer: may not approve
    assert client.post(reverse("workflow:bulk"), {"action": "approve", "ids": [mine.pk]}).status_code == 403
    client.force_login(viewer)
    assert client.post(reverse("workflow:bulk"), {"action": "assign", "ids": [mine.pk],
                                                  "assignee": "none"}).status_code == 403
    client.force_login(approver)
    r = client.post(reverse("workflow:bulk"), {"action": "approve", "ids": [theirs.pk], "confirm": "1"}, follow=True)
    assert "None of the selected shipments are in Test Imports" in r.content.decode()
    theirs.refresh_from_db()
    assert theirs.status == "ready" and not Approval.objects.exists()
    r = client.post(reverse("workflow:bulk"), {"action": "approve", "ids": []}, follow=True)
    assert "Select at least one shipment" in r.content.decode()


@pytest.mark.django_db
def test_two_factor_policy_applies_to_bulk_and_single_approval(client, org, other_org, approver):
    s = _ready(org, 1)[0]
    org.require_mfa = True
    org.save()
    reasons = decisions.approve(s, approver).reasons
    assert any("requires two-factor" in r for r in reasons)
    assert any("requires two-factor" in r for r in approval_blockers(s, approver))
    # Reached from another organization (a firm working in other_org): still refused.
    _member(other_org, "approver", Membership.Role.APPROVER)
    client.force_login(approver)
    client.get(reverse("core:dashboard") + "?org=other")
    r = client.post(reverse("review:approve", args=[s.pk]))
    s.refresh_from_db()
    assert s.status == "ready"
    # In the organization itself the sign-in rule sends the person to set up two-factor first.
    client.get(reverse("core:dashboard") + "?org=test")
    r = client.post(reverse("workflow:bulk"), {"action": "approve", "ids": [s.pk], "confirm": "1"})
    assert r.status_code == 302 and r["Location"] == reverse("accounts:security")
    s.refresh_from_db()
    assert s.status == "ready"
    # With two-factor on, it goes through.
    profile = mfa.profile_for(approver)
    profile.mfa_enabled_at = timezone.now()
    profile.save()
    assert decisions.approve(s, approver).ok


@pytest.mark.django_db
def test_bulk_assign_and_bulk_post(client, org, user, approver, monkeypatch, mailoutbox,
                                   django_capture_on_commit_callbacks):
    a, b = _ready(org, 2)
    client.force_login(approver)
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(reverse("workflow:bulk"), {"action": "assign", "ids": [a.pk, b.pk], "assignee": user.pk,
                                                   "next": "/review/?status=ready"})
    assert r.status_code == 302 and r["Location"] == "/review/?status=ready"
    assert set(Assignment.objects.values_list("assignee", flat=True)) == {user.pk}
    rows = AuditEvent.objects.filter(action="shipment.assigned")
    assert rows.count() == 2 and len({e.data["batch_id"] for e in rows}) == 1
    assert Notification.objects.filter(user=user, kind="assigned").count() == 2
    assert len(mailoutbox) == 2 and "assigned" in mailoutbox[0].subject

    # Post: approved shipments only, QuickBooks must be connected.
    queued = []
    from apps.accounting import tasks

    monkeypatch.setattr(tasks.post_shipment_task, "delay", lambda *args: queued.append(args))
    a.status = Shipment.Status.APPROVED
    a.save()
    r = client.post(reverse("workflow:bulk"), {"action": "post", "ids": [a.pk, b.pk], "confirm": "1"}, follow=True)
    assert "Connect QuickBooks" in r.content.decode() and queued == []
    QBOConnection.objects.create(organization=org, realm_id="1", access_token="a", refresh_token="r",
                                 access_expires_at=timezone.now() + timedelta(hours=1))
    r = client.post(reverse("workflow:bulk"), {"action": "post", "ids": [a.pk, b.pk], "confirm": "1"})
    assert queued == [(a.pk, approver.pk)]
    rows = _batch_events(_batch_id(r))
    assert [(e.action, int(e.object_id)) for e in rows] == [("shipment.post_requested", a.pk),
                                                           ("shipment.bulk_skipped", b.pk)]
    assert "Only approved shipments can be posted" in rows[1].data["reasons"][0]


# ================================================================== 2. keyboard shortcuts


@pytest.mark.django_db
def test_shortcuts_help_renders_and_can_be_turned_off(client, org, user):
    client.force_login(user)
    r = client.get(reverse("workflow:shortcuts"))
    body = r.content.decode()
    assert r.status_code == 200
    for words in ("<kbd>j</kbd> <kbd>k</kbd>", "<kbd>g</kbd> then <kbd>q</kbd>", "<kbd>?</kbd>",
                  "A confirmation opens first", "never fire while you type"):
        assert words in body
    page = client.get(reverse("review:queue")).content.decode()
    assert '<meta name="wf-shortcuts" content="on">' in page and 'id="wf-help"' in page
    assert "js/workflow.js" in page and "<script>" not in page.replace('<script src=', '')
    client.post(reverse("workflow:shortcuts"), {"shortcuts": "off", "next": "/review/"})
    assert Preference.objects.get(user=user).shortcuts is False
    assert '<meta name="wf-shortcuts" content="off">' in client.get(reverse("review:queue")).content.decode()
    client.post(reverse("workflow:shortcuts"), {"full": "1", "shortcuts": "on", "email_mentions": "on"})
    p = Preference.objects.get(user=user)
    assert p.shortcuts and p.email_mentions and not p.email_assignments


def test_shortcut_script_ignores_typing_and_modifiers():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "static" / "js" / "workflow.js").read_text()
    assert "typing(e.target) || typing(document.activeElement)" in js
    assert "e.ctrlKey || e.metaKey || e.altKey" in js
    assert 'getAttribute("content") === "on"' in js


@pytest.mark.django_db
def test_shipment_page_has_confirm_dialog_and_neighbours(client, org, approver):
    a, b, c = _ready(org, 3)
    client.force_login(approver)
    body = client.get(reverse("review:shipment", args=[b.pk])).content.decode()
    assert 'id="wf-approve-dialog"' in body and f"Approve {b.reference}?" in body
    assert "data-wf-prev" in body and "data-wf-next" in body
    assert b.status == "ready"


# ================================================================== 3. assignment


@pytest.mark.django_db
def test_assign_reassign_unassign_are_audited_and_alert_the_assignee(client, org, user, viewer, approver, hooks,
                                                                     mailoutbox, django_capture_on_commit_callbacks):
    Channel.objects.create(organization=org, kind="slack", name="AP", webhook_url=SLACK_URL,
                           events=[notify.ASSIGNED_EVENT])
    s = _ready(org, 1)[0]
    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("workflow:assign", args=[s.pk]), {"assignee": approver.pk})
    assert Assignment.objects.get(shipment=s).assignee == approver
    e = AuditEvent.objects.get(action="shipment.assigned")
    assert e.actor == user and e.data["assignee"] == "approver" and e.data["previous"] == ""
    n = Notification.objects.get(user=approver)
    assert n.kind == "assigned" and s.reference in n.title
    assert mailoutbox[-1].to == ["approver@example.com"] and "?org=test" in mailoutbox[-1].body
    assert hooks.bodies()[-1]["blocks"][0]["text"]["text"] == f"{s.reference} assigned to approver"

    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("workflow:assign", args=[s.pk]), {"assignee": "me"})
    e = AuditEvent.objects.filter(action="shipment.assigned").first()
    assert e.data["previous"] == "approver" and e.data["assignee"] == "reviewer"
    assert not Notification.objects.filter(user=user).exists()  # assigning yourself notifies nobody
    client.post(reverse("workflow:assign", args=[s.pk]), {"assignee": "none"})
    assert Assignment.objects.get(shipment=s).assignee is None
    assert AuditEvent.objects.filter(action="shipment.unassigned").count() == 1
    other = _ready(org, 1)[0]
    assert assignment.assign(other, None, user).reasons == [f"{other.reference} is already unassigned."]
    assert not Assignment.objects.filter(shipment=other).exists()

    # Only reviewers, approvers and admins of this organization can be given work; viewers can't assign.
    r = client.post(reverse("workflow:assign", args=[s.pk]), {"assignee": viewer.pk}, follow=True)
    assert "Choose a reviewer, approver or admin" in r.content.decode()
    client.force_login(viewer)
    assert client.post(reverse("workflow:assign", args=[s.pk]), {"assignee": "me"}).status_code == 403


@pytest.mark.django_db
def test_round_robin_takes_turns_and_respects_manual_unassign(org, user, approver, django_capture_on_commit_callbacks):
    second = _member(org, "reviewer2", Membership.Role.REVIEWER)
    AssignmentRules.objects.create(organization=org, mode=AssignmentRules.Mode.ROUND_ROBIN)
    got = []
    for _ in range(3):
        s = Shipment.objects.create(organization=org)
        with django_capture_on_commit_callbacks(execute=True):
            update_status(s)  # open -> ready: the rules run once the change is committed
        got.append(Assignment.objects.get(shipment=s).assignee)
    assert got == [user, second, user]                      # reviewers only, in turn
    e = AuditEvent.objects.filter(action="shipment.assigned").first()
    assert e.actor is None and e.data["reason"] == "round_robin"

    s = Shipment.objects.create(organization=org)
    Assignment.objects.create(organization=org, shipment=s, assignee=None)  # someone unassigned it on purpose
    with django_capture_on_commit_callbacks(execute=True):
        update_status(s)
    assert Assignment.objects.get(shipment=s).assignee is None

    rules = AssignmentRules.for_org(org)
    rules.include_approvers = True
    rules.save()
    assert approver in assignment.turn_pool(org, rules)


@pytest.mark.django_db
def test_vendor_rules_with_and_without_fallback(org, loaded, user, approver):
    s = Shipment.objects.filter(organization=org).first()
    keys = assignment.shipment_vendor_keys(s)
    assert keys
    Assignment.objects.filter(shipment=s).delete()
    rules = AssignmentRules.objects.create(organization=org, mode=AssignmentRules.Mode.VENDOR, vendor_fallback=False)
    VendorRule.objects.create(organization=org, vendor_key=keys[0], vendor_name="Vendor", assignee=approver)
    assert assignment.auto_assign(s).ok
    a = Assignment.objects.get(shipment=s)
    assert a.assignee == approver and a.reason == "vendor"

    VendorRule.objects.all().delete()
    a.delete()
    assert assignment.auto_assign(s) is None                # no rule and no fallback: left for a person
    rules.vendor_fallback = True
    rules.save()
    assert assignment.auto_assign(s).ok
    assert Assignment.objects.get(shipment=s).assignee == user and Assignment.objects.get(shipment=s).reason == "round_robin"


@pytest.mark.django_db
def test_assigned_to_me_filter_and_sidebar_count(client, org, user, approver):
    mine, theirs, nobody = _ready(org, 3)
    assignment.assign(mine, user, approver)
    assignment.assign(theirs, approver, approver)
    client.force_login(user)
    def listed(query):
        body = client.get(reverse("review:queue") + query).content.decode()
        return set(re.findall(r'class="row-link" href="[^"]+">(SHP-\d+)</a>', body)), body

    refs, body = listed("?status=ready&assigned=me")
    assert refs == {mine.reference}
    assert "Assigned to me" in body and 'class="wf-chip on"' in body
    assert listed("?status=ready&assigned=none")[0] == {nobody.reference}
    assert listed(f"?status=ready&assigned={approver.pk}")[0] == {theirs.reference}
    nav = re.search(r'Assigned to me<span class="count"[^>]*>(\d+)</span>', client.get(reverse("core:dashboard"))
                    .content.decode())
    assert nav and nav.group(1) == "1"


@pytest.mark.django_db
def test_assignment_settings_for_admins(client, org, user, admin_user, approver):
    client.force_login(user)
    assert client.get(reverse("workflow:assignment_settings")).status_code == 403
    client.force_login(admin_user)
    assert client.get(reverse("workflow:assignment_settings")).status_code == 200
    client.post(reverse("workflow:assignment_settings"), {"mode": "vendor", "vendor_fallback": "on"})
    rules = AssignmentRules.objects.get(organization=org)
    assert rules.mode == "vendor" and rules.vendor_fallback and not rules.include_approvers
    assert AuditEvent.objects.filter(action="assignment_rules.updated").exists()
    client.post(reverse("workflow:vendor_rule_add"), {"vendor_name": "Harborlink Logistics, LLC", "assignee": approver.pk})
    rule = VendorRule.objects.get(organization=org)
    assert rule.vendor_key == "harborlink logistics" and rule.assignee == approver
    _ready(org, 2)
    r = client.post(reverse("workflow:assign_waiting"), follow=True)
    assert "Assigned 2 waiting shipments" in r.content.decode()


# ================================================================== 4. comments and mentions


@pytest.mark.django_db
def test_mentions_only_reach_members_of_the_organization(client, org, other_org, user, approver, mailoutbox,
                                                         django_capture_on_commit_callbacks):
    outsider = _member(other_org, "outsider", Membership.Role.APPROVER)
    s = _ready(org, 1)[0]
    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("workflow:comment_create"), {
            "target": f"shipment:{s.pk}", "body": "@approver please check. Also @outsider and @nobody.",
            "next": reverse("review:shipment", args=[s.pk])})
    c = Comment.objects.get()
    assert list(c.mentions.all()) == [approver]
    assert Notification.objects.filter(user=approver, kind="mention").count() == 1
    assert not Notification.objects.filter(user=outsider).exists()
    assert [m.to for m in mailoutbox] == [["approver@example.com"]]
    body = client.get(reverse("review:shipment", args=[s.pk])).content.decode()
    assert 'class="wf-mention" title="approver">@approver</span>' in body
    assert "@outsider" in body and 'title="outsider"' not in body

    # Autocomplete lists members of this organization only, and only to its members.
    found = client.get(reverse("workflow:members") + f"?for={org.pk}&q=appr").json()["members"]
    assert [m["username"] for m in found] == ["approver"]
    assert client.get(reverse("workflow:members") + f"?for={org.pk}&q=outs").json()["members"] == []
    assert client.get(reverse("workflow:members") + f"?for={other_org.pk}&q=o").status_code == 404
    assert comments.parse_mentions(org, "hi @outsider") == []


@pytest.mark.django_db
def test_comment_edit_and_delete_window(client, org, user, approver, django_capture_on_commit_callbacks):
    s = _ready(org, 1)[0]
    c = comments.create(org, user, "First version", shipment=s)
    client.force_login(approver)
    r = client.post(reverse("workflow:comment_edit", args=[c.pk]), {"body": "Hijacked"}, follow=True)
    assert "only edit your own" in r.content.decode()
    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("workflow:comment_edit", args=[c.pk]), {"body": "Second version @approver"})
    c.refresh_from_db()
    assert c.body == "Second version @approver" and c.edited_at is not None
    e = AuditEvent.objects.get(action="comment.edited")
    assert e.data["previous"] == "First version" and e.data["mentioned_names"] == ["approver"]
    assert Notification.objects.filter(user=approver, kind="mention").count() == 1  # newly mentioned on edit

    Comment.objects.filter(pk=c.pk).update(created_at=timezone.now() - timedelta(minutes=16))
    r = client.post(reverse("workflow:comment_edit", args=[c.pk]), {"body": "Too late"}, follow=True)
    assert "15 minutes" in r.content.decode()
    client.post(reverse("workflow:comment_delete", args=[c.pk]))
    c.refresh_from_db()
    assert c.body == "Second version @approver" and c.deleted_at is None

    fresh = comments.create(org, user, "Delete me", shipment=s)
    client.post(reverse("workflow:comment_delete", args=[fresh.pk]))
    fresh.refresh_from_db()
    assert fresh.deleted_at is not None and fresh.body == ""
    assert AuditEvent.objects.get(action="comment.deleted").data["previous"] == "Delete me"


@pytest.mark.django_db
def test_replies_documents_and_roles(client, org, loaded, user, viewer, approver):
    s = Shipment.objects.filter(organization=org).first()
    doc = s.documents.first()
    top = comments.create(org, approver, "Is this the final invoice?", document=doc)
    client.force_login(user)
    client.post(reverse("workflow:comment_create"), {"target": f"document:{doc.pk}", "parent": top.pk,
                                                     "body": "Yes, confirmed by email."})
    reply = Comment.objects.get(parent=top)
    assert reply.document == doc
    assert Notification.objects.get(user=approver).kind == "reply"
    body = client.get(reverse("review:shipment", args=[s.pk])).content.decode()
    assert "Is this the final invoice?" in body and "Yes, confirmed by email." in body and doc.original_filename in body
    client.force_login(viewer)
    r = client.post(reverse("workflow:comment_create"), {"target": f"shipment:{s.pk}", "body": "Hello"}, follow=True)
    assert "read comments but not write them" in r.content.decode()
    assert Comment.objects.count() == 2


@pytest.mark.django_db
def test_comments_on_a_document_outside_any_shipment(client, org, dataset, user, approver):
    doc, _ = ingest_bytes(org, "loose.pdf", (dataset / "pdf" / "S01_1_commercial_invoice.pdf").read_bytes(),
                          process="none")
    client.force_login(user)
    page = client.get(reverse("review:document", args=[doc.pk]))
    assert page.status_code == 200 and 'id="comments"' in page.content.decode()
    r = client.post(reverse("workflow:comment_create"), {"target": f"document:{doc.pk}", "body": "@approver whose is this?",
                                                         "next": reverse("review:document", args=[doc.pk])})
    c = Comment.objects.get()
    assert r["Location"] == reverse("review:document", args=[doc.pk]) + f"#comment-{c.pk}"
    assert c.document == doc and c.shipment is None
    n = Notification.objects.get(user=approver)
    assert n.url == reverse("review:document", args=[doc.pk]) + f"#comment-{c.pk}"
    assert "whose is this?" in client.get(reverse("review:document", args=[doc.pk])).content.decode()


# ================================================================== notifications bell


@pytest.mark.django_db
def test_bell_counts_unread_and_open_marks_read(client, org, other_org, user, approver):
    s = _ready(org, 1)[0]
    n = notify.notify_user(user, org, "mention", "Approver mentioned you on SHP", url=f"/review/shipments/{s.pk}/",
                           actor=approver)
    notify.notify_user(user, other_org, "mention", "From an org the user is not in")   # never created
    assert Notification.objects.count() == 1
    client.force_login(user)
    page = client.get(reverse("core:dashboard")).content.decode()
    assert "Notifications, 1 unread" in page
    r = client.post(reverse("workflow:notification_open", args=[n.pk]))
    assert r.status_code == 302 and r["Location"] == f"/review/shipments/{s.pk}/?org=test"
    n.refresh_from_db()
    assert n.read_at is not None
    assert "You're all caught up" in client.get(reverse("workflow:notifications")).content.decode()
    client.force_login(approver)
    assert client.post(reverse("workflow:notification_open", args=[n.pk])).status_code == 404


# ================================================================== 5. approve from email or Slack


@pytest.mark.django_db
def test_approval_link_preselects_and_never_changes_anything_on_get(client, org, approver):
    s = _ready(org, 1)[0]
    url = links.approval_path(s)
    r = client.get(url)
    assert r.status_code == 302 and r["Location"].startswith(reverse("accounts:login"))  # sign in first
    client.force_login(approver)
    before = AuditEvent.objects.count()
    r = client.get(url)
    body = r.content.decode()
    assert r.status_code == 200
    assert f"You were sent here to approve <strong>{s.reference}</strong>" in body
    assert 'name="via" value="link"' in body
    s.refresh_from_db()
    assert s.status == "ready" and not Approval.objects.exists() and AuditEvent.objects.count() == before
    assert client.post(url).status_code == 405

    r = client.post(reverse("workflow:quick_approve", args=[s.pk]), {"via": "link"})
    s.refresh_from_db()
    assert s.status == "approved"
    assert AuditEvent.objects.get(action="shipment.approved").data["via"] == "alert link"


@pytest.mark.django_db
def test_approval_link_expired_tampered_and_other_org(client, org, other_org, approver, monkeypatch, settings):
    s = _ready(org, 1)[0]
    stranger_ship = _ready(other_org, 1)[0]
    client.force_login(approver)
    real_time = signing.time.time
    monkeypatch.setattr(signing.time, "time", lambda: real_time() - 73 * 3600)
    old = links.approval_path(s)
    monkeypatch.setattr(signing.time, "time", real_time)
    r = client.get(old)
    body = r.content.decode()
    assert r.status_code == 200 and "has expired" in body and "You were sent here" not in body
    assert 'name="via" value="link"' not in body

    token = links.make_token(s)
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    r = client.get(reverse("workflow:approval_link", args=[tampered]))
    assert r.status_code == 400 and "isn't valid" in r.content.decode()
    forged = signing.dumps({"o": org.pk, "s": s.pk}, salt="another-purpose")
    assert client.get(reverse("workflow:approval_link", args=[forged])).status_code == 400

    # A genuine link for an organization this person isn't in shows nothing.
    assert client.get(links.approval_path(stranger_ship)).status_code == 404
    stranger_ship.refresh_from_db()
    assert stranger_ship.status == "ready"


@pytest.mark.django_db
def test_approval_link_respects_two_factor(client, org, approver):
    s = _ready(org, 1)[0]
    org.require_mfa = True
    org.save()
    client.force_login(approver)
    r = client.get(links.approval_path(s))
    assert r.status_code == 302 and r["Location"] == reverse("accounts:security")


@pytest.mark.django_db
def test_ready_alert_links_to_the_approval_page(org, loaded, hooks, django_capture_on_commit_callbacks):
    Channel.objects.create(organization=org, kind="slack", name="AP", webhook_url=SLACK_URL, events=[events.READY])
    bad = Shipment.objects.get(organization=org, issues__code="total_mismatch")
    with django_capture_on_commit_callbacks(execute=True):
        bad.issues.update(resolved=True)
        update_status(bad)
    button = hooks.bodies()[-1]["blocks"][-2]["elements"][0]
    assert button["text"]["text"] == "Review and approve"
    assert button["url"].startswith("https://shipmatch.example.com/approve/")
    token = button["url"].rstrip("/").rsplit("/", 1)[1]
    target = links.read_token(token)
    assert (target.org_id, target.shipment_id, target.expired) == (org.pk, bad.pk, False)


# ================================================================== 6. firm view


@pytest.mark.django_db
def test_portfolio_shows_only_member_organizations(client, org, other_org, approver, django_user_model):
    hidden = Organization.objects.create(name="Not Mine Ltd", slug="notmine")
    _member(other_org, "approver", Membership.Role.REVIEWER)
    _ready(org, 2)
    Shipment.objects.create(organization=other_org, status=Shipment.Status.NEEDS_REVIEW)
    Shipment.objects.create(organization=hidden, status=Shipment.Status.NEEDS_REVIEW)
    QBOConnection.objects.create(organization=other_org, realm_id="1", access_token="a", refresh_token="r",
                                 access_expires_at=timezone.now(), needs_reconnect=True)
    client.force_login(approver)
    body = client.get(reverse("workflow:portfolio")).content.decode()
    assert "Test Imports" in body and "Other Client" in body and "Not Mine Ltd" not in body
    assert "Needs reconnecting" in body and 'href="/dashboard/?org=other"' in body
    assert "All clients" in client.get(reverse("core:dashboard")).content.decode()   # sidebar link for firms

    # Superusers see other organizations only in the platform admin, not here.
    root = django_user_model.objects.create_superuser("root", "root@example.com", PASSWORD)
    client.force_login(root)
    body = client.get(reverse("workflow:portfolio")).content.decode()
    start = body.index('<table class="data wf-clients">')
    table = body[start:body.index("</table>", start)]
    assert "Not Mine Ltd" not in table and "Test Imports" not in table
    assert "any organization yet" in table


@pytest.mark.django_db
def test_portfolio_sorting_and_two_factor_lock(org, other_org, approver):
    from apps.workflow.services import portfolio

    _member(other_org, "approver", Membership.Role.APPROVER)
    _ready(other_org, 3)
    _ready(org, 1)
    rows, key, direction = portfolio.sort_rows(portfolio.build(approver), "ready", "")
    assert (key, direction) == ("ready", "desc") and [r.org.slug for r in rows] == ["other", "test"]
    rows, _, _ = portfolio.sort_rows(portfolio.build(approver), "name", "asc")
    assert [r.org.slug for r in rows] == ["other", "test"]
    other_org.require_mfa = True
    other_org.save()
    rows, _, _ = portfolio.sort_rows(portfolio.build(approver), "ready", "desc")
    assert [r.org.slug for r in rows] == ["test", "other"] and rows[1].locked and rows[1].ready == 0


@pytest.mark.django_db
def test_my_work_across_organizations(client, org, other_org, user, approver):
    stranger = Organization.objects.create(name="Not Mine Ltd", slug="notmine")
    _member(other_org, "approver", Membership.Role.APPROVER)
    here, there = _ready(org, 1)[0], _ready(other_org, 1)[0]
    elsewhere = _ready(stranger, 1)[0]
    assigned = Shipment.objects.create(organization=org, status=Shipment.Status.NEEDS_REVIEW)
    assignment.assign(assigned, approver, user)
    prepared = _ready(other_org, 1)[0]   # approver prepared it, so it isn't awaiting their approval
    issue = ValidationIssue.objects.create(organization=other_org, shipment=prepared, code="missing_bl",
                                           severity="warning", message="x", fingerprint="f", resolved=True)
    audit(other_org, "issue.resolved", issue, actor=approver)
    client.force_login(approver)
    body = client.get(reverse("workflow:my_work")).content.decode()
    for s in (here, there, assigned):
        assert s.reference in body
    assert elsewhere.reference not in body and prepared.reference not in body
    assert "Other Client" in body and f'href="/review/shipments/{there.pk}/?org=other"' in body
    body = client.get(reverse("workflow:my_work") + "?kind=assigned").content.decode()
    assert assigned.reference in body and here.reference not in body


@pytest.mark.django_db
def test_firm_admins_create_client_organizations_when_enabled(client, org, user, admin_user, settings):
    settings.FIRM_CAN_CREATE_ORGS = False
    client.force_login(admin_user)
    assert client.post(reverse("workflow:create_org"), {"name": "Harbor Foods"}).status_code == 403
    settings.FIRM_CAN_CREATE_ORGS = True
    org.require_mfa, org.maker_checker = False, False
    org.save()
    r = client.post(reverse("workflow:create_org"), {"name": "Harbor Foods", "home_currency": "eur",
                                                     "timezone": "Europe/Berlin", "copy_from": org.pk})
    new = Organization.objects.get(name="Harbor Foods")
    assert r.status_code == 302 and r["Location"] == f"{reverse('core:team')}?org={new.slug}"
    assert new.home_currency == "EUR" and new.maker_checker is False
    assert Membership.objects.get(organization=new).user == admin_user
    assert Membership.objects.get(organization=new).role == "admin"
    assert AuditEvent.objects.filter(organization=new, action="org.created", actor=admin_user).exists()
    client.post(reverse("workflow:create_org"), {"name": "Harbor Foods", "home_currency": "USD", "timezone": "UTC"})
    assert Organization.objects.filter(slug="harbor-foods-2").exists()
    client.force_login(user)  # a reviewer is not a firm admin
    assert client.post(reverse("workflow:create_org"), {"name": "Sneaky"}).status_code == 403


# --------------------------------------------------------------------------- QA-056: my work does only the work it shows


def test_my_work_works_out_totals_only_for_the_rows_on_the_page(client, org, approver, monkeypatch):
    from apps.shipments.models import Shipment
    from apps.workflow.services import portfolio
    from apps.workflow.views import PER_PAGE

    for i in range(PER_PAGE + 15):
        Shipment.objects.create(organization=org, bl_number=f"MW{i}", status=Shipment.Status.READY)
    calls = []
    real = portfolio.shipment_totals
    monkeypatch.setattr(portfolio, "shipment_totals", lambda s: calls.append(s.pk) or real(s))
    client.force_login(approver)

    first = client.get(reverse("workflow:my_work"))
    assert first.status_code == 200 and first.context["total"] == PER_PAGE + 15
    assert len(calls) == PER_PAGE   # not PER_PAGE + 15

    calls.clear()
    second = client.get(reverse("workflow:my_work"), {"page": 2})
    assert second.status_code == 200 and len(calls) == 15
    assert all(row.totals is not None for row in second.context["page"].object_list)


@pytest.mark.django_db
def test_my_work_checks_approval_rules_in_bulk(client, org, approver):
    """The approval checks for ready shipments cost a fixed number of queries, not a few per shipment."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.shipments.models import Shipment
    from apps.workflow.views import PER_PAGE

    client.force_login(approver)
    client.get(reverse("workflow:my_work"))  # first request of a session does extra bookkeeping

    def queries(n):
        Shipment.objects.filter(organization=org).delete()
        for i in range(n):
            Shipment.objects.create(organization=org, bl_number=f"BK{i}", status=Shipment.Status.READY)
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(reverse("workflow:my_work")).status_code == 200
        return len(ctx.captured_queries)

    assert queries(PER_PAGE + 30) == queries(PER_PAGE)


@pytest.mark.django_db
def test_bulk_approval_checks_match_the_per_shipment_ones(org, approver, user):
    """prefetch_approval must not change the answer: open errors, maker-checker and dispute holds still block."""
    from apps.disputes.models import Dispute
    from apps.shipments.services.approval import approval_blockers, prefetch_approval

    clean, errored, prepared, held = _ready(org, 4)
    ValidationIssue.objects.create(organization=org, shipment=errored, code="x", severity="error", message="x",
                                   fingerprint="e")
    issue = ValidationIssue.objects.create(organization=org, shipment=prepared, code="y", severity="warning",
                                           message="y", fingerprint="p", resolved=True)
    audit(org, "issue.resolved", issue, actor=approver)
    Dispute.objects.create(organization=org, shipment=held, status=Dispute.Status.SENT, vendor_name="V",
                           amount_disputed=10, currency="USD")
    expected = {s.pk: approval_blockers(Shipment.objects.get(pk=s.pk), approver) for s in (clean, errored, prepared, held)}
    fresh = list(Shipment.objects.filter(pk__in=expected).select_related("organization"))
    prefetch_approval(fresh)
    assert {s.pk: approval_blockers(s, approver) for s in fresh} == expected
    assert expected[clean.pk] == [] and all(expected[s.pk] for s in (errored, prepared, held))
