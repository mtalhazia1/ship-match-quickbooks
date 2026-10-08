"""Turn a PDF into text.

1. Use the PDF's own text layer (free, exact) when it has one.
2. Otherwise OCR it, depending on OCR_PROVIDER:
   - anthropic: Claude reads the scanned pages and transcribes them (no AWS account needed);
   - textract: render each page and OCR it with AWS Textract, rebuilding lines from word
     positions so 'Label: value' pairs stay on one line;
   - auto (default): anthropic when Claude is the extraction provider, otherwise none.
3. If no OCR is available, mark the document as needing OCR (it goes to the review queue).
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import pdfplumber
from django.conf import settings

log = logging.getLogger(__name__)
MIN_CHARS_PER_PAGE = 40


@dataclass
class TextResult:
    text: str
    page_count: int
    needs_ocr: bool
    method: str  # text_layer | textract | anthropic | none
    words: list | None = None  # OCR word boxes [[page, x0, top, x1, bottom, text], ...] (Textract only)


def read_text(pdf_bytes: bytes) -> TextResult:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
    n = len(pages)
    text = "\n\f\n".join(pages).strip()
    if n and len(text) / n >= MIN_CHARS_PER_PAGE:
        return TextResult(text, n, False, "text_layer")
    provider = ocr_provider()
    if provider == "textract":
        ocr_text, words = textract_read(pdf_bytes)
        return TextResult(ocr_text, n, False, "textract", words)
    if provider == "anthropic":
        from .llm import LLMError, transcribe_pdf

        try:
            ocr = transcribe_pdf(pdf_bytes)
        except LLMError as e:
            log.warning("Claude OCR failed: %s", e)
            return TextResult(text, n, True, "none")
        if len(ocr.strip()) >= MIN_CHARS_PER_PAGE:
            return TextResult(ocr, n, False, "anthropic")
    return TextResult(text, n, True, "none")


def ocr_provider() -> str:
    value = (settings.OCR_PROVIDER or "auto").lower()
    if value == "auto":
        return "anthropic" if settings.EXTRACTION_PROVIDER == "anthropic" and settings.ANTHROPIC_API_KEY else "text"
    return value


def textract_text(pdf_bytes: bytes) -> str:
    return textract_read(pdf_bytes)[0]


def textract_read(pdf_bytes: bytes) -> tuple[str, list]:
    """OCR every page with Textract. Returns the text and the word boxes (for evidence highlighting)."""
    import boto3

    client = boto3.client("textract", region_name=settings.AWS_REGION)
    page_texts, words = [], []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            img = page.to_image(resolution=200).original
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            resp = client.detect_document_text(Document={"Bytes": buf.getvalue()})
            page_texts.append(_lines_from_blocks(resp.get("Blocks", [])))
            words += _words_from_blocks(resp.get("Blocks", []), number)
    return "\n\f\n".join(page_texts), words


def _words_from_blocks(blocks: list[dict], page: int) -> list[list]:
    """Textract WORD blocks as [page, x0, top, x1, bottom, text], coordinates 0..1 of the page image."""
    out = []
    for b in blocks:
        if b.get("BlockType") != "WORD" or not b.get("Text"):
            continue
        box = (b.get("Geometry") or {}).get("BoundingBox") or {}
        try:
            x0, top = float(box["Left"]), float(box["Top"])
            x1, bottom = x0 + float(box["Width"]), top + float(box["Height"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append([page, round(x0, 4), round(top, 4), round(x1, 4), round(bottom, 4), b["Text"]])
    return out


def _lines_from_blocks(blocks: list[dict], tolerance: float = 0.006) -> str:
    """Group Textract LINE blocks that sit on the same visual row, left to right."""
    lines = []
    for b in blocks:
        if b.get("BlockType") != "LINE":
            continue
        box = b["Geometry"]["BoundingBox"]
        lines.append((box["Top"] + box["Height"] / 2, box["Left"], b["Text"]))
    lines.sort()
    rows: list[list[tuple]] = []
    for item in lines:
        if rows and abs(rows[-1][0][0] - item[0]) <= tolerance:
            rows[-1].append(item)
        else:
            rows.append([item])
    return "\n".join(" ".join(t for _, _, t in sorted(r, key=lambda x: x[1])) for r in rows)
