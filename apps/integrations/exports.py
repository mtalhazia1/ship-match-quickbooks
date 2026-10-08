"""CSV and Excel exports of shipments, documents (with every value read) and validation issues.

The filters are the list pages' own (status tab, search, document view and type), so an export holds what the
person was looking at. Rows are produced one by one from the database in chunks (CSV is streamed to the
browser; Excel is written in write-only mode to a temporary file), and every row goes through
apps.core.csvsafe so a vendor name like "=HYPERLINK(...)" stays text in Excel.
"""
from __future__ import annotations

import csv
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from django.conf import settings
from django.db.models import Prefetch
from django.urls import reverse
from django.utils import timezone

from apps.core import csvsafe
from apps.core.context_processors import LOOSE_STATUSES
from apps.documents.models import Document, ExtractedField
from apps.documents.schemas import SCHEMAS
from apps.shipments.models import MatchLink, Shipment, ValidationIssue

KINDS = ("shipments", "documents", "issues")
FORMATS = ("csv", "xlsx")
CHUNK = 200
SHIPMENT_TABS = ("needs_review", "ready", "approved", "posted", "rejected", "open", "all")
DOC_VIEWS = ("attention", "matched", "all")


class ExportError(ValueError):
    pass


@dataclass
class Export:
    kind: str
    header: list[str]
    rows: Iterator[list]
    filename: str


# --------------------------------------------------------------------------- filters (as on the list pages)


def _search_shipments(qs, q: str):
    from apps.shipments.views import _search_shipments as search

    return search(qs, q)


def _search_documents(qs, q: str):
    from apps.shipments.views import _search_documents as search

    return search(qs, q)


