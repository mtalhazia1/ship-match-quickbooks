"""Data for evidence highlighting on the review screens: where each value is printed, and which
values a validation issue is about. Read by static/js/evidence.js from a JSON script tag."""
from __future__ import annotations

from django.urls import reverse

from apps.documents.services.locate import FOUND, NOT_FOUND, PARTIAL, norm
from apps.shipments.templatetags.review_tags import label

# Issues that point at values on the page. missing_field has nothing to show; shipment-level
# issues (missing B/L, ...) have no document.
ISSUE_TARGETS = {
    "total_mismatch": (["total_amount", "line_items.amount"], "the total and the line amounts"),
    "duplicate_invoice": (["invoice_number"], "the invoice number"),
    "amount_outlier": (["total_amount"], "the total amount"),
}
CONTAINER_ISSUES = {"invalid_container", "container_not_on_bl"}
NO_TARGET = {"missing_field", "missing_bl", "missing_commercial_invoice", "fuzzy_match"}


def doc_payload(doc) -> dict:
    """{id, url, mode, fields: {name: {label, status, hits | items, human}}} for one document.

    mode: "pdfjs" when the document has words with positions (text layer or OCR word boxes),
    "scanned" when it is an image without them (the browser's own PDF viewer is used).
    """
    fields, statuses = {}, set()
    for f in doc.fields.all():
        if f.value in (None, "", []):
            continue
        loc = f.location or {}
        status = loc.get("status") or "unknown"
        statuses.add(status)
        entry = {"label": phrase(f.name), "status": status}
        if f.source == "human":
            entry["human"] = True
        if loc.get("items") is not None:
            entry["items"] = {str(it["key"]): _hit(it) for it in loc["items"] if it.get("boxes")}
        elif loc.get("boxes"):
            entry["hits"] = [_hit(loc)]
        fields[f.name] = entry
    return {"id": doc.pk, "url": reverse("review:document_file", args=[doc.pk]), "title": doc.original_filename,
            "pages": doc.page_count or 0, "mode": _mode(doc, statuses), "fields": fields}


def _hit(loc: dict) -> dict:
    out = {"p": loc["page"], "b": loc["boxes"]}
    if loc.get("amount"):
        out["a"] = loc["amount"]
    return out


def _mode(doc, statuses: set) -> str:
    source = doc.text_source or ""
    if source == "text_layer" or (source == "" and doc.status == "received"):
        return "pdfjs"
    if source == "textract" and statuses & {FOUND, PARTIAL, NOT_FOUND}:
        return "pdfjs"
    return "scanned"


def phrase(name: str) -> str:
    """A field label inside a sentence: 'total amount', but 'B/L number' and 'PO numbers' keep capitals."""
    text = label(name)
    first = text.split(" ", 1)[0]
    return text if sum(c.isupper() for c in first) > 1 else text[:1].lower() + text[1:]


def issue_targets(issue) -> tuple[list[str], str] | None:
    """(targets, description) for an issue tied to values on a document, or None.

    Targets: "field" (the whole field), "field=KEY" (one container or PO), "line_items.amount"
    (every line amount). The description finishes "Highlighted ... on page 2".
    """
    if not issue.document_id or issue.code in NO_TARGET:
        return None
    data = issue.data or {}
    evidence = data.get("evidence")  # targets a check chose itself: {"targets": [...], "label": "..."}
    if isinstance(evidence, dict) and evidence.get("targets"):
        return [str(t) for t in evidence["targets"]], str(evidence.get("label") or "the values")
    if issue.code in ISSUE_TARGETS:
        return ISSUE_TARGETS[issue.code]
    if issue.code in CONTAINER_ISSUES and data.get("key"):
        return [f"container_numbers={norm(data['key'])}"], f"container {data['key']}"
    names = data.get("fields") or ([data["field"]] if isinstance(data.get("field"), str) else [])
    names = [n for n in names if isinstance(n, str) and n]
    if not names:
        return None
    words = [phrase(n) for n in names]
    text = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
    return names, f"the {text}"
