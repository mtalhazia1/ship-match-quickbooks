"""Pages and actions for files that became several documents, and for originals of converted files."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.core.permissions import require
from apps.core.utils import orgs_for_user, use_org
from apps.documents.models import Document

from .services import formats
from .services.archives import summary
from .services.splitting import SplitLocked
from .services.splitting import keep_whole as keep_whole_service


def _document_for(request, pk, perm: str = "view") -> Document:
    doc = get_object_or_404(Document.objects.filter(organization__in=orgs_for_user(request.user))
                            .select_related("organization", "parent"), pk=pk)
    use_org(request, doc.organization)
    require(request.user, doc.organization, perm)
    return doc


def _back_to(doc: Document):
    doc.refresh_from_db()
    if hasattr(doc, "match"):
        return redirect(f"{reverse('review:shipment', args=[doc.match.shipment_id])}#doc-{doc.pk}")
    return redirect("review:document", pk=doc.pk)


@login_required
def original_file(request, pk):
    """The file exactly as it was received (a photo, a spreadsheet or a ZIP), for download."""
    doc = _document_for(request, pk)
    if not doc.original_file:
        return redirect("review:document_file", pk=doc.pk)
    subtype = (doc.intake or {}).get("subtype") or ("zip" if doc.source_format == Document.Format.ARCHIVE else "")
    response = FileResponse(doc.original_file.open("rb"), as_attachment=True, filename=doc.original_filename,
                            content_type=formats.media_type(doc.source_format, subtype))
    response["Cache-Control"] = "private, max-age=300"
    return response


@login_required
@require_POST
def keep_whole(request, pk):
    """Undo a wrong split: the parts are removed and the original file is read again as one document."""
    doc = _document_for(request, pk, "edit")
    parts = doc.children.count()
    try:
        doc = keep_whole_service(doc, request.user)
    except SplitLocked as e:
        messages.error(request, f"Not changed. {e}")
        return redirect("review:document", pk=doc.pk)
    messages.success(request, f"{doc.original_filename} is one document again. Its {parts} parts were removed and "
                              "the file was read as a whole.")
    return _back_to(doc)


def container_detail(request, doc: Document):
    """The page of a ZIP archive or a split PDF: what came out of it. Called by review:document."""
    from apps.shipments.views import _doc_events

    children = list(doc.children.select_related("match__shipment").order_by("id"))
    by_id = {c.pk: c for c in children}
    info = doc.intake or {}
    rows = []
    if doc.status == Document.Status.SPLIT:
        for part in (info.get("split") or {}).get("parts") or []:
            child = by_id.get(part.get("document")) or Document.objects.filter(
                organization=doc.organization, pk=part.get("document")).select_related("match__shipment").first()
            rows.append({"doc": child, "pages": part.get("pages"), "duplicate": part.get("duplicate")})
    else:
        for m in (info.get("members") or []):
            if m["status"] in ("added", "duplicate"):
                child = by_id.get(m.get("document")) or Document.objects.filter(
                    organization=doc.organization, pk=m.get("document")).select_related("match__shipment").first()
                folder = m["name"].replace("\\", "/").rsplit("/", 1)[0] if "/" in m["name"].replace("\\", "/") else ""
                rows.append({"doc": child, "name": m["name"], "folder": folder, "duplicate": m["status"] == "duplicate"})
    return render(request, "intake/container.html", {
        "doc": doc, "rows": rows, "archive": summary(doc) if doc.status == Document.Status.ARCHIVE else None,
        "split": info.get("split") or {}, "timeline": list(_doc_events(doc)),
        "locked": [r for r in rows if r["doc"] and hasattr(r["doc"], "match") and r["doc"].match.shipment.is_locked],
    })
