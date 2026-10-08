"""Team workflow screens: bulk actions, assignment, comments, notifications, the focused approval page and
the link in "ready for approval" alerts, keyboard shortcut preferences, and the firm view across clients.

Function views with @login_required, Django messages for feedback and POST-redirect-GET, like the rest.
"""
from __future__ import annotations

import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from apps.core.paging import Paginator
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST, require_safe

from apps.accounting.models import vendor_key
from apps.core import timezones
from apps.core.models import Membership, Organization
from apps.core.permissions import has_perm, membership_for, require
from apps.core.utils import audit, current_org, orgs_for_user, use_org
from apps.documents.models import Document, ExtractedField
from apps.shipments.models import Shipment
from apps.shipments.services.approval import shipment_totals

from .models import AssignmentRules, Comment, Notification, Preference, VendorRule
from .services import assignment, bulk, comments, decisions, links, notify, orgs, portfolio
from .services.notify import display

PER_PAGE = 25
BATCH_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------- helpers


def _back(request, default: str) -> str:
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return nxt
    return default


def _shipment(request, pk, perm: str = "view") -> Shipment:
    s = get_object_or_404(Shipment.objects.filter(organization__in=orgs_for_user(request.user))
                          .select_related("organization"), pk=pk)
    use_org(request, s.organization)
    require(request.user, s.organization, perm)
    return s


def _mfa_gate(request, org):
    """The sign-in rules check the organization in use; pages that switch organization check again."""
    if decisions.mfa_missing(request.user, org):
        messages.warning(request, f"{org.name} requires two-factor authentication. Set it up to continue.")
        return redirect("accounts:security")
    return None


def _person(org, raw: str, me):
    if raw == "me":
        return me if assignment.assignable(org).filter(pk=me.pk).exists() else None
    if raw.isdigit():
        return assignment.assignable(org).filter(pk=int(raw)).first()
    return None


# ---------------------------------------------------------------- bulk actions


@login_required
@require_POST
def bulk_action(request):
    org = current_org(request)
    require(request.user, org, "view")
    back = _back(request, reverse("review:queue"))
    action = request.POST.get("action", "")
    if action not in bulk.ACTIONS:
        messages.error(request, "Choose what to do with the selected shipments.")
        return redirect(back)
    require(request.user, org, bulk.ACTIONS[action])
    ids = bulk.parse_ids(request.POST.getlist("ids"))
    if not ids:
        messages.error(request, "Select at least one shipment first: tick the boxes in the first column.")
        return redirect(back)
    if len(ids) > bulk.max_shipments():
        messages.error(request, f"Select at most {bulk.max_shipments()} shipments at a time.")
        return redirect(back)
    shipments = bulk.selected(org, ids)
    if not shipments:
        messages.error(request, f"None of the selected shipments are in {org.name}. Refresh the page and try again.")
        return redirect(back)

    if action == "assign":
        raw = request.POST.get("assignee", "")
        person = None if raw == "none" else _person(org, raw, request.user)
        if raw != "none" and person is None:
            messages.error(request, "Choose who to assign the shipments to.")
            return redirect(back)
        batch_id, outcomes = bulk.run("assign", org, request.user, shipments, assignee=person, unassign=raw == "none")
        done = sum(1 for o in outcomes if o.ok)
        who = display(person) if person else "nobody"
        if done == len(outcomes):
            messages.success(request, f"Assigned {done} shipment{'s' if done != 1 else ''} to {who}.")
            return redirect(back)
        messages.info(request, f"Assigned {done} of {len(outcomes)} shipments to {who}. The others are listed below.")
        return redirect(f"{reverse('workflow:batch', args=[batch_id])}?next={_quote(back)}")

    if request.POST.get("confirm") != "1":
        previews = bulk.preview(action, request.user, shipments)
        for p in previews:
            p.totals = shipment_totals(p.shipment)
        return render(request, "workflow/bulk_confirm.html", {
            "action": action, "previews": previews, "ready": sum(1 for p in previews if p.ok),
            "skipped": sum(1 for p in previews if not p.ok), "next": back,
            "missing": len(ids) - len(shipments),
        })
    batch_id, outcomes = bulk.run(action, org, request.user, shipments, note=request.POST.get("note", ""))
    done = sum(1 for o in outcomes if o.ok)
    verb = {"approve": "Approved", "post": "Started posting"}[action]
    level = messages.SUCCESS if done == len(outcomes) else (messages.WARNING if done else messages.ERROR)
    messages.add_message(request, level, f"{verb} {done} of {len(outcomes)} shipment{'s' if len(outcomes) != 1 else ''}."
                         + ("" if done == len(outcomes) else " The reasons for the others are below."))
    return redirect(f"{reverse('workflow:batch', args=[batch_id])}?next={_quote(back)}")


