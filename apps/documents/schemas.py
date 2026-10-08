"""Strict extraction schemas, one per document type.

The same Pydantic models are used to:
  * validate LLM output (bad output is rejected and retried),
  * describe the JSON schema sent to the LLM (tool / json_schema),
  * shape API responses.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator("container_numbers", "po_numbers", mode="before", check_fields=False)
    @classmethod
    def _listify(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [p.strip() for p in v.replace(";", ",").split(",") if p.strip()]
        return v

    @field_validator("currency", mode="before", check_fields=False)
    @classmethod
    def _upper_currency(cls, v):
        return v.strip().upper()[:3] if isinstance(v, str) and v.strip() else v


class LineItem(BaseModel):
    description: str
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    amount: Decimal


class GoodsLineItem(LineItem):
    """A product line on a commercial invoice. The optional fields feed landed cost (apps/landed)."""

    sku: Optional[str] = Field(default=None, description="Supplier's product code (SKU, item or part number) if printed")
    hs_code: Optional[str] = Field(default=None, description="Customs tariff (HS) code printed for the line")
    weight_kg: Optional[Decimal] = Field(default=None, description="Total weight of the line in kilograms, if printed")
    volume_cbm: Optional[Decimal] = Field(default=None, description="Total volume of the line in cubic metres, if printed")


class CommercialInvoice(_Base):
    """Supplier's invoice for the goods."""

    vendor_name: str = Field(description="Company that issued the invoice (the supplier)")
    invoice_number: str
    invoice_date: Optional[date] = None
    currency: Optional[str] = Field(default=None, description="ISO 4217 code, e.g. USD")
    po_numbers: list[str] = Field(default_factory=list, description="Buyer purchase order numbers")
    bl_number: Optional[str] = Field(default=None, description="Bill of lading number if printed")
    container_numbers: list[str] = Field(default_factory=list, description="ISO 6346 container numbers, e.g. MSCU1234565")
    line_items: list[GoodsLineItem] = Field(default_factory=list)
    total_amount: Decimal


class BillOfLading(_Base):
    """Carrier's bill of lading. Not payable; it anchors the shipment."""

    carrier_name: str
    bl_number: str
    issue_date: Optional[date] = None
    shipper: Optional[str] = None
    consignee: Optional[str] = None
    port_of_loading: Optional[str] = None
    port_of_discharge: Optional[str] = None
    vessel_voyage: Optional[str] = None
    container_numbers: list[str] = Field(default_factory=list)
    po_numbers: list[str] = Field(default_factory=list)


class FreightInvoice(_Base):
    """Forwarder / trucker / broker invoice for moving the shipment."""

    vendor_name: str
    invoice_number: str
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    currency: Optional[str] = None
    bl_number: Optional[str] = None
    container_numbers: list[str] = Field(default_factory=list)
    po_numbers: list[str] = Field(default_factory=list)
    line_items: list[LineItem] = Field(default_factory=list)
    total_amount: Decimal


class CreditNote(_Base):
    """A vendor's credit note (credit memo): reduces what is owed, usually against an earlier invoice."""

    vendor_name: str
    credit_note_number: str
    original_invoice_number: Optional[str] = Field(default=None, description="The invoice this credit note reduces")
    invoice_date: Optional[date] = None
    currency: Optional[str] = None
    total_amount: Decimal = Field(description="Amount credited, as a positive number")
    line_items: list[LineItem] = Field(default_factory=list)
    bl_number: Optional[str] = None
    container_numbers: list[str] = Field(default_factory=list)
    po_numbers: list[str] = Field(default_factory=list)


class EntryLine(BaseModel):
    """One line of a customs entry (CBP 7501 boxes 27-34). Every value may be missing on a hard-to-read form."""

    line_number: Optional[str] = None
    hts_code: Optional[str] = Field(default=None, description="Tariff number as printed, e.g. 8518.22.0000")
    description: Optional[str] = None
    entered_value: Optional[Decimal] = None
    duty_rate: Optional[str] = Field(default=None, description="Rate as printed: 4.9%, Free, or a specific rate")
    duty_amount: Optional[Decimal] = None


class CustomsEntry(_Base):
    """US entry summary (CBP Form 7501) or another country's customs import declaration. Not payable:
    the duty and fees it states are checked and reach landed cost (apps/customs)."""

    entry_number: str
    entry_type: Optional[str] = None
    entry_date: Optional[date] = None
    import_date: Optional[date] = None
    port_of_entry: Optional[str] = None
    importer_name: Optional[str] = None
    broker_name: Optional[str] = None
    bl_number: Optional[str] = None
    container_numbers: list[str] = Field(default_factory=list)
    country_of_origin: Optional[str] = None
    currency: Optional[str] = None
    invoice_currency: Optional[str] = None
    exchange_rate: Optional[Decimal] = None
    entry_lines: list[EntryLine] = Field(default_factory=list)
    total_entered_value: Optional[Decimal] = None
    total_duty: Optional[Decimal] = None
    merchandise_processing_fee: Optional[Decimal] = None
    harbor_maintenance_fee: Optional[Decimal] = None
    other_fees: Optional[Decimal] = None
    total_duty_and_fees: Optional[Decimal] = None