def _date_param(params, key: str):
    raw = (params.get(key) or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise ExportError(f"'{raw}' isn't a date. Use the form 2026-03-31.")


def _date_range(qs, params, field: str):
    tz = timezone.get_current_timezone()
    start, end = _date_param(params, "from"), _date_param(params, "to")
    if start:
        qs = qs.filter(**{f"{field}__gte": datetime.combine(start, datetime.min.time(), tzinfo=tz)})
    if end:
        qs = qs.filter(**{f"{field}__lte": datetime.combine(end, datetime.max.time(), tzinfo=tz)})
    return qs


def shipment_queryset(org, params):
    status = params.get("status") or "needs_review"
    if status not in SHIPMENT_TABS:
        raise ExportError("Choose a shipment status: needs_review, ready, approved, posted, rejected, open or all.")
    qs = Shipment.objects.filter(organization=org)
    if status != "all":
        qs = qs.filter(status=status)
    qs = _search_shipments(qs, params.get("q") or "")
    return _date_range(qs, params, "created_at")


def document_queryset(org, params):
    view = params.get("view") or "attention"
    if view not in DOC_VIEWS:
        raise ExportError("Choose a document view: attention, matched or all.")
    qs = Document.objects.filter(organization=org)
    if view == "attention":
        qs = qs.filter(status__in=LOOSE_STATUSES)
    elif view == "matched":
        qs = qs.filter(status=Document.Status.MATCHED)
    doc_type = params.get("type") or ""
    if doc_type:
        if doc_type not in Document.DocType.values:
            raise ExportError("Unknown document type.")
        qs = qs.filter(doc_type=doc_type)
    qs = _search_documents(qs, params.get("q") or "")
    return _date_range(qs, params, "received_at")


# --------------------------------------------------------------------------- values


def _local(value):
    if isinstance(value, datetime):
        return timezone.localtime(value).replace(tzinfo=None, microsecond=0) if value.tzinfo else value
    return value


def _text(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v not in (None, ""))
    return str(value)


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01")) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _money_text(amounts: dict[str, Decimal]) -> str:
    return "; ".join(f"{cur} {amount:,.2f}" for cur, amount in sorted(amounts.items()))


_SENTINEL = 2147000001


@lru_cache(maxsize=64)
def _link_template(name: str, site_url: str) -> str:
    return f"{site_url}{reverse(name, args=[_SENTINEL])}".replace(str(_SENTINEL), "{}")


def _link(name: str, pk) -> str:
    # The URL pattern is resolved once per export, not once per row (a 6,000-row export saves seconds).
    return _link_template(name, settings.SITE_URL).format(pk)


def _user(u) -> str:
    return (u.get_full_name() or u.get_username()) if u else ""


def _payment_fields(model) -> list:
    """Payment columns another feature may have added (payment_status, paid_at, payment_reference ...)."""
    return [f for f in model._meta.concrete_fields
            if f.name.startswith(("payment", "paid")) and not f.is_relation]


def _label(name: str) -> str:
    return str(name).replace("_", " ").capitalize().replace("Bl ", "B/L ").replace("Po ", "PO ")


# --------------------------------------------------------------------------- shipments


def _shipment_docs(s) -> list[Document]:
    return [link.document for link in s.links.all()]


def _totals(org, docs) -> tuple[dict, Decimal | None, dict, int, int]:
    by_cur: dict[str, Decimal] = {}
    credits: dict[str, Decimal] = {}
    invoices = credit_notes = 0
    for d in docs:
        if not d.posts_to_accounting:
            continue
        data = {f.name: f.value for f in d.fields.all()}
        amount = _dec(data.get("total_amount"))
        if amount is None:
            continue
        cur = str(data.get("currency") or org.home_currency).upper()
        if d.is_credit:
            credit_notes += 1
            credits[cur] = credits.get(cur, Decimal("0.00")) + abs(amount)
            amount = -abs(amount)
        else:
            invoices += 1
        by_cur[cur] = by_cur.get(cur, Decimal("0.00")) + amount
    home: Decimal | None = Decimal("0.00")
    for cur, amount in by_cur.items():
        converted = org.to_home(amount, cur)
        if converted is None:
            home = None
            break
        home += converted
    return by_cur, home, credits, invoices, credit_notes


def shipments_export(org, params) -> Export:
    from apps.shipments.labels import issue_title

    pay_fields = _payment_fields(Shipment)
    from apps.accounting.models import PostedBill

    bill_pay_fields = _payment_fields(PostedBill)
    home = org.home_currency
    header = ["Shipment", "Status", "Bill of lading", "Containers", "PO numbers", "Documents", "Invoices",
              "Credit notes", "Total by currency", f"Total ({home})", "Credits by currency", "Open errors",
              "Open warnings", "Money at risk", "Open issues", "Approved by", "Approved at", "Posted to accounting",
              "Accounting ids", "Posting errors"]
    header += [_label(f.name) for f in pay_fields]
    header += [f"Bill {_label(f.name).lower()}" for f in bill_pay_fields]
    header += ["Created", "Updated", "Link"]
    qs = (shipment_queryset(org, params).select_related("approved_by")
          .prefetch_related(Prefetch("links", queryset=MatchLink.objects.select_related("document")
                                     .prefetch_related(Prefetch(   # only the two values the totals need
                                         "document__fields",
                                         queryset=ExtractedField.objects.filter(name__in=("total_amount", "currency"))
                                         .only("id", "document_id", "name", "value"))),
                                     ),
                            Prefetch("issues", queryset=ValidationIssue.objects.filter(resolved=False)
                                     .order_by("id"), to_attr="open_issues"),
                            "posted_bills")
          .order_by("-updated_at", "-id"))
    status_names = dict(Shipment.Status.choices)

    def rows():
        for s in qs.iterator(chunk_size=CHUNK):
            docs = _shipment_docs(s)
            by_cur, home_total, credits, invoices, credit_notes = _totals(org, docs)
            errors = [i for i in s.open_issues if i.severity == "error"]
            at_risk: dict[str, Decimal] = {}
            for i in s.open_issues:
                if i.amount_at_risk:
                    cur = i.currency or home
                    at_risk[cur] = at_risk.get(cur, Decimal("0.00")) + i.amount_at_risk
            bills = list(s.posted_bills.all())
            posted = [b for b in bills if b.status == "posted"]
            row = [
                s.reference, status_names.get(s.status, s.status), s.bl_number, _text(s.container_numbers),
                _text(s.po_numbers), len(docs), invoices, credit_notes, _money_text(by_cur), home_total,
                _money_text(credits), len(errors), len(s.open_issues) - len(errors), _money_text(at_risk),
                "; ".join(sorted({issue_title(i.code) for i in s.open_issues})), _user(s.approved_by),
                _local(s.approved_at), f"{len(posted)} of {len(bills)}" if bills else "",
                _text([b.qbo_bill_id for b in posted if b.qbo_bill_id]),
                "; ".join(b.error[:200] for b in bills if b.status == "failed" and b.error),
            ]
            row += [_local(getattr(s, f.name)) for f in pay_fields]
            row += [_text([_local(getattr(b, f.name)) for b in bills if getattr(b, f.name, None) not in (None, "")])
                    for f in bill_pay_fields]
            row += [_local(s.created_at), _local(s.updated_at), _link("review:shipment", s.pk)]
            yield row

    return Export("shipments", header, rows(), f"shipmatch-shipments-{org.slug}-{timezone.localdate()}")


# --------------------------------------------------------------------------- documents


def _schema_fields() -> list[str]:
    names: list[str] = []
    for schema in SCHEMAS.values():
        for n in schema.model_fields:
            if n != "line_items" and n not in names:
                names.append(n)
    return names


def documents_export(org, params) -> Export:
    names = _schema_fields()
    threshold = org.review_threshold or settings.REVIEW_CONFIDENCE_THRESHOLD
    header = (["Document id", "File", "Type", "Status", "Source", "Format", "Received", "Shipment",
               "Shipment status"] + [_label(n) for n in names]
              + ["Line items", "Corrected by people", "Values to check", "Read by", "From archive or batch",
                 "Link"])
    qs = (document_queryset(org, params).select_related("match__shipment", "parent")
          .prefetch_related(Prefetch("fields", queryset=ExtractedField.objects.order_by("id")))
          .order_by("-received_at", "-id"))
    types, statuses = dict(Document.DocType.choices), dict(Document.Status.choices)
    sources, fmts = dict(Document.Source.choices), dict(Document.Format.choices)
    ship_status = dict(Shipment.Status.choices)

    def rows():
        for d in qs.iterator(chunk_size=CHUNK):
            fields = {f.name: f for f in d.fields.all()}
            shipment = d.match.shipment if hasattr(d, "match") else None
            values = []
            for n in names:
                f = fields.get(n)
                v = f.value if f else None
                values.append(_dec(v) if n == "total_amount" and _dec(v) is not None else _text(v))
            items = fields.get("line_items")
            yield ([d.pk, d.original_filename, types.get(d.doc_type, d.doc_type), statuses.get(d.status, d.status),
                    sources.get(d.source, d.source), fmts.get(d.source_format, d.source_format),
                    _local(d.received_at), shipment.reference if shipment else "",
                    ship_status.get(shipment.status, "") if shipment else ""] + values
                   + [len(items.value) if items and isinstance(items.value, list) else 0,
                      _text([_label(n) for n, f in fields.items() if f.source == ExtractedField.Source.HUMAN]),
                      _text([_label(n) for n, f in fields.items()
                             if f.source != ExtractedField.Source.HUMAN and f.confidence < threshold]),
                      d.extraction_provider, d.parent.original_filename if d.parent_id else "",
                      _link("review:document", d.pk)])

    return Export("documents", header, rows(), f"shipmatch-documents-{org.slug}-{timezone.localdate()}")


# --------------------------------------------------------------------------- issues


def issues_export(org, params) -> Export:
    shipments = shipment_queryset(org, params)
    qs = ValidationIssue.objects.filter(organization=org, shipment__in=shipments)
    if params.get("open") in ("1", "true", "yes"):
        qs = qs.filter(resolved=False)
    severity = params.get("severity") or ""
    if severity:
        if severity not in ("error", "warning"):
            raise ExportError("Severity is error or warning.")
        qs = qs.filter(severity=severity)
    qs = qs.select_related("shipment", "document", "resolved_by").order_by("shipment_id", "resolved", "id")
    header = ["Shipment", "Shipment status", "Document", "Severity", "Issue", "Code", "Details", "Money at risk",
              "Currency", "Open", "Accepted or overridden by", "When", "Note", "Found", "Link"]
    ship_status = dict(Shipment.Status.choices)

    def rows():
        for i in qs.iterator(chunk_size=CHUNK):
            yield [i.shipment.reference if i.shipment_id else "",
                   ship_status.get(i.shipment.status, "") if i.shipment_id else "",
                   i.document.original_filename if i.document_id else "",
                   "Error" if i.severity == "error" else "Warning", i.title, i.code, i.message,
                   i.amount_at_risk, i.currency, "No" if i.resolved else "Yes", _user(i.resolved_by),
                   _local(i.resolved_at), i.resolution_note, _local(i.created_at),
                   _link("review:shipment", i.shipment_id) if i.shipment_id else ""]

    return Export("issues", header, rows(), f"shipmatch-issues-{org.slug}-{timezone.localdate()}")


BUILDERS = {"shipments": shipments_export, "documents": documents_export, "issues": issues_export}


def build(org, kind: str, params) -> Export:
    if kind not in BUILDERS:
        raise ExportError("Export shipments, documents or issues.")
    return BUILDERS[kind](org, params)


# --------------------------------------------------------------------------- writers


class _Echo:
    def write(self, value):
        return value


def _csv_value(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return ""
    return value


def csv_chunks(export: Export) -> Iterator[str]:
    """UTF-8 with a byte order mark, so Excel shows accented vendor names correctly."""
    writer = csv.writer(_Echo())
    yield "﻿" + writer.writerow(csvsafe.row(export.header))
    for row in export.rows:
        yield writer.writerow(csvsafe.row([_csv_value(v) for v in row]))


def _xlsx_value(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def write_xlsx(export: Export):
    """Write the workbook to a temporary file (write-only mode keeps memory flat) and return it, rewound."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(export.kind.capitalize())
    ws.freeze_panes = "A2"
    bold = Font(bold=True)
    head = []
    for title in csvsafe.row(export.header):
        c = WriteOnlyCell(ws, value=title)
        c.font = bold
        head.append(c)
    ws.append(head)
    limit = settings.EXPORT_XLSX_MAX_ROWS
    for n, row in enumerate(export.rows):
        if n >= limit:
            ws.append([f"Stopped after {limit:,} rows. Use the CSV export (no limit) or narrow the filters."])
            break
        cells = []
        for value in csvsafe.row([_xlsx_value(v) for v in row]):
            cell = WriteOnlyCell(ws, value=value)
            if isinstance(value, str):
                cell.data_type = "s"  # never a formula, whatever the text starts with
            elif isinstance(value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm"
            cells.append(cell)
        ws.append(cells)
    out = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, suffix=".xlsx")
    wb.save(out)
    out.seek(0)
    return out


def filters_for_audit(params) -> dict:
    keep = ("status", "q", "view", "type", "from", "to", "open", "severity")
    return {k: str(params.get(k))[:100] for k in keep if params.get(k)}