def _quote(url: str) -> str:
    from urllib.parse import quote

    return quote(url, safe="/")


@login_required
def batch(request, batch_id):
    if not BATCH_RE.match(batch_id):
        raise Http404
    org = current_org(request)
    require(request.user, org, "view")
    lines = bulk.batch_lines(org, request.user, batch_id)
    if not lines:
        raise Http404("This bulk action isn't in this organization, or it was someone else's.")
    return render(request, "workflow/batch.html", {
        "lines": lines, "done": sum(1 for x in lines if x.ok), "skipped": sum(1 for x in lines if not x.ok),
        "action": next((x.action for x in lines if x.action), ""), "at": lines[0].at, "batch_id": batch_id,
        "back": _back(request, reverse("review:queue")),
    })


# ---------------------------------------------------------------- assignment


@login_required
@require_POST
def assign_shipment(request, pk):
    s = _shipment(request, pk, "edit")
    back = _back(request, reverse("review:shipment", args=[s.pk]))
    raw = request.POST.get("assignee", "")
    if raw in ("", "none"):
        person = None
    else:
        person = _person(s.organization, raw, request.user)
        if person is None:
            messages.error(request, "Choose a reviewer, approver or admin of this organization.")
            return redirect(back)
    outcome = assignment.assign(s, person, request.user)
    if outcome.ok:
        messages.success(request, f"{s.reference} is now assigned to {display(person)}." if person
                         else f"Nobody is assigned to {s.reference} now.")
    else:
        messages.info(request, " ".join(outcome.reasons))
    return redirect(back)


# ---------------------------------------------------------------- focused approval page


def _quick_context(request, s: Shipment, *, via_link: bool = False, link_expired: bool = False) -> dict:
    org = s.organization
    user = request.user
    docs = []
    order = {"bill_of_lading": 0, "commercial_invoice": 1, "freight_invoice": 2}   # as on the shipment page
    for d in sorted(s.documents.prefetch_related("fields"), key=lambda x: (order.get(x.doc_type, 9), x.received_at)):
        docs.append({"doc": d, "vendor": d.field("vendor_name") or d.field("carrier_name") or "",
                     "number": d.field("invoice_number") or d.field("bl_number") or "",
                     "amount": d.field("total_amount"), "currency": d.field("currency") or org.home_currency})
    issues = list(s.issues.filter(resolved=False).select_related("document").order_by("-severity", "id"))
    membership = membership_for(user, org)
    can_approve = has_perm(user, org, "approve")
    blockers = decisions.approve_blockers(s, user) if not s.is_locked and can_approve else []
    return {
        "shipment": s, "totals": shipment_totals(s), "docs": docs, "open_issues": issues,
        "open_errors": sum(1 for i in issues if i.severity == "error"),
        "blockers": blockers, "can_approve": can_approve,
        "limit": membership.approval_limit if membership else None,
        "decision": s.approvals.select_related("user").order_by("-created_at").first(),
        "assignee": assignment.current_assignee(s), "via_link": via_link, "link_expired": link_expired,
        "next_item": _next_to_approve(user, s) if s.is_locked or s.status == Shipment.Status.REJECTED else None,
        "reject_min": _reject_min(),
    }


def _reject_min() -> int:
    from apps.shipments.views import REJECT_NOTE_MIN

    return REJECT_NOTE_MIN


