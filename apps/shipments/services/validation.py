"""Validation rules. Plain code, no AI: money math and duplicate checks must never guess.

Each rule yields IssueSpec objects. Running validation replaces a shipment's unresolved
issues with the current findings; issues a reviewer already resolved are not raised again
(matched by fingerprint).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from statistics import median

from django.conf import settings
from django.db import transaction

from apps.accounting.models import vendor_key
from apps.core.utils import audit
from apps.documents.models import Document
from apps.documents.schemas import REQUIRED_FIELDS
from apps.documents.services.normalize import norm_ref
from apps.shipments.models import MatchLink, Shipment, ValidationIssue

from .containers import is_valid_container

ERROR, WARNING = ValidationIssue.Severity.ERROR, ValidationIssue.Severity.WARNING
TOTAL_TOLERANCE = Decimal("0.02")
OUTLIER_HIGH, OUTLIER_LOW, OUTLIER_MIN_HISTORY = Decimal("2.5"), Decimal("0.4"), 3


@dataclass
class IssueSpec:
    code: str
    severity: str
    message: str
    document: Document | None = None
    data: dict = field(default_factory=dict)
    amount_at_risk: Decimal | None = None  # money saved if this is caught (overcharge, duplicate, ...)
    currency: str = ""

    @property
    def fingerprint(self) -> str:
        return f"{self.code}:{self.document.pk if self.document else '-'}:{self.data.get('key', '')}"


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v)).quantize(Decimal("0.01")) if v not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


# --------------------------------------------------------------------------- document rules


def check_required(doc: Document, data: dict):
    missing = [f for f in REQUIRED_FIELDS.get(doc.doc_type, []) if data.get(f) in (None, "", [])]
    if missing:
        yield IssueSpec("missing_field", ERROR, f"{doc.original_filename}: missing {', '.join(missing)}", doc,
                        {"fields": missing})


def check_total(doc: Document, data: dict):
    total, items = _dec(data.get("total_amount")), data.get("line_items") or []
    if total is None or not items:
        return
    line_sum = sum((_dec(i.get("amount")) or Decimal("0") for i in items), Decimal("0.00"))
    if abs(line_sum - total) > TOTAL_TOLERANCE:
        yield IssueSpec("total_mismatch", ERROR,
                        f"{doc.original_filename}: lines add up to {line_sum:,.2f} but the printed total is {total:,.2f}",
                        doc, {"line_sum": str(line_sum), "total": str(total)},
                        amount_at_risk=(total - line_sum) if total > line_sum else None,
                        currency=data.get("currency") or "")


def check_containers_valid(doc: Document, data: dict):
    for c in data.get("container_numbers") or []:
        if not is_valid_container(c):
            yield IssueSpec("invalid_container", ERROR,
                            f"{doc.original_filename}: container {c} fails the ISO 6346 check digit (likely a typo)",
                            doc, {"key": norm_ref(c)})


def check_low_confidence(doc: Document, threshold: float):
    low = [f.name for f in doc.fields.all() if f.confidence < threshold and f.source != "human"]
    if low:
        yield IssueSpec("low_confidence", WARNING,
                        f"{doc.original_filename}: please confirm {', '.join(low)}", doc, {"fields": low})


def check_duplicate(doc: Document, data: dict):
    """Same vendor + invoice number + amount already received (in any shipment)."""
    if not doc.is_payable or not data.get("invoice_number"):
        return
    vk, num, amt = vendor_key(data.get("vendor_name")), norm_ref(data.get("invoice_number")), _dec(data.get("total_amount"))
    earlier = (Document.objects.filter(organization=doc.organization, doc_type=doc.doc_type)
               .exclude(pk=doc.pk).filter(received_at__lte=doc.received_at).prefetch_related("fields"))
    for other in earlier:
        od = other.data()
        if (norm_ref(od.get("invoice_number")) == num and vendor_key(od.get("vendor_name")) == vk
                and _dec(od.get("total_amount")) == amt and other.pk < doc.pk):
            yield IssueSpec("duplicate_invoice", ERROR,
                            f"{doc.original_filename}: same vendor, invoice number {data.get('invoice_number')} and amount "
                            f"as {other.original_filename}. Do not pay twice.", doc, {"duplicate_of": other.pk},
                            amount_at_risk=amt, currency=data.get("currency") or "")
            return


def check_outlier(doc: Document, data: dict):
    """Per-container cost far outside this vendor's history."""
    if doc.doc_type != Document.DocType.FREIGHT_INVOICE:
        return
    total = _dec(data.get("total_amount"))
    if total is None:
        return
    per_box = total / max(1, len(data.get("container_numbers") or []))
    vk = vendor_key(data.get("vendor_name"))
    history = []
    others = (Document.objects.filter(organization=doc.organization, doc_type=doc.doc_type).exclude(pk=doc.pk)
              .prefetch_related("fields"))
    for other in others:
        od = other.data()
        if vendor_key(od.get("vendor_name")) == vk and _dec(od.get("total_amount")):
            history.append(_dec(od["total_amount"]) / max(1, len(od.get("container_numbers") or [])))
    if len(history) < OUTLIER_MIN_HISTORY:
        return
    typical = Decimal(str(median(history))).quantize(Decimal("0.01"))
    if per_box > typical * OUTLIER_HIGH or per_box < typical * OUTLIER_LOW:
        yield IssueSpec("amount_outlier", WARNING,
                        f"{doc.original_filename}: {per_box:,.2f} per container vs typical {typical:,.2f} for this vendor",
                        doc, {"per_container": str(per_box), "typical": str(typical)},
                        amount_at_risk=((per_box - typical) * max(1, len(data.get("container_numbers") or []))
                                        ).quantize(Decimal("0.01")) if per_box > typical else None,
                        currency=data.get("currency") or "")