class ArrivalContainer(BaseModel):
    """Free time dates printed for one container on an arrival notice."""

    container_number: Optional[str] = None
    discharge_date: Optional[date] = None
    demurrage_last_free_day: Optional[date] = None
    detention_last_free_day: Optional[date] = None


class ArrivalNotice(_Base):
    """Carrier or forwarder arrival notice (or delivery order): when the containers arrive, how long they
    may stay at the terminal for free, and what must be paid before release. Not payable."""

    carrier_name: Optional[str] = None
    notice_date: Optional[date] = None
    bl_number: str
    vessel_voyage: Optional[str] = None
    port_of_discharge: Optional[str] = None
    terminal: Optional[str] = None
    estimated_arrival_date: Optional[date] = None
    actual_arrival_date: Optional[date] = None
    discharge_date: Optional[date] = None
    demurrage_free_days: Optional[int] = None
    detention_free_days: Optional[int] = None
    free_time_basis: Optional[str] = Field(default=None, description="How free days are counted, as printed")
    demurrage_last_free_day: Optional[date] = None
    detention_last_free_day: Optional[date] = None
    container_numbers: list[str] = Field(default_factory=list)
    container_dates: list[ArrivalContainer] = Field(default_factory=list)
    currency: Optional[str] = None
    line_items: list[LineItem] = Field(default_factory=list)
    total_amount: Optional[Decimal] = None


SCHEMAS: dict[str, type[_Base]] = {
    "commercial_invoice": CommercialInvoice,
    "bill_of_lading": BillOfLading,
    "freight_invoice": FreightInvoice,
    "credit_note": CreditNote,
    "customs_entry": CustomsEntry,
    "arrival_notice": ArrivalNotice,
}

# Fields every payable document must have before it can be posted.
REQUIRED_FIELDS = {
    "commercial_invoice": ["vendor_name", "invoice_number", "total_amount"],
    "freight_invoice": ["vendor_name", "invoice_number", "total_amount"],
    "bill_of_lading": ["bl_number"],
    "credit_note": ["vendor_name", "credit_note_number", "total_amount"],
    "customs_entry": ["entry_number", "entry_date"],
    "arrival_notice": ["bl_number"],
}

# Fields that hold a table (one dict per printed row): the row key that names the row and the row key with
# its amount (None: no amount). Shown as tables on the review screen, located row by row on the PDF,
# never edited as one text value.
TABLE_FIELDS: dict[str, tuple[str, str | None]] = {
    "line_items": ("description", "amount"),
    "entry_lines": ("hts_code", "duty_amount"),
    "container_dates": ("container_number", None),
}


def field_kind(name: str) -> str:
    """'number', 'date' or 'text' for a field name, from its type in the schemas (rows included)."""
    if not _KINDS:
        import typing

        def walk(model):
            for fname, f in model.model_fields.items():
                ann = f.annotation
                args = [a for a in typing.get_args(ann) if a is not type(None)]
                if typing.get_origin(ann) is list and args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
                    walk(args[0])
                    continue
                base = args[0] if typing.get_origin(ann) is not None and args else ann
                kind = "number" if base in (Decimal, int) else "date" if base is date else "text"
                _KINDS.setdefault(fname, kind)

        for schema in SCHEMAS.values():
            walk(schema)
    return _KINDS.get(name, "text")


_KINDS: dict[str, str] = {}


# --------------------------------------------------------------------------- LLM wire format

