"""Review screens: queue, documents, search, shipment detail, corrections, approval and posting.

Every view checks the user's role in the object's organization (see apps.core.permissions).
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from apps.core.paging import Paginator
from django.db.models import Count, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounting.models import PostedBill, VendorMapping, vendor_key
from apps.accounting.services.payments import filter_shipments
from apps.accounting.services.providers import active_connection, set_rule_account
from apps.core.context_processors import LOOSE_STATUSES
from apps.core.permissions import require
from apps.core.utils import audit, current_org, orgs_for_user, use_org
from apps.documents.models import Document
from apps.documents.schemas import SCHEMAS, TABLE_FIELDS
from apps.documents.services.corrections import EDITABLE, FieldValueError, after_correction, correct_field
from apps.documents.services.ingest import RejectedFile, ingest_bytes
from apps.shipments.models import Approval, Shipment, ValidationIssue
from apps.shipments.services.approval import approval_blockers, posting_blockers, shipment_totals
from apps.shipments.services.matching import assign_manually
from apps.shipments.services.timeline import timeline
from apps.shipments.services.validation import update_status, validate_shipment
from apps.shipments.templatetags.review_tags import label
from apps.workflow.queue import filter_queue

TABS = [
    ("needs_review", "Needs review"), ("ready", "Ready to approve"), ("approved", "Approved"),
    ("posted", "Posted"), ("rejected", "Rejected"), ("all", "All"),
]
SORTS = {
    "attention": ("-n_errors", "-n_warnings", "created_at"),
    "newest": ("-created_at",),
    "oldest": ("created_at",),
    "updated": ("-updated_at",),
}
PER_PAGE = 25
OVERRIDE_NOTE_MIN = 10
REJECT_NOTE_MIN = 5


# ---------------------------------------------------------------- helpers


def _shipment_for(request, pk, perm: str = "view") -> Shipment:
    shipment = get_object_or_404(Shipment.objects.filter(organization__in=orgs_for_user(request.user))
                                 .select_related("organization"), pk=pk)
    use_org(request, shipment.organization)
    require(request.user, shipment.organization, perm)
    return shipment


def _document_for(request, pk, perm: str = "view") -> Document:
    doc = get_object_or_404(Document.objects.filter(organization__in=orgs_for_user(request.user))
                            .select_related("organization"), pk=pk)
    use_org(request, doc.organization)
    require(request.user, doc.organization, perm)
    return doc


def _back(request, default: str) -> str:
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return default


def _annotated(qs):
    return qs.annotate(
        n_docs=Count("links", distinct=True),
        n_errors=Count("issues", filter=Q(issues__resolved=False, issues__severity="error"), distinct=True),
        n_warnings=Count("issues", filter=Q(issues__resolved=False, issues__severity="warning"), distinct=True),
    )


def _search_shipments(qs, q: str):
    q = q.strip()
    if not q:
        return qs
    term = q.upper().replace(" ", "")
    return qs.filter(Q(reference__icontains=q) | Q(bl_number__icontains=term)
                     | Q(container_numbers__icontains=term) | Q(po_numbers__icontains=term)
                     | Q(links__document__original_filename__icontains=q)
                     | Q(links__document__fields__name__in=["invoice_number", "vendor_name", "carrier_name"],
                         links__document__fields__value__icontains=q)).distinct()


def _search_documents(qs, q: str):
    q = q.strip()
    if not q:
        return qs
    return qs.filter(Q(original_filename__icontains=q)
                     | Q(fields__name__in=["invoice_number", "vendor_name", "carrier_name", "bl_number",
                                           "container_numbers", "po_numbers"], fields__value__icontains=q)).distinct()


def _query_without(request, *keys) -> str:
    params = request.GET.copy()
    for k in keys:
        params.pop(k, None)
    return params.urlencode()


# ---------------------------------------------------------------- lists


@login_required
def queue(request):
    org = current_org(request)
    require(request.user, org, "view")
    tab = request.GET.get("status", "needs_review")
    tab = tab if tab in dict(TABS) else "needs_review"
    sort = request.GET.get("sort", "attention")
    sort = sort if sort in SORTS else "attention"
    q = request.GET.get("q", "")

    base = filter_queue(request, Shipment.objects.filter(organization=org))  # "Assigned to" filter (apps.workflow)
    counts = dict(base.values_list("status").annotate(c=Count("id")))
    counts["all"] = sum(counts.values())
    qs = base if tab == "all" else base.filter(status=tab)
    qs = filter_shipments(qs, request.GET.get("pay", ""))   # payment status of posted bills
    qs = _annotated(_search_shipments(qs, q)).order_by(*SORTS[sort], "-id")
    page = Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))
    return render(request, "review/queue.html", {
        "tab": tab, "tabs": [(k, v, counts.get(k, 0)) for k, v in TABS], "page": page, "q": q, "sort": sort,
        "sorts": [("attention", "Most issues first"), ("oldest", "Oldest first"), ("newest", "Newest first"),
                  ("updated", "Recently updated")],
        "query": _query_without(request, "page"), "tab_query": _query_without(request, "page", "status"),
        "loose_count": Document.objects.filter(organization=org, status__in=LOOSE_STATUSES).count(),
        "qbo": active_connection(org),   # the accounting system: QuickBooks or Xero
    })


DOC_VIEWS = [("attention", "Not in a shipment"), ("matched", "In a shipment"), ("all", "All")]


@login_required
def documents(request):
    org = current_org(request)
    require(request.user, org, "view")
    view = request.GET.get("view", "attention")
    view = view if view in dict(DOC_VIEWS) else "attention"
    doc_type = request.GET.get("type", "")
    q = request.GET.get("q", "")
    base = Document.objects.filter(organization=org)
    counts = {"attention": base.filter(status__in=LOOSE_STATUSES).count(),
              "matched": base.filter(status=Document.Status.MATCHED).count(), "all": base.count()}
    qs = {"attention": base.filter(status__in=LOOSE_STATUSES),
          "matched": base.filter(status=Document.Status.MATCHED), "all": base}[view]
    if doc_type in Document.DocType.values:
        qs = qs.filter(doc_type=doc_type)
    qs = _search_documents(qs, q).select_related("match__shipment", "email", "parent").order_by("-received_at", "-id")
    page = Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))
    return render(request, "review/documents.html", {
        "view": view, "views": [(k, v, counts[k]) for k, v in DOC_VIEWS], "page": page, "q": q,
        "doc_type": doc_type, "types": Document.DocType.choices,
        "query": _query_without(request, "page"), "view_query": _query_without(request, "page", "view"),
    })


@login_required
def search(request):
    org = current_org(request)
    require(request.user, org, "view")
    q = request.GET.get("q", "").strip()
    shipments, docs = [], []
    if q:
        shipments = list(_annotated(_search_shipments(Shipment.objects.filter(organization=org), q))
                         .order_by("-updated_at")[:25])
        if len(shipments) == 1 and shipments[0].reference.lower() == q.lower():
            return redirect("review:shipment", pk=shipments[0].pk)
        docs = list(_search_documents(Document.objects.filter(organization=org), q)
                    .select_related("match__shipment").order_by("-received_at")[:25])
    return render(request, "review/search.html", {"q": q, "shipments": shipments, "docs": docs})


# ---------------------------------------------------------------- upload


@login_required
@require_POST
def upload(request):
    org = current_org(request)
    require(request.user, org, "upload")
    files = request.FILES.getlist("files")
    back = _back(request, reverse("review:queue"))
    if not files:
        messages.error(request, "Choose at least one file to upload.")
        return redirect(back)
    received, duplicates, rejected, archives = [], [], [], []
    for f in files:
        try:
            doc, created = ingest_bytes(org, f.name, f.read(), source=Document.Source.UPLOAD, actor=request.user)
        except RejectedFile as e:
            rejected.append(str(e))
            continue
        if created and doc.source_format == Document.Format.ARCHIVE:
            archives.append(doc)
        else:
            (received if created else duplicates).append((f.name, doc))
    if received:
        names = ", ".join(n for n, _ in received[:3]) + (f" and {len(received) - 3} more" if len(received) > 3 else "")
        messages.success(request, f"Received {len(received)} document{'s' if len(received) != 1 else ''}: {names}. "
                                  "Reading and matching takes a few seconds; refresh to see the result.")
    from apps.intake.services.messages import archive_messages, photo_hint

    for level, text in archive_messages(archives):
        messages.add_message(request, level, text)
    for name, doc in duplicates:
        where = f" It is in {doc.match.shipment.reference}." if hasattr(doc, "match") else ""
        if doc.original_filename and doc.original_filename != name:   # same contents under another name
            messages.info(request, f"{name} has the same contents as {doc.original_filename}, which was uploaded "
                                   f"before, so it was not added again.{where}")
        else:
            messages.info(request, f"{name} was uploaded before, so it was not added again.{where}")
    for reason in rejected:
        messages.error(request, f"Not uploaded: {reason}")
    hint = photo_hint([d for _, d in received])
    if hint:
        messages.info(request, hint)
    return redirect(back)


# ---------------------------------------------------------------- shipment


DOC_ORDER = {"bill_of_lading": 0, "commercial_invoice": 1, "freight_invoice": 2}


def _doc_rows(d: Document):
    schema_fields = list(SCHEMAS[d.doc_type].model_fields) if d.doc_type in SCHEMAS else []
    by_name = {f.name: f for f in d.fields.all()}
    names = [n for n in schema_fields if n not in TABLE_FIELDS]
    names += [n for n in by_name if n not in names and n not in TABLE_FIELDS]
    return [{"name": n, "field": by_name.get(n), "editable": n in EDITABLE} for n in names]


@login_required
def shipment_detail(request, pk):
    shipment = _shipment_for(request, pk)
    org = shipment.organization
    issues = list(shipment.issues.select_related("document", "resolved_by").order_by("resolved", "severity", "id"))
    docs = []
    for d in sorted(shipment.documents.prefetch_related("fields").select_related("match", "email"),
                    key=lambda x: (DOC_ORDER.get(x.doc_type, 9), x.received_at)):
        mapping = None
        if d.posts_to_accounting and d.field("vendor_name"):
            mapping = VendorMapping.objects.filter(organization=org, vendor_key=vendor_key(d.field("vendor_name"))).first()
        docs.append({"doc": d, "rows": _doc_rows(d), "line_items": d.field("line_items") or [], "mapping": mapping,
                     "posted": PostedBill.objects.filter(document=d).first(),
                     "open_issues": [i for i in issues if i.document_id == d.pk and not i.resolved]})
    blockers = approval_blockers(shipment, request.user) if not shipment.is_locked else []
    open_issues = [i for i in issues if not i.resolved]
    bills = [x for x in docs if x["doc"].posts_to_accounting]
    post_summary = None
    if shipment.status == "approved" and bills:   # a half-posted shipment says so in its header, not only in the cards
        sent = [x["posted"].status for x in bills if x["posted"]]
        if sent:
            post_summary = {"posted": sent.count(PostedBill.Status.POSTED), "failed": sent.count(PostedBill.Status.FAILED),
                            "total": len(bills)}
    pending_splits = []
    if not shipment.is_locked and any(i.code == "container_not_on_bl" for i in open_issues):
        from apps.landed.services.rules import unconfirmed_splits

        pending_splits = unconfirmed_splits(shipment)   # confirming the split clears those container errors
    return render(request, "review/shipment.html", {
        "shipment": shipment, "docs": docs, "open_issues": open_issues,
        "resolved_issues": [i for i in issues if i.resolved],
        "open_errors": sum(1 for i in open_issues if i.severity == "error"),
        "blockers": blockers, "pending_splits": pending_splits, "post_summary": post_summary, "totals": shipment_totals(shipment),
        "timeline": timeline(shipment), "qbo": active_connection(org),
        "approvals": shipment.approvals.select_related("user").order_by("-created_at"),
        "other_shipments": Shipment.objects.filter(organization=org).exclude(pk=shipment.pk)
        .exclude(status__in=["approved", "posted"]).order_by("-updated_at")[:50],
        "override_min": OVERRIDE_NOTE_MIN, "reject_min": REJECT_NOTE_MIN, "doc_types": Document.DocType.choices,
    })


@login_required
def document_file(request, pk):
    doc = _document_for(request, pk)
    response = FileResponse(doc.file.open("rb"), content_type="application/pdf", filename=doc.pdf_filename)
    response["Content-Disposition"] = response["Content-Disposition"].replace("attachment", "inline")
    response["Cache-Control"] = "private, max-age=300"
    return response


@login_required
def document_detail(request, pk):
    doc = _document_for(request, pk)
    if doc.is_container:  # a ZIP or a split PDF: show what came out of it
        from apps.intake.views import container_detail

        return container_detail(request, doc)
    if hasattr(doc, "match"):
        return redirect(f"{reverse('review:shipment', args=[doc.match.shipment_id])}#doc-{doc.pk}")
    by_name = {f.name: f for f in doc.fields.all()}
    key_rows = [{"name": n, "field": by_name.get(n)} for n in ("bl_number", "container_numbers", "po_numbers")]
    other_rows = [{"name": f.name, "field": f} for f in doc.fields.all()
                  if f.name not in {"bl_number", "container_numbers", "po_numbers", *TABLE_FIELDS}]
    return render(request, "review/document.html", {
        "doc": doc, "key_rows": key_rows, "other_rows": other_rows,
        "shipments": Shipment.objects.filter(organization=doc.organization)
        .exclude(status__in=["approved", "posted"]).order_by("-updated_at")[:50],
        "timeline": [e for e in _doc_events(doc)], "doc_types": Document.DocType.choices,
    })


def _doc_events(doc: Document):
    from apps.core.models import AuditEvent
    from apps.shipments.labels import describe_action

    for e in AuditEvent.objects.filter(object_type="Document", object_id=str(doc.pk)).select_related("actor")[:50]:
        yield {"at": e.created_at, "who": (e.actor.get_full_name() or e.actor.get_username()) if e.actor else "ShipMatch",
               "text": describe_action(e.action, e.data)}


def _after_doc_change(doc: Document):
    doc.refresh_from_db()
    if hasattr(doc, "match"):
        return redirect(f"{reverse('review:shipment', args=[doc.match.shipment_id])}#doc-{doc.pk}")
    return redirect("review:document", pk=doc.pk)


@login_required
@require_POST
def update_field(request, pk):
    doc = _document_for(request, pk, "edit")
    name = request.POST.get("name", "")
    before = doc.match.shipment if hasattr(doc, "match") else None
    if before and before.is_locked:
        messages.error(request, f"{before.reference} is {before.get_status_display().lower()}, so it is locked. "
                                "Reopen it to make changes.")
        return _after_doc_change(doc)
    try:
        changed = correct_field(doc, name, request.POST.get("value", ""), request.user)
    except FieldValueError as e:
        messages.error(request, str(e))
        return _after_doc_change(doc)
    except ValueError:
        messages.error(request, f"{label(name)} can't be edited.")
        return _after_doc_change(doc)
    if changed is None:
        return _after_doc_change(doc)
    after_correction(doc, name)
    doc.refresh_from_db()
    after = doc.match.shipment if hasattr(doc, "match") else None
    if after and (before is None or before.pk != after.pk):
        messages.success(request, f"{label(name)} saved. {doc.original_filename} now matches {after.reference}, "
                                  "so it was moved there.")
    else:
        messages.success(request, f"{label(name)} saved.")
    return _after_doc_change(doc)


@login_required
@require_POST
def set_type(request, pk):
    """Reviewer fixes the document type; the document is read again as that type and re-matched."""
    from apps.documents.services.pipeline import process_document
    from apps.shipments.services.matching import refresh_keys

    doc = _document_for(request, pk, "edit")
    new_type = request.POST.get("doc_type", "")
    if new_type not in Document.DocType.values:
        messages.error(request, "Choose a document type.")
        return _after_doc_change(doc)
    before = doc.match.shipment if hasattr(doc, "match") else None
    if before and before.is_locked:
        messages.error(request, f"{before.reference} is locked. Reopen it to change document types.")
        return _after_doc_change(doc)
    if new_type == doc.doc_type:
        return _after_doc_change(doc)
    old_type = doc.get_doc_type_display()
    if not doc.text:  # scanned and not readable: keep the type so fields can be typed by hand
        doc.doc_type, doc.classification_confidence = new_type, 1.0
        doc.save(update_fields=["doc_type", "classification_confidence", "updated_at"])
    else:
        doc = process_document(doc.pk, force_type=new_type)
    audit(doc.organization, "document.type_changed", doc, actor=request.user, old=old_type,
          new=doc.get_doc_type_display())
    if before and Shipment.objects.filter(pk=before.pk).exists():
        if before.links.exists():
            refresh_keys(before)
            validate_shipment(before)
        else:
            before.delete()
    messages.success(request, f"Changed to {doc.get_doc_type_display().lower()} and read the document again.")
    return _after_doc_change(doc)


@login_required
@require_POST
def set_account(request, pk):
    """Learn: this vendor's invoices go to this expense account (saved per organization)."""
    doc = _document_for(request, pk, "edit")
    name = doc.field("vendor_name") or ""
    if not name:
        messages.error(request, "Add the vendor name first, then choose its expense account.")
        return _after_doc_change(doc)
    mapping, _ = VendorMapping.objects.get_or_create(
        organization=doc.organization, vendor_key=vendor_key(name), defaults={"display_name": name[:200]})
    account_id, account_name = set_rule_account(mapping, request.POST.get("account_id", ""),
                                                request.POST.get("account_name", ""))   # QuickBooks or Xero
    mapping.learned_from_correction = True
    mapping.save()
    audit(doc.organization, "vendor_mapping.updated", mapping, actor=request.user, vendor=mapping.display_name,
          account=account_id, name=account_name)
    messages.success(request, f"Saved. Invoices from {mapping.display_name} will be coded to "
                              f"{account_name or account_id or 'the default account'}.")
    return _after_doc_change(doc)


