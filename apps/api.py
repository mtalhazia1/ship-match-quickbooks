"""JSON API (Django Ninja).

Two ways to authenticate:
  * Integrations: an organization API key in the header  Authorization: Bearer sm_<prefix>_<secret>
    (create keys in Settings > API keys; a key only reaches its own organization). Each key has scopes
    (shipments:read, documents:read, exports:read, documents:write); keys made before scopes have them all
    for their access level (apps/integrations/apiscopes.py).
  * The browser: the signed-in session (CSRF enforced); the role decides, scopes don't apply.

Requests are rate limited per key or per user (API_RATE_LIMIT_PER_MINUTE).
"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Literal, Optional

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q
from django.http import FileResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from ninja import File, NinjaAPI, Schema
from ninja.errors import HttpError
from ninja.files import UploadedFile
from ninja.security import HttpBearer
from ninja.security.session import SessionAuth

from apps.accounts.models import ApiKey
from apps.accounts.services.apikeys import verify
from apps.core.permissions import PERMISSION_MIN_ROLE, ROLE_RANK, has_perm
from apps.core.utils import audit, orgs_for_user
from apps.documents.models import Document
from apps.documents.services.ingest import RejectedFile, ingest_bytes
from apps.integrations import apiscopes
from apps.shipments.models import Shipment


class ApiKeyAuth(HttpBearer):
    def authenticate(self, request, token):
        key = verify(token)
        if key is None:
            return None
        request.api_key = key
        return key


class BrowserSessionAuth(SessionAuth):
    """The signed-in browser session, unless the request presented a Bearer key: a bad key must not fall back."""

    def authenticate(self, request, key):
        if request.headers.get("Authorization", "").lower().startswith("bearer "):
            return None
        return super().authenticate(request, key)


api = NinjaAPI(title="ShipMatch API", version="1.2", auth=[ApiKeyAuth(), BrowserSessionAuth()],
               description="Upload shipping documents, read reconciled shipments with their issues, read documents "
                           "with every value read, and download exports.\n\n"
                           "Authenticate with `Authorization: Bearer sm_...` (Settings > API keys). Each key has "
                           "scopes: `shipments:read`, `documents:read`, `exports:read` and `documents:write` "
                           "(upload). A request outside the key's scopes gets 403 with the scope it needs. "
                           "Keys created before scopes existed have every scope their access level allows.\n\n"
                           "To hear about changes instead of polling, add an outgoing webhook in "
                           "Settings > Webhooks.")


class FieldOut(Schema):
    name: str
    value: Any = None
    confidence: float
    grounded: bool
    source: str


class SkippedFileOut(Schema):
    name: str
    reason: str


class DocumentOut(Schema):
    id: int
    original_filename: str
    doc_type: str
    status: str
    received_at: datetime
    shipment: Optional[str] = None
    fields: list[FieldOut]
    source_format: str = "pdf"
    parent: Optional[int] = None
    children: list[int] = []
    not_added: list[SkippedFileOut] = []

    @staticmethod
    def resolve_shipment(obj):
        return obj.match.shipment.reference if hasattr(obj, "match") else None

    @staticmethod
    def resolve_fields(obj):
        return list(obj.fields.all())

    @staticmethod
    def resolve_parent(obj):
        return obj.parent_id

    @staticmethod
    def resolve_children(obj):
        """Documents made from this file: the files of a ZIP, or the parts of a split PDF."""
        return list(obj.children.order_by("id").values_list("id", flat=True))

    @staticmethod
    def resolve_not_added(obj):
        members = (obj.intake or {}).get("members") or []
        return [{"name": m["name"], "reason": m.get("reason") or m["status"]} for m in members
                if m["status"] in ("skipped", "duplicate")]


class IssueOut(Schema):
    id: int = 0
    code: str
    title: str = ""
    severity: str
    message: str
    resolved: bool
    document_id: Optional[int] = None
    amount_at_risk: Optional[str] = None
    currency: str = ""
    resolution_note: str = ""

    @staticmethod
    def resolve_title(obj):
        return obj.title

    @staticmethod
    def resolve_amount_at_risk(obj):
        return str(obj.amount_at_risk) if obj.amount_at_risk is not None else None


class ShipmentOut(Schema):
    id: int
    reference: str
    status: str
    bl_number: str
    container_numbers: list[str]
    po_numbers: list[str]
    updated_at: datetime
    created_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    open_errors: int = 0
    open_warnings: int = 0

    @staticmethod
    def resolve_open_errors(obj):
        n = getattr(obj, "n_errors", None)
        return n if n is not None else obj.issues.filter(resolved=False, severity="error").count()

    @staticmethod
    def resolve_open_warnings(obj):
        n = getattr(obj, "n_warnings", None)
        return n if n is not None else obj.issues.filter(resolved=False, severity="warning").count()


class TotalsOut(Schema):
    by_currency: dict[str, str]
    credits: dict[str, str]
    home_currency: str
    home: Optional[str] = None
    missing_rates: list[str] = []


class ShipmentDetailOut(ShipmentOut):
    documents: list[DocumentOut]
    issues: list[IssueOut]
    totals: Optional[TotalsOut] = None

    @staticmethod
    def resolve_documents(obj):
        return list(obj.documents.prefetch_related("fields"))

    @staticmethod
    def resolve_issues(obj):
        return list(obj.issues.all())

    @staticmethod
    def resolve_totals(obj):
        from apps.shipments.services.approval import shipment_totals

        t = shipment_totals(obj)
        return {"by_currency": {k: str(v) for k, v in t.by_currency.items()},
                "credits": {k: str(v) for k, v in t.credits.items()},
                "home_currency": obj.organization.home_currency,
                "home": str(t.home) if t.home is not None else None, "missing_rates": t.missing_rates}


class ErrorOut(Schema):
    detail: str


def _rate_limit(request) -> None:
    limit = settings.API_RATE_LIMIT_PER_MINUTE
    if not limit:
        return
    who = f"key:{request.api_key.pk}" if getattr(request, "api_key", None) else f"user:{request.user.pk}"
    bucket = f"api-rate:{who}:{int(time.time() // 60)}"
    cache.add(bucket, 0, 70)
    try:
        n = cache.incr(bucket)
    except ValueError:  # expired between add and incr
        cache.set(bucket, 1, 70)
        n = 1
    if n > limit:
        raise HttpError(429, f"Rate limit is {limit} requests per minute. Try again shortly.")


def _refuse(request, org, perm: str, message: str, key=None):
    from apps.core.errors import record_denial

    record_denial(request, org=org, permission=perm, reason=message, api_key=key)
    raise HttpError(403, message)


def _org(request, slug: str, perm: str = "view", scope: str = ""):
    """The organization in the URL, if the caller may use it for `perm` (and, for an API key, `scope`)."""
    _rate_limit(request)
    key: ApiKey | None = getattr(request, "api_key", None)
    if key is not None:
        if key.organization.slug != slug:
            raise HttpError(404, "Not found")
        if ROLE_RANK[key.role] < ROLE_RANK[PERMISSION_MIN_ROLE[perm]]:
            _refuse(request, key.organization, perm, "This API key is read only. Create a key with upload access.", key)
        if scope and not apiscopes.allows(key, scope):
            _refuse(request, key.organization, perm, f"This API key doesn't have the {scope} scope. Create a key that "
                                                      "includes it in Settings > API keys.", key)
        return key.organization
    org = get_object_or_404(orgs_for_user(request.user), slug=slug)
    if not has_perm(request.user, org, perm):
        _refuse(request, org, perm, "Your role does not allow this.")
    return org


@api.post("/{org}/documents", response={201: DocumentOut, 200: DocumentOut, 400: ErrorOut, 402: ErrorOut},
          summary="Upload a document: PDF, JPG, PNG, TIFF, WebP, XLSX, CSV or ZIP (same file twice returns the "
                  "existing document)")
def upload_document(request, org: str, file: UploadedFile = File(...)):
    """A ZIP returns the archive's own document; `children` lists the documents made from its files and
    `not_added` the files that were skipped or received before. A PDF holding several invoices is split
    after reading: its `children` are filled once processing has finished. Scope: documents:write.
    402 means the organization's plan has paused new documents (the message says why and until when)."""
    organization = _org(request, org, "upload", "documents:write")
    key = getattr(request, "api_key", None)
    from apps.billing.usage import UsageLimitReached

    try:
        doc, created = ingest_bytes(organization, file.name, file.read(), source=Document.Source.UPLOAD,
                                    actor=None if key else request.user)
    except UsageLimitReached as e:
        return 402, {"detail": str(e)}
    except RejectedFile as e:
        return 400, {"detail": str(e)}
    if key and created:
        audit(organization, "api.document_uploaded", doc, api_key=key.name, prefix=key.prefix)
    return (201 if created else 200), doc


