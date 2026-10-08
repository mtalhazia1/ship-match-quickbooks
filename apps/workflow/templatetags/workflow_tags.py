"""Template helpers for the workflow includes in shared pages (base.html, the queue, the shipment page),
so those pages only need one {% include %} line each and no view changes beyond the queue filter."""
from __future__ import annotations

from django import template
from django.urls import reverse

from apps.core.permissions import has_perm
from apps.documents.models import Document
from apps.shipments.models import Shipment

from ..models import Assignment, Preference
from ..queue import ASSIGNED_CHOICES, neighbours
from ..services import assignment, comments, notify, portfolio

register = template.Library()


def _user(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    return request, user if getattr(user, "is_authenticated", False) else None


@register.simple_tag(takes_context=True)
def wf_prefs(context):
    _, user = _user(context)
    return Preference.for_user(user) if user else Preference()


@register.simple_tag(takes_context=True)
def wf_nav(context):
    """Sidebar: shipments assigned to me in this organization, and whether the firm links show."""
    request, user = _user(context)
    org = context.get("org")
    if user is None:
        return {}
    cached = getattr(request, "_wf_nav", None)
    if cached is not None:
        return cached
    out = {"multi_org": portfolio.is_multi_org(user), "assigned": 0, "assigned_url": ""}
    if org is not None:
        mine = Shipment.objects.filter(organization=org, status__in=assignment.ACTIVE_STATUSES,
                                       assignment__assignee=user)
        counts = dict(mine.values_list("status").order_by().annotate(n=_count()))
        out["assigned"] = sum(counts.values())
        tab = next((s for s in ("needs_review", "ready", "open") if counts.get(s)), "needs_review")
        tab = "all" if tab == "open" else tab
        out["assigned_url"] = f"{reverse('review:queue')}?status={tab}&assigned=me"
        match = getattr(request, "resolver_match", None)
        out["on"] = bool(match and match.view_name == "review:queue" and request.GET.get("assigned") == "me")
    request._wf_nav = out
    return out


def _count():
    from django.db.models import Count

    return Count("id")


@register.simple_tag(takes_context=True)
def wf_bell(context):
    _, user = _user(context)
    if user is None:
        return {"unread": 0, "latest": []}
    qs = notify.visible(user).select_related("organization")
    return {"unread": qs.filter(read_at__isnull=True).count(),
            "latest": list(qs.order_by("-created_at", "-id")[:6])}


@register.simple_tag(takes_context=True)
def wf_queue(context):
    """Review queue toolbar: the Assigned to chips with counts, who can be assigned, what the person may do."""
    request, user = _user(context)
    org = context.get("org")
    if user is None or org is None:
        return {}
    current = request.GET.get("assigned", "")
    params = request.GET.copy()
    params.pop("page", None)
    base = Shipment.objects.filter(organization=org)
    tab = context.get("tab") or "needs_review"
    if tab != "all":
        base = base.filter(status=tab)
    counts = {"me": base.filter(assignment__assignee=user).count(),
              "none": base.filter(assignment__assignee__isnull=True).count()}
    chips = []
    for key, label in ASSIGNED_CHOICES:
        p = params.copy()
        if key:
            p["assigned"] = key
        else:
            p.pop("assigned", None)
        chips.append({"key": key, "label": label, "count": counts.get(key), "url": "?" + p.urlencode(),
                      "on": current == key})
    people = list(assignment.assignable(org))
    if current.isdigit():
        who = next((u for u in people if str(u.pk) == current), None)
        if who is not None:
            p = params.copy()
            chips.append({"key": current, "label": f"Assigned to {notify.display(who)}", "count": None,
                          "url": "?" + p.urlencode(), "on": True})
    return {"chips": chips, "people": people, "me_assignable": any(u.pk == user.pk for u in people),
            "next": request.get_full_path()}


@register.simple_tag(takes_context=True)
def wf_shipment(context, shipment):
    """Shipment page: who it is assigned to, who it can go to, and the previous/next shipment in its tab."""
    _, user = _user(context)
    if user is None or not isinstance(shipment, Shipment):
        return {}
    a = Assignment.objects.filter(shipment=shipment).select_related("assignee", "assigned_by").first()
    prev, nxt = neighbours(shipment)
    people = list(assignment.assignable(shipment.organization))
    return {"assignment": a, "assignee": a.assignee if a else None, "people": people,
            "me_assignable": any(u.pk == user.pk for u in people), "prev": prev, "next": nxt}


@register.simple_tag(takes_context=True)
def wf_comments(context, shipment=None, document=None):
    request, user = _user(context)
    # A variable missing from the page's context arrives as "" (string_if_invalid), not None.
    shipment = shipment if isinstance(shipment, Shipment) else None
    document = document if isinstance(document, Document) else None
    if user is None or (shipment is None and document is None):
        return {}
    org = (shipment or document).organization
    threads = comments.threads_for(shipment=shipment, document=document)
    targets = []
    if shipment is not None:
        targets.append((f"shipment:{shipment.pk}", f"The whole shipment ({shipment.reference})"))
        for d in shipment.documents:
            targets.append((f"document:{d.pk}", f"{d.get_doc_type_display()}: {d.original_filename}"))
    else:
        targets.append((f"document:{document.pk}", document.original_filename))
    draft = request.session.pop("wf_comment_draft", "") if request is not None and hasattr(request, "session") else ""
    count = sum(1 + len(t.replies) for t in threads)
    return {"threads": threads, "count": count, "targets": targets, "can_comment": has_perm(user, org, "edit"),
            "draft": draft, "members_url": f"{reverse('workflow:members')}?for={org.pk}",
            "edit_minutes": int(comments.edit_window().total_seconds() // 60), "next": request.get_full_path()}


@register.filter
def wf_can_change(comment, user) -> bool:
    return comments.can_change(comment, user)


@register.filter
def wf_minutes_left(comment) -> int:
    return comments.minutes_left(comment)