def _next_to_approve(user, current: Shipment) -> Shipment | None:
    """The oldest other shipment in this organization this person can approve now."""
    for s in (Shipment.objects.filter(organization=current.organization, status=Shipment.Status.READY)
              .exclude(pk=current.pk).select_related("organization").order_by("created_at")[:20]):
        if not decisions.approve_blockers(s, user):
            return s
    return None


@login_required
@require_safe
def quick(request, pk):
    s = _shipment(request, pk)
    gate = _mfa_gate(request, s.organization)
    if gate:
        return gate
    return render(request, "workflow/quick.html", _quick_context(request, s))


@login_required
@require_safe
def approval_link(request, token):
    """Where "ready for approval" alerts point. The token only names the shipment: the person must be signed
    in and a member of its organization, and opening the page changes nothing."""
    try:
        target = links.read_token(token)
    except signing.BadSignature:
        return render(request, "workflow/link_invalid.html", status=400)
    s = (Shipment.objects.filter(pk=target.shipment_id, organization_id=target.org_id,
                                 organization__memberships__user=request.user)
         .select_related("organization").first())
    if s is None:
        raise Http404("This shipment isn't in any organization you belong to.")
    use_org(request, s.organization)
    gate = _mfa_gate(request, s.organization)
    if gate:
        return gate
    require(request.user, s.organization, "view")
    ctx = _quick_context(request, s, via_link=not target.expired, link_expired=target.expired)
    return render(request, "workflow/quick.html", ctx)


@login_required
@require_POST
def quick_approve(request, pk):
    s = _shipment(request, pk, "approve")
    gate = _mfa_gate(request, s.organization)
    if gate:
        return gate
    via = "alert link" if request.POST.get("via") == "link" else "approval page"
    outcome = decisions.approve(s, request.user, request.POST.get("note", ""), via=via)
    if outcome.ok:
        messages.success(request, f"{s.reference} approved.")
    else:
        messages.error(request, "Not approved. " + " ".join(outcome.reasons))
    return redirect("workflow:quick", pk=s.pk)


@login_required
@require_POST
def quick_reject(request, pk):
    s = _shipment(request, pk, "approve")
    gate = _mfa_gate(request, s.organization)
    if gate:
        return gate
    via = "alert link" if request.POST.get("via") == "link" else "approval page"
    outcome = decisions.reject(s, request.user, request.POST.get("note", ""), via=via)
    if outcome.ok:
        messages.info(request, f"{s.reference} rejected.")
    else:
        messages.error(request, "Not rejected. " + " ".join(outcome.reasons))
    return redirect("workflow:quick", pk=s.pk)


# ---------------------------------------------------------------- comments


def _target(request):
    kind, _, pk = request.POST.get("target", "").partition(":")
    if not pk.isdigit():
        raise Http404
    allowed = orgs_for_user(request.user)
    if kind == "shipment":
        obj = get_object_or_404(Shipment.objects.filter(organization__in=allowed).select_related("organization"),
                                pk=pk)
        return obj.organization, obj, None
    if kind == "document":
        obj = get_object_or_404(Document.objects.filter(organization__in=allowed).select_related("organization"),
                                pk=pk)
        return obj.organization, None, obj
    raise Http404


def _comment_back(request, c: Comment | None, default: str) -> str:
    url = _back(request, default).split("#")[0]
    return f"{url}#comment-{c.pk}" if c else f"{url}#comments"


@login_required
@require_POST
def comment_create(request):
    org, shipment, document = _target(request)
    require(request.user, org, "view")
    default = (reverse("review:shipment", args=[shipment.pk]) if shipment else
               reverse("review:document", args=[document.pk]))
    parent = None
    if request.POST.get("parent", "").isdigit():
        parent = Comment.objects.filter(pk=request.POST["parent"], organization=org).first()
    body = request.POST.get("body", "")
    try:
        c = comments.create(org, request.user, body, shipment=shipment, document=document, parent=parent)
    except comments.CommentError as e:
        messages.error(request, str(e))
        request.session["wf_comment_draft"] = body[:comments.BODY_MAX]
        return redirect(_comment_back(request, None, default))
    request.session.pop("wf_comment_draft", None)
    mentioned = c.mentions.exclude(pk=request.user.pk).count()
    messages.success(request, "Comment posted." + (f" {mentioned} {'person was' if mentioned == 1 else 'people were'}"
                                                   " notified." if mentioned else ""))
    unknown = comments.unknown_mentions(org, body)
    if unknown:
        shown = ", ".join("@" + n for n in unknown[:3])
        messages.info(request, f"{shown} {'is' if len(unknown) == 1 else 'are'} not a member of this organization, "
                               "so nobody was notified. Pick a name from the list that appears after typing @.")
    return redirect(_comment_back(request, c, default))