def _paging(limit: int, offset: int) -> tuple[int, int]:
    """limit 1-500 (a larger one is capped at 500), offset 0 or more; anything else is a mistake worth saying so."""
    if limit < 1:
        raise HttpError(422, "limit must be 1 or more (at most 500).")
    if offset < 0:
        raise HttpError(422, "offset can't be negative.")
    return min(limit, 500), offset


@api.get("/{org}/documents", response=list[DocumentOut], summary="Documents with extracted fields, newest first")
def list_documents(request, org: str, view: Literal["attention", "matched", "all"] = "all",
                   type: Optional[str] = None, status: Optional[str] = None, q: Optional[str] = None,
                   received_from: Optional[date] = None, received_to: Optional[date] = None,
                   limit: int = 100, offset: int = 0):
    """Scope: documents:read. `view` as on the Documents page (attention = not in a shipment). `q` searches file
    names, invoice numbers, vendors, B/L, containers and POs. Dates are the organization's local days."""
    from apps.integrations.exports import document_queryset

    organization = _org(request, org, "view", "documents:read")
    params = {"view": view, "type": type or "", "q": q or "",
              "from": received_from.isoformat() if received_from else "",
              "to": received_to.isoformat() if received_to else ""}
    try:
        qs = document_queryset(organization, params)
    except ValueError as e:
        raise HttpError(400, str(e))
    if status:
        if status not in Document.Status.values:
            raise HttpError(422, f"Unknown status “{status}”. Use one of: {', '.join(Document.Status.values)}.")
        qs = qs.filter(status=status)
    limit, offset = _paging(limit, offset)
    return list(qs.select_related("match__shipment").prefetch_related("fields", "children")
                .order_by("-received_at", "-id")[offset:offset + limit])