@login_required
@require_POST
def move_document(request, pk):
    doc = _document_for(request, pk, "edit")
    org = doc.organization
    current = doc.match.shipment if hasattr(doc, "match") else None
    if current and current.is_locked:
        messages.error(request, f"{current.reference} is locked. Reopen it before moving documents out of it.")
        return _after_doc_change(doc)
    target = request.POST.get("target", "")
    if target == "new":
        shipment = Shipment.objects.create(organization=org)
    else:
        shipment = Shipment.objects.filter(organization=org, pk=target if target.isdigit() else 0).first()
        if shipment is None:
            messages.error(request, "Choose a shipment to move the document to.")
            return _after_doc_change(doc)
    if shipment.is_locked:
        messages.error(request, f"{shipment.reference} is locked, so documents can't be added to it.")
        return _after_doc_change(doc)
    assign_manually(doc, shipment, request.user)
    validate_shipment(shipment)
    if current and current.pk != shipment.pk and Shipment.objects.filter(pk=current.pk).exists():
        validate_shipment(current)
    messages.success(request, f"Moved {doc.original_filename} to {shipment.reference}.")
    return redirect("review:shipment", pk=shipment.pk)


@login_required
@require_POST
def resolve_issue(request, pk):
    issue = get_object_or_404(ValidationIssue.objects.filter(organization__in=orgs_for_user(request.user))
                              .select_related("shipment", "organization"), pk=pk)
    use_org(request, issue.organization)
    is_error = issue.severity == ValidationIssue.Severity.ERROR
    require(request.user, issue.organization, "override_error" if is_error else "accept_warning")
    back = (f"{reverse('review:shipment', args=[issue.shipment_id])}#issues" if issue.shipment_id
            else reverse("review:queue"))
    if issue.resolved:
        return redirect(back)
    if issue.shipment and issue.shipment.is_locked:
        messages.error(request, "This shipment is locked. Reopen it first.")
        return redirect(back)
    note = request.POST.get("note", "").strip()[:500]
    if is_error and len(note) < OVERRIDE_NOTE_MIN:
        messages.error(request, f"To override “{issue.title}”, explain what you checked "
                                f"(at least {OVERRIDE_NOTE_MIN} characters). The note is kept in the audit log.")
        return redirect(back)
    issue.resolved, issue.resolved_by, issue.resolved_at, issue.resolution_note = True, request.user, timezone.now(), note
    issue.save(update_fields=["resolved", "resolved_by", "resolved_at", "resolution_note"])
    audit(issue.organization, "issue.resolved", issue, actor=request.user, code=issue.code,
          severity=issue.severity, note=note)
    if issue.shipment:
        update_status(issue.shipment)
    messages.success(request, f"{'Overrode' if is_error else 'Accepted'} “{issue.title}”.")
    return redirect(back)


