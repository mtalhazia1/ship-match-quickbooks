"""Extraction entry point: rules or LLM, then grounding-based confidence.

Confidence does not come from the model's opinion of itself. A value gets high confidence
only if it can be found in the document text ("grounded"). An LLM value that does not
appear in the text is treated as a possible hallucination and sent to review.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from pydantic import ValidationError

from apps.documents.schemas import SCHEMAS, TABLE_FIELDS, field_kind, lenient_model, wire_schema
from apps.learning.context import current_hint

from . import llm
from .extract_rules import extract_rules
from .normalize import date_variants, norm_text, numbers_in_text, parse_date

log = logging.getLogger(__name__)

GROUNDED_LLM_CONF = 0.95
UNGROUNDED_CONF = 0.50

SYSTEM_PROMPT = (
    "You extract data from shipping and accounting documents for an accounts-payable team. "
    "Copy every value exactly as printed. If a value is not printed, return null (or an empty list); "
    "never guess. Never calculate, infer or correct values: if the printed total looks wrong, still return "
    "the printed total. Dates as YYYY-MM-DD. Amounts as plain numbers without thousands separators or "
    "currency symbols. Container numbers without spaces or dashes. Line items: one entry per charge or "
    "product row, with the row's amount."
)


@dataclass
class FieldOut:
    name: str
    value: object
    confidence: float
    grounded: bool
    source: str


def extract(doc_type: str, text: str, pdf: bytes | None = None) -> tuple[list[FieldOut], str]:
    """Return extracted fields and the provider name actually used.

    `pdf`: the original file, sent to the LLM alongside the text when the provider can read PDFs.
    """
    if doc_type not in SCHEMAS:
        return [], "none"
    if llm.is_enabled():
        try:
            return _from_llm(doc_type, text, pdf if llm.can_read_pdfs() else None), llm.provider()
        except (llm.LLMError, ValidationError) as e:
            log.warning("LLM extraction failed, falling back to rules: %s", e)
    return _from_rules(doc_type, text), "rules"


def _from_rules(doc_type: str, text: str) -> list[FieldOut]:
    from apps.intake.services.readers import rules_for  # spreadsheets and credit notes have their own readers

    r = rules_for(doc_type, text) or extract_rules(doc_type, text)
    ctx = _GroundingContext(text)
    out = []
    for name, value in r.values.items():
        g = ctx.grounded(name, value)
        out.append(FieldOut(name, value, r.confidence[name] if g else UNGROUNDED_CONF, g, "rules"))
    return out


def _from_llm(doc_type: str, text: str, pdf: bytes | None = None) -> list[FieldOut]:
    schema = wire_schema(doc_type)
    if pdf:
        user = (f"Document type: {doc_type}. The PDF is attached; its text layer is below for reference.\n\n"
                f"<document_text>\n{text[:30000]}\n</document_text>")
    else:
        user = f"Document type: {doc_type}\n\n<document>\n{text[:60000]}\n</document>"
    user += current_hint()  # vendor notes learned from reviewer corrections (apps.learning), size-capped
    raw = llm.structured_call(SYSTEM_PROMPT, user, schema, name=f"record_{doc_type}", pdf=pdf, purpose="extract")
    from apps.intake.services.credit import normalize_values  # credit amounts are stored as positive numbers

    data = normalize_values(doc_type, _parse_lenient(doc_type, raw))
    ctx = _GroundingContext(text)
    out = []
    for name, value in data.items():
        if value in ("", []):
            continue
        g = ctx.grounded(name, value)
        out.append(FieldOut(name, value, GROUNDED_LLM_CONF if g else UNGROUNDED_CONF, g, "llm"))
    return out


def _parse_lenient(doc_type: str, raw: dict) -> dict:
    """Validate the model's answer; a field that cannot be parsed (a date like 'next Tuesday') is dropped,
    so it shows as missing for a reviewer instead of failing the whole document."""
    model = lenient_model(doc_type)
    raw = dict(raw or {})
    for _ in range(len(raw) + 1):
        try:
            return model.model_validate(raw).model_dump(mode="json", exclude_none=True)
        except ValidationError as e:
            bad = {err["loc"][0] for err in e.errors() if err.get("loc")}
            if not bad & set(raw):
                raise
            log.info("Dropping unparseable fields from LLM output: %s", sorted(bad))
            for key in bad:
                raw.pop(key, None)
    return {}


class _GroundingContext:
    """Checks whether an extracted value actually appears in the document text."""

    def __init__(self, text: str):
        self.raw = text or ""
        self.compact = norm_text(self.raw)
        self.numbers = numbers_in_text(self.raw)

    def grounded(self, name: str, value) -> bool:
        if value in (None, "", []):
            return False
        if isinstance(value, list):
            return all(self.grounded(name, v) for v in value)
        if isinstance(value, dict):  # a table row (line item): what names it and its amount must both be present
            label_key, amount_key = TABLE_FIELDS.get(name, ("description", "amount"))
            return (self.grounded(label_key, value.get(label_key))
                    and (amount_key is None or self.grounded(amount_key, value.get(amount_key))))
        kind = field_kind(name)
        if name.endswith("_date") or kind == "date":
            d = parse_date(value)
            return bool(d) and any(norm_text(v) in self.compact for v in date_variants(d))
        if name in {"total_amount", "amount", "unit_price"} or kind == "number":
            try:
                amount = Decimal(str(value)).quantize(Decimal("0.01"))
            except Exception:
                return False
            return amount in self.numbers or -amount in self.numbers  # a credit printed as -500.00
        return norm_text(value) in self.compact