@api.get("/{org}/documents/{doc_id}", response=DocumentOut, summary="One document with extracted fields")
def get_document(request, org: str, doc_id: int):
    """Scope: documents:read."""
    return get_object_or_404(Document, organization=_org(request, org, "view", "documents:read"), pk=doc_id)


@api.get("/{org}/shipments", response=list[ShipmentOut], summary="Shipments, newest activity first")
def list_shipments(request, org: str,
                   status: Optional[Literal["open", "needs_review", "ready", "approved", "posted", "rejected"]] = None,
                   q: Optional[str] = None, limit: int = 100, offset: int = 0):
    """Scope: shipments:read. `status`: needs_review, ready, approved, posted, rejected or open (empty = all).
    `q` searches references, B/L, containers, POs, invoice numbers, vendors and file names."""
    from apps.integrations.exports import _search_shipments

    qs = Shipment.objects.filter(organization=_org(request, org, "view", "shipments:read"))
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = _search_shipments(qs, q)
    qs = qs.annotate(
        n_errors=Count("issues", filter=Q(issues__resolved=False, issues__severity="error"), distinct=True),
        n_warnings=Count("issues", filter=Q(issues__resolved=False, issues__severity="warning"), distinct=True),
    ).order_by("-updated_at", "-id")
    limit, offset = _paging(limit, offset)
    return qs[offset:offset + limit]


@api.get("/{org}/shipments/{shipment_id}", response=ShipmentDetailOut,
         summary="One shipment with documents, totals and issues")
def get_shipment(request, org: str, shipment_id: int):
    """Scope: shipments:read. Totals are payable invoices net of credit notes, per currency and in the home
    currency (empty when an exchange rate is missing; see missing_rates)."""
    return get_object_or_404(Shipment.objects.select_related("organization"),
                             organization=_org(request, org, "view", "shipments:read"), pk=shipment_id)


@api.get("/{org}/exports/{kind}", response={200: None, 400: ErrorOut},
         summary="Download an export: shipments, documents or issues, as CSV or Excel",
         openapi_extra={"responses": {200: {"description": "The file", "content": {
             "text/csv": {"schema": {"type": "string"}},
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                 "schema": {"type": "string", "format": "binary"}}}}}})
def export(request, org: str, kind: Literal["shipments", "documents", "issues"],
           format: Literal["csv", "xlsx"] = "csv", status: Optional[str] = None, view: Optional[str] = None,
           type: Optional[str] = None, q: Optional[str] = None, open: Optional[bool] = None,
           severity: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None):
    """Scope: exports:read. The same files as the Export buttons in ShipMatch, with the same filters:
    shipments and issues take `status` (default needs_review; `all` for every shipment) and `q`; documents take
    `view` (attention, matched, all; default attention), `type` and `q`; issues also `open` and `severity`.
    `date_from`/`date_to` limit by the day the shipment was created or the document received. CSV is
    streamed (UTF-8 with BOM); every cell that could run as a spreadsheet formula is prefixed with '."""
    from apps.integrations import exports

    organization = _org(request, org, "view", "exports:read")
    params = {"status": status or "",
              "view": view or "", "type": type or "", "q": q or "", "open": "1" if open else "",
              "severity": severity or "", "from": date_from.isoformat() if date_from else "",
              "to": date_to.isoformat() if date_to else ""}
    try:
        built = exports.build(organization, kind, params)
    except exports.ExportError as e:
        return 400, {"detail": str(e)}
    key = getattr(request, "api_key", None)
    audit(organization, "export.downloaded", organization, actor=None if key else request.user, kind=kind,
          format=format, filters=exports.filters_for_audit(params), api_key=key.name if key else "")
    if format == "xlsx":
        return FileResponse(exports.write_xlsx(built), as_attachment=True, filename=f"{built.filename}.xlsx",
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response = StreamingHttpResponse(exports.csv_chunks(built), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{built.filename}.csv"'
    return response