def _own_comment(request, pk) -> Comment:
    c = get_object_or_404(Comment.objects.filter(organization__in=orgs_for_user(request.user))
                          .select_related("organization", "shipment", "document"), pk=pk)
    require(request.user, c.organization, "view")
    return c


def _comment_default(c: Comment) -> str:
    return notify.comment_path(c).split("#")[0]


@login_required
@require_POST
def comment_edit(request, pk):
    c = _own_comment(request, pk)
    try:
        comments.edit(c, request.user, request.POST.get("body", ""))
        messages.success(request, "Comment updated.")
    except comments.CommentError as e:
        messages.error(request, str(e))
    return redirect(_comment_back(request, c, _comment_default(c)))


@login_required
@require_POST
def comment_delete(request, pk):
    c = _own_comment(request, pk)
    try:
        comments.delete(c, request.user)
        messages.success(request, "Comment deleted.")
    except comments.CommentError as e:
        messages.error(request, str(e))
    return redirect(_comment_back(request, None, _comment_default(c)))


@login_required
@require_safe
def members(request):
    """Autocomplete for @mentions: members of one organization the person can see, nobody else."""
    raw = request.GET.get("for", "")
    org = Organization.objects.filter(pk=int(raw)).first() if raw.isdigit() else getattr(request, "org", None)
    if org is None or not has_perm(request.user, org, "view"):
        raise Http404
    q = request.GET.get("q", "").strip().lstrip("@")[:60]
    people = Membership.objects.filter(organization=org, user__is_active=True).select_related("user")
    if q:
        people = people.filter(Q(user__username__icontains=q) | Q(user__first_name__icontains=q)
                               | Q(user__last_name__icontains=q) | Q(user__email__icontains=q))
    roles = dict(Membership.Role.choices)
    out = [{"username": m.user.get_username(), "name": display(m.user), "role": roles.get(m.role, "")}
           for m in people.order_by("user__first_name", "user__username")[:8]]
    return JsonResponse({"members": out})


# ---------------------------------------------------------------- notifications


@login_required
def notification_list(request):
    show = "all" if request.GET.get("show") == "all" else "unread"
    qs = notify.visible(request.user).select_related("organization", "actor")
    unread = qs.filter(read_at__isnull=True).count()
    if show == "unread":
        qs = qs.filter(read_at__isnull=True)
    page = Paginator(qs.order_by("-created_at", "-id"), PER_PAGE).get_page(request.GET.get("page"))
    return render(request, "workflow/notifications.html", {"page": page, "show": show, "unread": unread,
                                                           "query": f"show={show}"})


@login_required
@require_POST
def notification_open(request, pk):
    n = get_object_or_404(notify.visible(request.user), pk=pk)
    if n.read_at is None:
        Notification.objects.filter(pk=n.pk).update(read_at=timezone.now())
    target = notify.with_org(n.url, n.organization) if n.url else ""
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect("workflow:notifications")


@login_required
@require_POST
def notifications_read_all(request):
    n = Notification.objects.filter(user=request.user, read_at__isnull=True).update(read_at=timezone.now())
    messages.success(request, f"Marked {n} notification{'s' if n != 1 else ''} as read." if n else "Nothing unread.")
    return redirect(_back(request, reverse("workflow:notifications")))


# ---------------------------------------------------------------- shortcuts and preferences


