"""Hooks the documents pipeline calls for non-PDF inputs and new document types.

  * `document_text`: the text of a stored document. Spreadsheets are read from the original file (the text
    form keeps rows and columns); parts of a split scanned PDF reuse the OCR text of their pages; every
    other document goes through the normal PDF text / OCR path.
  * `rules_for`: the rule-based reader for spreadsheets and credit notes, or None for the default rules.
"""
from __future__ import annotations

import io

from apps.documents.services.ocr import TextResult, read_text


def document_text(doc, pdf_bytes: bytes) -> TextResult:
    if doc.source_format == "spreadsheet" and doc.original_file:
        from .sheets import text_for

        with doc.original_file.open("rb") as fh:
            content = fh.read()
        text = text_for(doc.original_filename, content, (doc.intake or {}).get("subtype", "xlsx"))
        return TextResult(text, page_count(pdf_bytes), False, "spreadsheet")
    part = (doc.intake or {}).get("part") or {}
    if part.get("text_source") in ("anthropic", "textract") and doc.text:
        return TextResult(doc.text, part.get("page_count") or doc.page_count, False, part["text_source"])
    return read_text(pdf_bytes)


def page_count(pdf_bytes: bytes) -> int:
    from pypdf import PdfReader

    try:
        return len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception:
        return 0


def rules_for(doc_type: str, text: str):
    from .sheets import is_sheet_text, sheet_rules

    if is_sheet_text(text):
        return sheet_rules(doc_type, text)
    if doc_type == "credit_note":
        from .credit import credit_note_rules

        return credit_note_rules(text)
    return None