# --------------------------------------------------------------------------- shipment rules


def check_shipment(shipment: Shipment, docs: list[Document]):
    types = {d.doc_type for d in docs}
    if Document.DocType.BILL_OF_LADING not in types:
        yield IssueSpec("missing_bl", WARNING, "No bill of lading received for this shipment yet")
    if Document.DocType.COMMERCIAL_INVOICE not in types:
        yield IssueSpec("missing_commercial_invoice", WARNING, "No commercial invoice received for this shipment yet")

    bl_containers = {norm_ref(c) for d in docs if d.doc_type == Document.DocType.BILL_OF_LADING
                     for c in (d.field("container_numbers") or [])}
    if bl_containers:
        for d in docs:
            if d.doc_type == Document.DocType.BILL_OF_LADING:
                continue
            for c in d.field("container_numbers") or []:
                c = norm_ref(c)
                if c not in bl_containers and is_valid_container(c):
                    yield IssueSpec("container_not_on_bl", ERROR,
                                    f"{d.original_filename}: container {c} is not on the bill of lading", d, {"key": c})

    for d in docs:
        link = getattr(d, "match", None)
        if link and link.method == MatchLink.Method.FUZZY:
            yield IssueSpec("fuzzy_match", WARNING, f"{d.original_filename}: matched by near-match ({link.reason}); confirm",
                            d)



# --------------------------------------------------------------------------- runners

# Other apps add rules without editing this file: call register_document_rule / register_shipment_rule
# from their AppConfig.ready(). Document rules take (doc, data); shipment rules take (shipment, docs).
DOCUMENT_RULES = [check_required, check_total, check_containers_valid, check_duplicate, check_outlier]
SHIPMENT_RULES = [check_shipment]


def register_document_rule(rule) -> None:
    if rule not in DOCUMENT_RULES:
        DOCUMENT_RULES.append(rule)


def register_shipment_rule(rule) -> None:
    if rule not in SHIPMENT_RULES:
        SHIPMENT_RULES.append(rule)


def collect(shipment: Shipment) -> list[IssueSpec]:
    docs = list(shipment.documents.select_related("match").prefetch_related("fields"))
    threshold = shipment.organization.review_threshold or settings.REVIEW_CONFIDENCE_THRESHOLD
    specs: list[IssueSpec] = []
    for rule in SHIPMENT_RULES:
        specs.extend(rule(shipment, docs))
    for d in docs:
        data = d.data()
        for rule in DOCUMENT_RULES:
            specs.extend(rule(d, data))
        specs.extend(check_low_confidence(d, threshold))
    return specs


@transaction.atomic
def validate_shipment(shipment: Shipment) -> list[ValidationIssue]:
    if shipment.is_locked:
        return list(shipment.issues.all())
    specs = collect(shipment)
    shipment.issues.filter(resolved=False).delete()
    resolved = set(shipment.issues.filter(resolved=True).values_list("fingerprint", flat=True))
    created = [
        ValidationIssue.objects.create(
            organization=shipment.organization, shipment=shipment, document=s.document, code=s.code,
            severity=s.severity, message=s.message[:500], fingerprint=s.fingerprint, data=s.data,
            amount_at_risk=s.amount_at_risk, currency=(s.currency or shipment.organization.home_currency)[:3],
        )
        for s in specs if s.fingerprint not in resolved
    ]
    update_status(shipment)
    audit(shipment.organization, "shipment.validated", shipment, issues=[i.code for i in created])
    return created


def update_status(shipment: Shipment) -> None:
    if shipment.is_locked or shipment.status == Shipment.Status.REJECTED:
        return
    open_issues = shipment.issues.filter(resolved=False)
    shipment.status = Shipment.Status.NEEDS_REVIEW if open_issues.exists() else Shipment.Status.READY
    shipment.save(update_fields=["status", "updated_at"])


def can_approve(shipment: Shipment) -> tuple[bool, str]:
    if shipment.is_locked:
        return False, "Shipment is already approved"
    errors = shipment.issues.filter(resolved=False, severity=ERROR).count()
    if errors:
        return False, f"Resolve {errors} error(s) first"
    return True, ""


def revalidate_related(doc: Document, skip_id: int | None = None) -> None:
    """A new invoice can make an older one a duplicate or change a vendor's cost history.
    Re-check other open shipments that contain invoices from the same vendor."""
    vk = vendor_key(doc.field("vendor_name"))
    if not vk:
        return
    active = Shipment.objects.filter(organization=doc.organization, status__in=[
        Shipment.Status.OPEN, Shipment.Status.NEEDS_REVIEW, Shipment.Status.READY]).exclude(pk=skip_id)
    for s in active:
        if any(vendor_key(d.field("vendor_name")) == vk for d in s.documents if d.posts_to_accounting):
            validate_shipment(s)
