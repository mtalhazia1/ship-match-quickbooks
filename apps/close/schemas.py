"""The vendor statement schema: what is read from a statement, by the AI reader or the rules.

The same shape as the document schemas in apps/documents/schemas.py: a strict Pydantic model to validate
answers, and a JSON schema for structured output with `additionalProperties: false`, every field present
but nullable, and no `$ref` or `pattern`.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

LINE_TYPES = ["invoice", "credit", "payment", "opening_balance"]


class StatementLineSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["invoice", "credit", "payment", "opening_balance"] = "invoice"
    invoice_number: Optional[str] = Field(default=None, description="Invoice, credit note or payment number")
    date: Optional[dt.date] = None
    reference: Optional[str] = Field(default=None, description="Reference printed on the line: B/L, PO, shipment")
    amount: Decimal = Field(description="Amount of the line as a positive number; the type says which way it goes")
    balance: Optional[Decimal] = Field(default=None, description="Running balance after this line, if printed")


class StatementSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    vendor_name: Optional[str] = None
    statement_date: Optional[dt.date] = None
    currency: Optional[str] = None
    opening_balance: Optional[Decimal] = None
    closing_balance: Optional[Decimal] = None
    lines: list[StatementLineSchema] = Field(default_factory=list)

    @field_validator("currency", mode="before")
    @classmethod
    def _upper(cls, v):
        return v.strip().upper()[:3] if isinstance(v, str) and v.strip() else None


HINTS = {
    "vendor_name": "Company that issued the statement (the vendor we owe money to), not the customer it is sent to",
    "statement_date": "Date of the statement as YYYY-MM-DD (statement date, 'as of' or 'as at' date)",
    "currency": "ISO 4217 code such as USD or EUR",
    "opening_balance": "Balance brought forward at the start of the statement, if printed (negative if in our favour)",
    "closing_balance": "Balance due at the statement date as printed (total due, closing balance)",
    "type": "invoice, credit (credit note or credit memo), payment (payment received from us) or opening_balance",
    "invoice_number": "Invoice, credit note or payment number exactly as printed",
    "date": "Date of the line as YYYY-MM-DD",
    "reference": "Other reference printed on the line: B/L number, PO, shipment or description",
    "amount": "Amount of the line as a positive number",
    "balance": "Running balance printed after this line, or null",
}


def _nullable(kind: str, name: str) -> dict:
    return {"type": [kind, "null"], "description": HINTS[name]}


def wire_schema() -> dict:
    line = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": LINE_TYPES, "description": HINTS["type"]},
            "invoice_number": _nullable("string", "invoice_number"),
            "date": _nullable("string", "date"),
            "reference": _nullable("string", "reference"),
            "amount": {"type": "number", "description": HINTS["amount"]},
            "balance": _nullable("number", "balance"),
        },
        "required": ["type", "invoice_number", "date", "reference", "amount", "balance"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "vendor_name": _nullable("string", "vendor_name"),
            "statement_date": _nullable("string", "statement_date"),
            "currency": _nullable("string", "currency"),
            "opening_balance": _nullable("number", "opening_balance"),
            "closing_balance": _nullable("number", "closing_balance"),
            "lines": {"type": "array", "items": line},
        },
        "required": ["vendor_name", "statement_date", "currency", "opening_balance", "closing_balance", "lines"],
        "additionalProperties": False,
    }