@login_required
def shortcuts(request):
    prefs = Preference.for_user(request.user)
    if request.method == "POST":
        prefs = Preference.objects.get_or_create(user=request.user)[0]
        if request.POST.get("full") == "1":
            prefs.shortcuts = request.POST.get("shortcuts") == "on"
            prefs.email_mentions = request.POST.get("email_mentions") == "on"
            prefs.email_assignments = request.POST.get("email_assignments") == "on"
            messages.success(request, "Preferences saved.")
        elif request.POST.get("shortcuts") in ("on", "off"):
            prefs.shortcuts = request.POST["shortcuts"] == "on"
            messages.success(request, "Keyboard shortcuts are on." if prefs.shortcuts else
                             "Keyboard shortcuts are off. Turn them on again under Keyboard shortcuts.")
        prefs.save()
        return redirect(_back(request, reverse("workflow:shortcuts")))
    return render(request, "workflow/shortcuts.html", {"prefs": prefs})


# ---------------------------------------------------------------- assignment rules (settings)


@login_required
def assignment_settings(request):
    org = current_org(request)
    require(request.user, org, "manage")
    rules = AssignmentRules.for_org(org)
    if request.method == "POST":
        mode = request.POST.get("mode", "")
        if mode not in AssignmentRules.Mode.values:
            messages.error(request, "Choose how new shipments are assigned.")
            return redirect("workflow:assignment_settings")
        before = {"mode": rules.mode, "include_approvers": rules.include_approvers,
                  "vendor_fallback": rules.vendor_fallback}
        rules.mode = mode
        rules.include_approvers = request.POST.get("include_approvers") == "on"
        rules.vendor_fallback = request.POST.get("vendor_fallback") == "on"
        rules.save()
        after = {"mode": rules.mode, "include_approvers": rules.include_approvers,
                 "vendor_fallback": rules.vendor_fallback}
        changes = {k: [before[k], after[k]] for k in before if before[k] != after[k]}
        if changes:
            audit(org, "assignment_rules.updated", rules, actor=request.user, changes=changes,
                  mode=rules.get_mode_display().lower())
        messages.success(request, "Assignment rules saved.")
        return redirect("workflow:assignment_settings")
    vendors = sorted({v.strip() for v in ExtractedField.objects.filter(document__organization=org, name="vendor_name")
                      .values_list("value", flat=True)[:1000] if isinstance(v, str) and v.strip()}, key=str.lower)
    waiting = Shipment.objects.filter(organization=org, status__in=assignment.AUTO_STATUSES,
                                      assignment__isnull=True).count()
    return render(request, "workflow/assignment_settings.html", {
        "rules": rules, "modes": AssignmentRules.Mode.choices, "people": assignment.assignable(org),
        "pool": assignment.turn_pool(org, rules), "vendor_rules": VendorRule.objects.filter(organization=org)
        .select_related("assignee"), "vendors": vendors[:200], "waiting": waiting,
    })


@login_required
@require_POST
def vendor_rule_add(request):
    org = current_org(request)
    require(request.user, org, "manage")
    name = request.POST.get("vendor_name", "").strip()[:200]
    key = vendor_key(name)
    person = _person(org, request.POST.get("assignee", ""), request.user)
    if not key:
        messages.error(request, "Type the vendor's name as it appears on its invoices.")
    elif person is None:
        messages.error(request, "Choose a reviewer, approver or admin to give this vendor's shipments to.")
    else:
        rule, created = VendorRule.objects.update_or_create(
            organization=org, vendor_key=key,
            defaults={"vendor_name": name, "assignee": person, "created_by": request.user})
        audit(org, "assignment_rules.vendor_set", rule, actor=request.user, vendor=name, assignee=display(person),
              replaced=not created)
        messages.success(request, f"Shipments from {name} will go to {display(person)}.")
    return redirect("workflow:assignment_settings")


@login_required
@require_POST
def vendor_rule_delete(request, pk):
    org = current_org(request)
    require(request.user, org, "manage")
    rule = get_object_or_404(VendorRule, pk=pk, organization=org)
    audit(org, "assignment_rules.vendor_removed", rule, actor=request.user, vendor=rule.vendor_name)
    rule.delete()
    messages.success(request, f"Removed the rule for {rule.vendor_name}.")
    return redirect("workflow:assignment_settings")


