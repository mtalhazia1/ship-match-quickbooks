"""End-to-end processing of one document: text -> type -> fields -> shipment -> checks."""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import OperationalError, transaction

from apps.core.utils import audit
from apps.documents.models import Document, ExtractedField
from apps.learning.services.apply import learning_for

from . import llm, locate
from .classify import classify
from .errors import friendly, scrub
from .extract import extract
from .ocr import TextResult

log = logging.getLogger(__name__)


def process_document(doc_id: int, force_type: str | None = None) -> Document:
    """Read, classify, extract, match and check one document.

    force_type: a reviewer chose the document type; skip classification and reuse the text already read.
    """
    from apps.intake.services.readers import document_text
    from apps.intake.services.splitting import split_if_batch
    from apps.shipments.services.matching import lock_org, match_document
    from apps.shipments.services.validation import revalidate_related, validate_shipment

    doc = Document.objects.select_related("organization").get(pk=doc_id)
    if doc.status == Document.Status.ARCHIVE:
        return doc  # a ZIP: the files inside are documents of their own
    try:
        with doc.file.open("rb") as fh:
            data = fh.read()
        with llm.track_usage() as calls:
            if force_type and doc.text:
                tr = TextResult(doc.text, doc.page_count, False, doc.text_source or "text_layer")
            else:
                tr = document_text(doc, data)
            doc.text, doc.page_count, doc.text_source = tr.text, tr.page_count, tr.method
            if tr.needs_ocr:
                doc.status = Document.Status.NEEDS_OCR
                doc.llm_usage = llm.summarize(calls)
                doc.save(update_fields=["text", "page_count", "text_source", "status", "llm_usage", "updated_at"])
                audit(doc.organization, "document.needs_ocr", doc, pages=tr.page_count)
                return doc
            if not force_type and split_if_batch(doc, tr, data, calls):
                return doc  # several invoices in one PDF: each part is processed as its own document

            if force_type:
                doc.doc_type, doc.classification_confidence = force_type, 1.0
            else:
                doc.doc_type, doc.classification_confidence = classify(tr.text)
            learned = learning_for(doc, tr.text)  # what reviewers taught us about this vendor
            with learned.prompt_hint():
                fields, provider = extract(doc.doc_type, tr.text, pdf=data if _send_pdf(tr.method) else None)
            fields = learned.apply(fields, provider)
        doc.llm_usage = _usage(doc.llm_usage if force_type else None, llm.summarize(calls))
        with transaction.atomic():
            # Human corrections survive re-processing.
            human = set(doc.fields.filter(source=ExtractedField.Source.HUMAN).values_list("name", flat=True))
            doc.fields.exclude(name__in=human).delete()
            ExtractedField.objects.bulk_create([
                ExtractedField(document=doc, name=f.name, value=f.value, confidence=f.confidence,
                               grounded=f.grounded, source=f.source)
                for f in fields if f.name not in human
            ])
            learned.record(doc, skip=human)
            doc.extraction_provider = provider
            doc.status = Document.Status.EXTRACTED
            doc.error = ""
            doc.save()
        audit(doc.organization, "document.extracted", doc, doc_type=doc.doc_type, provider=provider,
              fields=len(fields), method=tr.method, cost_usd=doc.llm_usage.get("cost_usd", 0))
        locate.safe_locate(doc, data, ocr_words=tr.words)  # where each value is printed; never raises

        # Matching and validation for one organization run one at a time (row lock), so parallel
        # workers cannot create two shipments for one B/L or write conflicting issues.
        with transaction.atomic():
            lock_org(doc.organization_id)
            shipment = match_document(doc)
            if shipment:
                validate_shipment(shipment)
                revalidate_related(doc, skip_id=shipment.pk)
    except OperationalError:
        raise  # deadlock or lost connection: let Celery retry the whole document
    except Exception as e:  # keep the document visible in the queue with the error
        log.exception("Processing failed for document %s", doc_id)
        doc.status = Document.Status.ERROR
        doc.error = friendly(e)   # the details are in the log above, never on a page
        doc.save(update_fields=["status", "error", "updated_at"])
        audit(doc.organization, "document.error", doc, error=scrub(str(e))[:300])
    doc.refresh_from_db()
    return doc


def _send_pdf(text_method: str) -> bool:
    """Send the PDF itself to the LLM? Always for LLM_INPUT=pdf; for scans only with auto."""
    mode = (settings.LLM_INPUT or "auto").lower()
    return mode == "pdf" or (mode == "auto" and text_method not in ("text_layer", "spreadsheet"))


def _usage(previous: dict | None, current: dict) -> dict:
    """Add this run's AI usage to earlier runs of the same document (e.g. after a type change)."""
    if not previous or not previous.get("calls"):
        return current
    merged = {"calls": previous["calls"] + current["calls"]}
    for key in ("input_tokens", "output_tokens", "ms"):
        merged[key] = previous.get(key, 0) + current.get(key, 0)
    merged["cost_usd"] = round(previous.get("cost_usd", 0) + current.get("cost_usd", 0), 6)
    return merged
