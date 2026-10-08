"""A guard for the invoice pipeline: a vendor statement that arrives by email or upload must never be
paid as if it were an invoice. Registered from CloseConfig.ready() with register_document_rule."""
from __future__ import annotations

import re

from apps.documents.models import Document
from apps.shipments.services.validation import ERROR, IssueSpec

# A statement's title is near the top: "STATEMENT OF ACCOUNT", "Vendor statement", "Account statement".
_TITLE = re.compile(r"\b(statement of account|account statement|statement of accounts|vendor statement|"
                    r"supplier statement|customer statement|statement)\b", re.I)
_INVOICE_TITLE = re.compile(r"\b(invoice|credit note|credit memo|bill of lading)\b", re.I)
_STATEMENT_WORDS = re.compile(r"\b(balance (brought )?forward|opening balance|closing balance|balance due|"
                              r"running balance|aged|ageing|aging|outstanding)\b", re.I)


def looks_like_statement(text: str) -> bool:
    """True when the text is titled as a statement and reads like one (balances, several documents)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()][:8]
    head = "\n".join(lines)
    title = _TITLE.search(head)
    if not title:
        return False
    # "Invoice" in the same title line ("Invoice statement") is too ambiguous to block a payable document.
    title_line = next((ln for ln in lines if _TITLE.search(ln)), "")
    if _INVOICE_TITLE.search(title_line):
        return False
    return bool(_STATEMENT_WORDS.search(text or "")) or "statement of account" in head.lower()


def check_statement_sent_as_invoice(doc: Document, data: dict):
    if not doc.posts_to_accounting or not looks_like_statement(doc.text):
        return
    yield IssueSpec(
        "looks_like_statement", ERROR,
        f"{doc.original_filename} looks like a vendor statement, not an invoice. A statement lists invoices "
        "that each arrive on their own, so paying it would pay them twice. Upload it under Month-end > Vendor "
        "statements and move it out of this shipment.",
        doc, {"key": "statement"})