@login_required
@require_POST
def assign_waiting(request):
    org = current_org(request)
    require(request.user, org, "manage")
    rules = AssignmentRules.for_org(org)
    if rules.mode == AssignmentRules.Mode.OFF:
        messages.error(request, "Turn on automatic assignment first, then assign the waiting shipments.")
        return redirect("workflow:assignment_settings")
    n = assignment.assign_waiting(org)
    messages.success(request, f"Assigned {n} waiting shipment{'s' if n != 1 else ''}." if n else
                     "No waiting shipment could be assigned. Check that the rules have someone to give work to.")
    return redirect("workflow:assignment_settings")


# ---------------------------------------------------------------- firm view


CLIENT_COLUMNS = [("name", "Client", False), ("needs_review", "Needs review", True), ("ready", "Ready to approve", True),
                  ("assigned", "Assigned to you", True), ("failed", "Failed postings", True),
                  ("connection", "Accounting", False), ("at_risk", "Money at risk", True),
                  ("oldest", "Oldest waiting", True)]


@login_required
def client_list(request):
    rows = portfolio.build(request.user)
    rows, sort, direction = portfolio.sort_rows(rows, request.GET.get("sort", ""), request.GET.get("dir", ""))
    open_rows = [r for r in rows if not r.locked]
    totals = {"needs_review": sum(r.needs_review for r in open_rows), "ready": sum(r.ready for r in open_rows),
              "assigned": sum(r.assigned_to_me for r in open_rows),
              "failed": sum(r.failed_postings for r in open_rows),
              "reconnect": sum(1 for r in open_rows if r.connection == "reconnect")}
    can_create = orgs.can_create(request.user)
    columns = []
    for key, label, num in CLIENT_COLUMNS:
        on = key == sort
        flip = "asc" if (on and direction == "desc") else ("desc" if on else
                                                           ("desc" if key in portfolio.DEFAULT_DESC else "asc"))
        columns.append({"key": key, "label": label, "num": num, "url": f"?sort={key}&dir={flip}",
                        "aria": ("descending" if direction == "desc" else "ascending") if on else ""})
    return render(request, "workflow/clients.html", {
        "rows": rows, "sort": sort, "dir": direction, "totals": totals, "can_create": can_create, "columns": columns,
        "copy_choices": orgs.firm_admin_orgs(request.user).order_by("name") if can_create else [],
        "zones": _zones() if can_create else [],
    })


def _zones():
    import zoneinfo

    return timezones.choices()


@login_required
@require_POST
def create_org(request):
    if not orgs.can_create(request.user):
        raise PermissionDenied("Creating client organizations isn't turned on for your account.")
    copy_from = None
    raw = request.POST.get("copy_from", "")
    if raw.isdigit():
        copy_from = orgs.firm_admin_orgs(request.user).filter(pk=int(raw)).first()
    try:
        org = orgs.create(request.user, request.POST.get("name", ""),
                          home_currency=request.POST.get("home_currency", "USD"),
                          tz=request.POST.get("timezone", "UTC"), copy_from=copy_from)
    except orgs.OrgError as e:
        messages.error(request, str(e))
        return redirect("workflow:portfolio")
    messages.success(request, f"Created {org.name}. You are its admin; invite the team and connect QuickBooks or Xero next.")
    return redirect(f"{reverse('core:team')}?org={org.slug}")


@login_required
def my_work(request):
    client = request.GET.get("client", "")
    kind = request.GET.get("kind", "")
    kind = kind if kind in ("assigned", "approve") else ""
    items, hidden = portfolio.my_work(request.user, client=client, kind=kind)
    page = Paginator(items, PER_PAGE).get_page(request.GET.get("page"))
    portfolio.add_totals(page.object_list)
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "workflow/my_work.html", {
        "page": page, "hidden": hidden, "client": client, "kind": kind, "query": params.urlencode(),
        "clients": portfolio.member_orgs(request.user).order_by("name"), "total": len(items),
        "assigned_total": sum(1 for i in items if i.assigned), "approve_total": sum(1 for i in items if i.can_approve),
    })

