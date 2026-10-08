"""Reviewer corrections: edit a field, then re-match and re-validate what it affects."""
from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction

from apps.core.utils import audit
from apps.documents.models import Document, ExtractedField
from apps.documents.schemas import SCHEMAS, TABLE_FIELDS, field_kind
from apps.documents.services.locate import safe_locate, to_decimal

LIST_FIELDS = {"container_numbers", "po_numbers"}
KEY_FIELDS = {"bl_number", "container_numbers", "po_numbers", "entry_number"}   # entry number: apps/customs matching
EDITABLE = {name for schema in SCHEMAS.values() for name in schema.model_fields} - set(TABLE_FIELDS)

# What a person may type. Money must fit the database columns (14 digits, 2 decimals) so a typo can never
# become a number that later breaks a page; dates are written year-month-day; text has a sane length.
MAX_MONEY = Decimal("999999999999.99")
MAX_RATE = Decimal("999999.999999")
MAX_LIST_ITEMS = 50
MAX_ITEM_LENGTH = 40
MAX_TEXT_LENGTH = {"bl_number": 40}
DEFAULT_TEXT_LENGTH = 200
_NUMBER_CHARS = re.compile(r"^[\s$€£¥]*\(?[-+]?[\d.,\s]+\)?$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class FieldValueError(ValueError):
    """A typed value that can't be stored for this field. The message is written for the person who typed it."""


def _pretty(name: str) -> str:
    from apps.shipments.templatetags.review_tags import label  # the names the review screen shows

    return str(label(name))


def _parse_number(name: str, raw: str) -> str:
    example = "1.0850" if name == "exchange_rate" else "1250.00"
    if not _NUMBER_CHARS.match(raw):
        raise FieldValueError(f"{_pretty(name)} must be a number, for example {example}.")
    value = to_decimal(raw)
    if value is None or not value.is_finite():
        raise FieldValueError(f"{_pretty(name)} must be a number, for example {example}.")
    if value < 0:
        raise FieldValueError(f"{_pretty(name)} can't be negative. Type it as a positive number.")
    limit = MAX_RATE if name == "exchange_rate" else MAX_MONEY
    if value > limit:
        raise FieldValueError(f"{_pretty(name)} is too large ({value:,}). Check the number against the document.")
    places = Decimal("0.000001") if name == "exchange_rate" else Decimal("0.01")
    try:
        return str(value.quantize(places, rounding=ROUND_HALF_UP))
    except InvalidOperation:
        raise FieldValueError(f"{_pretty(name)} must be a number, for example {example}.")


def _parse_date(name: str, raw: str) -> str:
    try:
        value = date.fromisoformat(raw)
    except ValueError:
        raise FieldValueError(f"{_pretty(name)} must be a date written year-month-day, for example 2026-01-08.")
    if not 1990 <= value.year <= 2100:
        raise FieldValueError(f"{_pretty(name)} {raw} looks wrong. Check the year on the document.")
    return value.isoformat()


def _parse_text(name: str, raw: str) -> str:
    text = _CONTROL.sub("", raw).strip()
    if name == "currency":
        if not re.fullmatch(r"[A-Za-z]{3}", text):
            raise FieldValueError("Currency must be a three-letter code such as USD.")
        return text.upper()
    limit = MAX_TEXT_LENGTH.get(name, DEFAULT_TEXT_LENGTH)
    if len(text) > limit:
        raise FieldValueError(f"{_pretty(name)} can be at most {limit} characters (you typed {len(text)}).")
    return text


def parse_input(name: str, raw: str):
    """The value to store for what a person typed, or FieldValueError saying what is wrong with it."""
    raw = (raw or "").strip()
    if name in LIST_FIELDS:
        items = [p.strip().upper().replace(" ", "") for p in raw.replace(";", ",").split(",") if p.strip()]
        if len(items) > MAX_LIST_ITEMS:
            raise FieldValueError(f"{_pretty(name)} can hold at most {MAX_LIST_ITEMS} entries.")
        if any(len(i) > MAX_ITEM_LENGTH for i in items):
            raise FieldValueError(f"Each entry in {_pretty(name).lower()} can be at most {MAX_ITEM_LENGTH} characters.")
        return items
    if not raw:
        return None
    kind = field_kind(name)
    if kind == "number":
        return _parse_number(name, raw)
    if kind == "date":
        return _parse_date(name, raw)
    return _parse_text(name, raw)


@transaction.atomic
def correct_field(doc: Document, name: str, raw_value: str, user) -> ExtractedField | None:
    """Save a reviewer's value. Returns None when the value did not change (nothing is recorded)."""
    if name not in EDITABLE:
        raise ValueError(f"{name} cannot be edited here")
    value = parse_input(name, raw_value)
    field = ExtractedField.objects.filter(document=doc, name=name).first()
    if field is not None and field.value == value:
        return None
    if field is None and value in (None, []):
        return None
    field = field or ExtractedField(document=doc, name=name)
    old = field.value
    field.value, field.confidence, field.source = value, 1.0, ExtractedField.Source.HUMAN
    field.save()
    audit(doc.organization, "field.corrected", doc, actor=user, field=name, old=old, new=value)
    safe_locate(doc)  # find the new value on the page; a typed value that isn't printed gets no box
    field.refresh_from_db(fields=["location", "page"])
    return field


def after_correction(doc: Document, name: str) -> None:
    from apps.shipments.services.matching import match_document, refresh_keys
    from apps.shipments.services.validation import validate_shipment

    old_shipment = doc.match.shipment if hasattr(doc, "match") else None
    if name in KEY_FIELDS:
        new_shipment = match_document(doc)
        if old_shipment and (not new_shipment or old_shipment.pk != new_shipment.pk):
            if old_shipment.links.exists():
                refresh_keys(old_shipment)
                validate_shipment(old_shipment)
            else:
                old_shipment.delete()
    doc.refresh_from_db()
    if hasattr(doc, "match"):
        validate_shipment(doc.match.shipment)
