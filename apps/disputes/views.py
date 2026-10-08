"""Disputes: start from a shipment, write and send the email, follow up, record the credit.

Permissions: viewing needs "view"; drafting, editing drafts, logging replies, notes and follow-up dates
need "edit"; sending, recording credits, resolving, closing and approving without waiting need "approve";
dispute email settings need "manage".
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from apps.core.paging import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounting.models import vendor_key
from apps.core.permissions import has_perm, require
from apps.core.utils import audit, current_org, orgs_for_user, use_org
from apps.documents.models import Document
from apps.shipments.models import Shipment

from . import reports
from .evidence import disputable_groups, money
from .models import Dispute, DisputeSettings
from .services import auto_credit, sending, workflow
from .services.workflow import DisputeError

TABS = [
    ("waiting", "Waiting for vendor"), ("draft", "Drafts"), ("credit_received", "Credit received"),
    ("resolved", "Resolved"), ("closed", "Closed without recovery"), ("all", "All"),
]
PER_PAGE = 25


def _dispute_for(request, pk, perm: str = "view") -> Dispute:
    dispute = get_object_or_404(Dispute.objects.filter(organization__in=orgs_for_user(request.user))
                                .select_related("organization", "shipment", "invoice", "credit_note"), pk=pk)
    use_org(request, dispute.organization)
    require(request.user, dispute.organization, perm)
    return dispute


def _detail(dispute: Dispute):
    return redirect("disputes:detail", pk=dispute.pk)


# ---------------------------------------------------------------- list


@login_required
def dispute_list(request):
    org = current_org(request)
    require(request.user, org, "view")
    tab = request.GET.get("status", "waiting")
    tab = tab if tab in dict(TABS) else "waiting"
    vendor = request.GET.get("vendor", "")
    age = request.GET.get("age", "")
    q = request.GET.get("q", "").strip()

    base = Dispute.objects.filter(organization=org)
    counts = {
        "waiting": base.filter(status__in=Dispute.WAITING).count(),
        "draft": base.filter(status=Dispute.Status.DRAFT).count(),
        "credit_received": base.filter(status=Dispute.Status.CREDIT_RECEIVED).count(),
        "resolved": base.filter(status=Dispute.Status.RESOLVED).count(),
        "closed": base.filter(status=Dispute.Status.CLOSED).count(),
        "all": base.count(),
    }
    qs = {"waiting": base.filter(status__in=Dispute.WAITING), "all": base}.get(tab) or base.filter(status=tab)
    if vendor:
        qs = qs.filter(vendor_key=vendor)
    if age == "overdue":
        qs = qs.filter(reports.overdue_q(org))
    elif age:
        qs = reports.filter_age(qs, age)
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(invoice_number__icontains=q) | Q(vendor_name__icontains=q)
                       | Q(shipment_reference__icontains=q))
    order = ("follow_up_on", "sent_at") if tab == "waiting" else ("-updated_at",)
    qs = qs.select_related("organization", "shipment").order_by(*order, "-id")
    page = Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))
    vendors = list(base.order_by("vendor_name").values_list("vendor_key", "vendor_name").distinct())
    seen, vendor_choices = set(), []
    for key, name in vendors:
        if key not in seen:
            seen.add(key)
            vendor_choices.append((key, name))
    params = request.GET.copy()
    params.pop("page", None)
    tab_params = params.copy()
    tab_params.pop("status", None)
    return render(request, "disputes/list.html", {
        "tab": tab, "tabs": [(k, v, counts[k]) for k, v in TABS], "page": page, "s": reports.summary(org),
        "vendor": vendor, "vendors": vendor_choices, "age": age, "ages": reports.AGE_BUCKETS, "q": q,
        "query": params.urlencode(), "tab_query": tab_params.urlencode(),
    })


# ---------------------------------------------------------------- create from a shipment


@login_required
@require_POST
def create(request, shipment_pk):
    shipment = get_object_or_404(Shipment.objects.filter(organization__in=orgs_for_user(request.user))
                                 .select_related("organization"), pk=shipment_pk)
    use_org(request, shipment.organization)
    require(request.user, shipment.organization, "edit")
    back = f"{reverse('review:shipment', args=[shipment.pk])}#disputes"
    invoice = Document.objects.filter(organization=shipment.organization,
                                      pk=request.POST.get("invoice") if str(request.POST.get("invoice", "")).isdigit()
                                      else 0).first()
    if invoice is None:
        messages.error(request, "Choose the invoice to dispute.")
        return redirect(back)
    try:
        dispute, note = workflow.create_draft(shipment, invoice, request.POST.getlist("issues"), request.user,
                                              use_ai=request.POST.get("ai") == "on")
    except DisputeError as e:
        messages.error(request, str(e))
        return redirect(back)
    messages.success(request, f"Draft {dispute.reference} is ready. Check the wording and the vendor's address, then send it."
                     + (f" {note}" if note else ""))
    return redirect("disputes:edit", pk=dispute.pk)


# ---------------------------------------------------------------- detail and composer


def _sent_text(dispute: Dispute) -> str:
    from apps.demo.mail import outbound_blocked

    if outbound_blocked():
        return (f"Marked {dispute.reference} as sent. This is the public demo, so no email went to "
                f"{dispute.vendor_email}.")
    return f"Sent {dispute.reference} to {dispute.vendor_email}."


def _credit_candidates(dispute: Dispute):
    """Documents that could be the credit note or corrected invoice: same organization, newest first."""
    qs = (Document.objects.filter(organization=dispute.organization).exclude(pk=dispute.invoice_id)
          .prefetch_related("fields").order_by("-received_at")[:60])
    credits, same, other = [], [], []
    for d in qs:
        if vendor_key(d.field("vendor_name") or "") == dispute.vendor_key:
            (credits if d.is_credit else same).append(d)
        else:
            other.append(d)
    return credits + same   # another vendor's documents can't be this vendor's credit


@login_required
def detail(request, pk):
    dispute = _dispute_for(request, pk)
    if dispute.status == Dispute.Status.DRAFT and has_perm(request.user, dispute.organization, "edit"):
        return redirect("disputes:edit", pk=dispute.pk)
    settings_ = DisputeSettings.for_org(dispute.organization)
    sent_event = dispute.events.filter(kind="sent").order_by("-created_at").first()
    return render(request, "disputes/detail.html", {
        "sent_reply_to": (sent_event.data or {}).get("reply_to") if sent_event else None,
        "dispute": dispute, "items": list(dispute.items.select_related("issue")),
        "events": dispute.events.select_related("actor")[:200],
        "candidates": _credit_candidates(dispute) if dispute.status in Dispute.WAITING | Dispute.RECOVERED else [],
        "suggested": auto_credit.suggestion(dispute),
        "settings": settings_, "note_min": workflow.NOTE_MIN,
        "shipment_locked": bool(dispute.shipment and dispute.shipment.is_locked),
    })


@login_required
def edit(request, pk):
    dispute = _dispute_for(request, pk, "edit")
    if dispute.status != Dispute.Status.DRAFT:
        return _detail(dispute)
    if request.method == "POST":
        action = request.POST.get("action", "save")
        p = request.POST
        try:
            notes = workflow.update_draft(
                dispute, request.user, vendor_email=p.get("vendor_email", ""), contact_name=p.get("contact_name", ""),
                cc=p.get("cc", ""), subject=p.get("subject", ""), body=p.get("body", ""), amount=p.get("amount", ""),
                issue_ids=p.getlist("issues"), follow_up=p.get("follow_up_on", ""), remember=p.get("remember") == "on")
        except DisputeError as e:
            messages.error(request, str(e))
            return redirect("disputes:edit", pk=dispute.pk)
        for n in notes:
            messages.info(request, n)
        if action == "rewrite":
            workflow.rebuild(dispute, request.user)
            messages.success(request, "The email was written again from the evidence.")
        elif action == "polish":
            messages.info(request, workflow.repolish(dispute, request.user))
        elif action == "send":
            if not has_perm(request.user, dispute.organization, "approve"):
                messages.error(request, "Draft saved. Only approvers and admins can send disputes to vendors; "
                                        "ask one to review and send it.")
                return redirect("disputes:edit", pk=dispute.pk)
            try:
                sending.send(dispute, request.user)
            except DisputeError as e:
                messages.error(request, str(e))
                return redirect("disputes:edit", pk=dispute.pk)
            messages.success(request, _sent_text(dispute) +
                             f" We'll flag it if there's no answer by {dispute.follow_up_on:%d %b %Y}.")
            return _detail(dispute)
        else:
            messages.success(request, "Draft saved.")
        return redirect("disputes:edit", pk=dispute.pk)

    groups = disputable_groups(dispute.shipment) if dispute.shipment else []
    group = next((g for g in groups if g.invoice.pk == dispute.invoice_id), None)
    linked = set(dispute.items.exclude(issue=None).values_list("issue_id", flat=True))
    settings_ = DisputeSettings.for_org(dispute.organization)
    return render(request, "disputes/edit.html", {
        "dispute": dispute, "items": list(dispute.items.all()), "issues": group.issues if group else [],
        "linked": linked, "settings": settings_, "reply_to": sending.reply_to_for(dispute, request.user),
        "default_amount": workflow.default_amount(dispute), "follow_up_default": workflow.follow_up_default(dispute.organization),
        "ai_enabled": _ai_enabled(), "events": dispute.events.select_related("actor")[:50],
    })


def _ai_enabled() -> bool:
    from apps.documents.services import llm

    return llm.is_enabled()


@login_required
@require_POST
def send(request, pk):
    dispute = _dispute_for(request, pk, "approve")
    try:
        sending.send(dispute, request.user)
    except DisputeError as e:
        messages.error(request, str(e))
        return redirect("disputes:edit" if dispute.status == Dispute.Status.DRAFT else "disputes:detail", pk=dispute.pk)
    messages.success(request, _sent_text(dispute))
    return _detail(dispute)


@login_required
@require_POST
def discard(request, pk):
    dispute = _dispute_for(request, pk, "edit")
    shipment_id = dispute.shipment_id
    try:
        workflow.discard(dispute, request.user)
    except DisputeError as e:
        messages.error(request, str(e))
        return _detail(dispute)
    messages.success(request, "Draft discarded. Nothing was sent to the vendor.")
    if shipment_id:
        return redirect(f"{reverse('review:shipment', args=[shipment_id])}#disputes")
    return redirect("disputes:list")


def _run(request, pk, perm: str, fn, success: str):
    dispute = _dispute_for(request, pk, perm)
    try:
        result = fn(dispute)
    except DisputeError as e:
        messages.error(request, str(e))
        return _detail(dispute)
    messages.success(request, success.format(d=dispute, result=result))
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return redirect(nxt)
    return _detail(dispute)


@login_required
@require_POST
def reply(request, pk):
    p = request.POST
    return _run(request, pk, "edit", lambda d: workflow.log_reply(
        d, request.user, p.get("text", ""), p.get("agreed") == "on", p.get("follow_up_on", "")),
        "Reply logged on {d.reference}.")


@login_required
@require_POST
def note(request, pk):
    return _run(request, pk, "edit", lambda d: workflow.add_note(d, request.user, request.POST.get("text", "")),
                "Note added.")


@login_required
@require_POST
def follow_up(request, pk):
    return _run(request, pk, "edit", lambda d: workflow.set_follow_up(d, request.user, request.POST.get("follow_up_on", "")),
                "Follow-up date saved.")


@login_required
@require_POST
def credit(request, pk):
    p = request.POST
    dispute = _dispute_for(request, pk, "approve")
    try:
        workflow.record_credit(dispute, request.user, p.get("amount", ""), p.get("credit_note", ""),
                               p.get("settles") == "on", p.get("note", ""), confirm_over=p.get("confirm_over") == "on")
    except DisputeError as e:
        messages.error(request, str(e))
        return _detail(dispute)
    text = f"Recorded {money(dispute.amount_recovered, dispute.currency)} recovered on {dispute.reference}."
    if dispute.status == Dispute.Status.RESOLVED:
        text += " The dispute is resolved."
    if dispute.amount_recovered > dispute.amount_disputed:
        messages.info(request, "The credit is more than the amount disputed, as you confirmed.")
    messages.success(request, text)
    return _detail(dispute)


@login_required
@require_POST
def resolve(request, pk):
    return _run(request, pk, "approve", lambda d: workflow.resolve(d, request.user, request.POST.get("note", "")),
                "{d.reference} resolved.")


@login_required
@require_POST
def close(request, pk):
    return _run(request, pk, "approve", lambda d: workflow.close(d, request.user, request.POST.get("note", "")),
                "{d.reference} closed without recovery.")


@login_required
@require_POST
def release(request, pk):
    return _run(request, pk, "approve", lambda d: workflow.release_hold(d, request.user, request.POST.get("note", "")),
                "{d.shipment_reference} can now be approved; {d.reference} stays open to collect the credit.")


# ---------------------------------------------------------------- settings


@login_required
def dispute_settings(request):
    org = current_org(request)
    require(request.user, org, "manage")
    obj = DisputeSettings.for_org(org)
    if request.method == "POST":
        p = request.POST
        try:
            reply_to = workflow.clean_emails(p.get("reply_to", ""), "reply-to address")
            if len(reply_to) > 1:
                raise DisputeError("Use one reply-to address, for example ap@yourcompany.com.")
            days = int(p.get("follow_up_days", "7") or 7)
            if not 1 <= days <= 90:
                raise DisputeError("Follow up after 1 to 90 days.")
        except ValueError as e:  # DisputeError is a ValueError
            messages.error(request, str(e) if isinstance(e, DisputeError) else "Follow-up days should be a whole number.")
            return redirect("disputes:settings")
        before = {"reply_to": obj.reply_to, "signature": obj.signature, "follow_up_days": obj.follow_up_days,
                  "copy_reply_to": obj.copy_reply_to}
        obj.reply_to = reply_to[0] if reply_to else ""
        obj.signature = p.get("signature", "").replace("\r\n", "\n").strip()[:1000]
        obj.follow_up_days = days
        obj.copy_reply_to = p.get("copy_reply_to") == "on"
        obj.save()
        after = {k: getattr(obj, k) for k in before}
        audit(org, "dispute_settings.updated", obj, actor=request.user,
              changes={k: [before[k], after[k]] for k in before if before[k] != after[k]})
        messages.success(request, "Dispute settings saved.")
        return redirect("disputes:settings")
    return render(request, "disputes/settings.html", {"obj": obj})