# Extra guidance per field, sent to the model with the schema.
FIELD_HINTS = {
    "vendor_name": "Company that issued this invoice and should be paid",
    "carrier_name": "Shipping line or carrier that issued the bill of lading",
    "invoice_number": "Invoice number exactly as printed",
    "bl_number": "Bill of lading (B/L, BOL, HBL or MBL) number exactly as printed",
    "invoice_date": "Date as YYYY-MM-DD",
    "issue_date": "Date as YYYY-MM-DD",
    "due_date": "Date as YYYY-MM-DD",
    "currency": "ISO 4217 code such as USD or EUR",
    "total_amount": "Grand total printed on the document, as a number",
    "container_numbers": "ISO 6346 container numbers (4 letters + 7 digits), no spaces or dashes",
    "po_numbers": "Buyer purchase order numbers",
    "credit_note_number": "Credit note (credit memo) number exactly as printed",
    "original_invoice_number": "Number of the earlier invoice this credit note reduces or cancels, exactly as printed",
    # customs entries (CBP 7501 box numbers in brackets) and arrival notices
    "entry_number": "Entry number exactly as printed, e.g. ABC-1234567-8 [box 1] or the declaration number",
    "entry_type": "Entry type code and name as printed, e.g. 01 ABI/A [box 2]",
    "entry_date": "Entry date as YYYY-MM-DD [box 7]",
    "import_date": "Import (arrival) date as YYYY-MM-DD [box 11]",
    "port_of_entry": "Port of entry code or name as printed [box 6 or 20]",
    "importer_name": "Importer of record name [box 26]",
    "broker_name": "Customs broker or filer that prepared the entry",
    "country_of_origin": "Country of origin as printed (code or name) [box 10]",
    "invoice_currency": "ISO 4217 code of the commercial invoice the values were converted from",
    "exchange_rate": "Exchange rate used to convert the invoice currency, as a number",
    "total_entered_value": "Total entered value [box 35], as a number",
    "total_duty": "Total duty [box 37], as a number",
    "merchandise_processing_fee": "Merchandise Processing Fee, class code 499, as a number",
    "harbor_maintenance_fee": "Harbor Maintenance Fee, class code 501, as a number",
    "other_fees": "Other fees and taxes not listed above, as a number",
    "total_duty_and_fees": "Total duty, taxes and fees [box 40], as a number",
    "entry_lines": "One entry per tariff line; a Chapter 99 line (e.g. 9903.88.15) is its own entry",
    "notice_date": "Date of the notice as YYYY-MM-DD",
    "terminal": "Terminal or pier where the containers are discharged",
    "estimated_arrival_date": "ETA as YYYY-MM-DD",
    "actual_arrival_date": "Actual arrival (ATA) as YYYY-MM-DD",
    "discharge_date": "Date the containers were (or will be) discharged, as YYYY-MM-DD",
    "demurrage_free_days": "Number of free days at the terminal before demurrage starts",
    "detention_free_days": "Number of free days to return the empty container before detention starts",
    "free_time_basis": "How free days are counted, as printed (calendar days, working days, excluding weekends ...)",
    "demurrage_last_free_day": "Last free day at the terminal (pick up by), as YYYY-MM-DD",
    "detention_last_free_day": "Last free day to return the empty container, as YYYY-MM-DD",
    "container_dates": "One entry per container with the dates printed for it",
}


def _wire_type(annotation) -> dict:
    """JSON schema for one field, in the subset structured outputs accept (no $ref, no pattern)."""
    import typing

    origin = typing.get_origin(annotation)
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if origin is typing.Union or str(origin) == "types.UnionType":
        return _wire_type(args[0])
    if origin is list:
        item = args[0]
        if isinstance(item, type) and issubclass(item, BaseModel):
            return {"type": "array", "items": _wire_object(item)}
        return {"type": "array", "items": {"type": "string"}}
    if annotation in (Decimal, int):
        return {"type": ["number", "null"]}
    return {"type": ["string", "null"]}


def _wire_object(model: type[BaseModel]) -> dict:
    props = {}
    for name, f in model.model_fields.items():
        schema = _wire_type(f.annotation)
        hint = FIELD_HINTS.get(name) or f.description
        if hint:
            schema["description"] = hint
        props[name] = schema
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def wire_schema(doc_type: str) -> dict:
    """Schema sent to the LLM. Every field is present but may be null, so the model never has to
    invent a value it cannot see; required fields are enforced later by validation rules."""
    return _wire_object(SCHEMAS[doc_type])


def lenient_model(doc_type: str) -> type[BaseModel]:
    """Same fields as the strict schema, all optional, for parsing LLM answers."""
    from pydantic import create_model

    cache = _LENIENT
    if doc_type not in cache:
        strict = SCHEMAS[doc_type]
        fields = {}
        for name, f in strict.model_fields.items():
            if name == "line_items":
                import typing

                item = typing.get_args(f.annotation)[0]  # LineItem, or GoodsLineItem with SKU/HS/weight/volume
                line = create_model(f"Lenient{item.__name__}",
                                    **{n: (Optional[_unwrap(i.annotation)], None) for n, i in item.model_fields.items()})
                fields[name] = (list[line], Field(default_factory=list))
            elif name in ("container_numbers", "po_numbers"):
                fields[name] = (list[str], Field(default_factory=list))
            else:
                fields[name] = (Optional[f.annotation], None)
        cache[doc_type] = create_model(f"Lenient{strict.__name__}", __base__=_Base, **fields)
    return cache[doc_type]


def _unwrap(annotation):
    """Optional[X] -> X, so a lenient field is Optional[X] once."""
    import typing

    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    return args[0] if typing.get_origin(annotation) is typing.Union and args else annotation


_LENIENT: dict[str, type[BaseModel]] = {}