# ---------------------------------------------------------------- decisions


@login_required
@require_POST
def approve(request, pk):
    shipment = _shipment_for(request, pk, "approve")
    blockers = approval_blockers(shipment, request.user)
    if blockers:
        messages.error(request, "Not approved. " + " ".join(blockers))
        return redirect("review:shipment", pk=pk)
    totals = shipment_totals(shipment)
    Approval.objects.create(shipment=shipment, user=request.user, decision=Approval.Decision.APPROVE,
                            note=request.POST.get("note", "").strip()[:500])
    shipment.status, shipment.approved_by, shipment.approved_at = Shipment.Status.APPROVED, request.user, timezone.now()
    shipment.save()
    audit(shipment.organization, "shipment.approved", shipment, actor=request.user,
          note=request.POST.get("note", "").strip()[:500], total_home=totals.home,
          totals={k: str(v) for k, v in totals.by_currency.items()})
    connected = active_connection(shipment.organization)
    messages.success(request, f"{shipment.reference} approved."
                     + (f" Post the bills to {connected.system_name} when you're ready." if connected else ""))
    return redirect("review:shipment", pk=pk)


@login_required
@require_POST
def reject(request, pk):
    shipment = _shipment_for(request, pk, "approve")
    if shipment.is_locked:
        messages.error(request, f"{shipment.reference} is already {shipment.get_status_display().lower()}. Reopen it first.")
        return redirect("review:shipment", pk=pk)
    note = request.POST.get("note", "").strip()[:500]
    if len(note) < REJECT_NOTE_MIN:
        messages.error(request, "Give a reason for rejecting, so the team knows what to fix.")
        return redirect("review:shipment", pk=pk)
    Approval.objects.create(shipment=shipment, user=request.user, decision=Approval.Decision.REJECT, note=note)
    shipment.status = Shipment.Status.REJECTED
    shipment.save(update_fields=["status", "updated_at"])
    audit(shipment.organization, "shipment.rejected", shipment, actor=request.user, note=note)
    messages.info(request, f"{shipment.reference} rejected.")
    return redirect("review:shipment", pk=pk)


@login_required
@require_POST
def reopen(request, pk):
    shipment = _shipment_for(request, pk, "approve")
    if shipment.status == Shipment.Status.POSTED:
        messages.error(request, "Posted shipments can't be reopened. Void the bills in your accounting system first.")
        return redirect("review:shipment", pk=pk)
    shipment.status, shipment.approved_by, shipment.approved_at = Shipment.Status.OPEN, None, None
    shipment.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    validate_shipment(shipment)
    audit(shipment.organization, "shipment.reopened", shipment, actor=request.user)
    messages.success(request, f"{shipment.reference} reopened for changes.")
    return redirect("review:shipment", pk=pk)


@login_required
@require_POST
def post_to_qbo(request, pk):
    from apps.accounting.tasks import post_shipment_task

    shipment = _shipment_for(request, pk, "post")
    conn = active_connection(shipment.organization)   # QuickBooks or Xero
    held = posting_blockers(shipment)
    if shipment.status != Shipment.Status.APPROVED:
        messages.error(request, "Approve the shipment before posting it.")
    elif held:
        messages.error(request, "Not posted. " + " ".join(held))
    elif conn is None:
        messages.error(request, "Connect QuickBooks or Xero in Settings > Accounting before posting.")
    else:
        post_shipment_task.delay(shipment.pk, request.user.pk)
        audit(shipment.organization, "shipment.post_requested", shipment, actor=request.user, system=conn.system_name)
        messages.success(request, f"Posting to {conn.system_name} started. Bill numbers appear on each invoice when done.")
    return redirect("review:shipment", pk=pk)
