"""Write the dispute email: a fixed template filled with the evidence, optionally reworded by AI.

The template is the source of truth. AI may only rephrase it; if the reworded text drops any amount,
invoice number, container or reference, the template is kept (`polish` raises PolishRejected).
"""
from __future__ import annotations

import json
import logging
import re

from apps.documents.services import llm

from ..evidence import money
from ..models import Dispute, DisputeSettings

log = logging.getLogger(__name__)

SUBJECT_MAX = 200
BODY_MAX = 8000

POLISH_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string", "description": "Email subject line"},
        "body": {"type": "string", "description": "Plain-text email body"},
    },
    "required": ["subject", "body"],
    "additionalProperties": False,
}

POLISH_SYSTEM = (
    "You edit emails that an accounts payable team sends to a vendor about a wrong invoice. "
    "Make the draft clear, polite and firm, in plain business English, and keep it short. "
    "Rules: keep every amount, currency, invoice number, date, container number, bill of lading number and "
    "reference exactly as written. Do not add facts, promises, deadlines, threats or legal language. "
    "Keep the request for a credit note or corrected invoice. Keep the numbered list of problems and the "
    "signature. Plain text only, no markdown."
)


class PolishRejected(RuntimeError):
    pass


def _greeting(dispute: Dispute) -> str:
    if dispute.contact_name:
        return f"Hello {dispute.contact_name.split()[0]},"
    return f"Hello {dispute.vendor_name} accounts team," if dispute.vendor_name else "Hello,"


def signature(org) -> str:
    sig = (DisputeSettings.for_org(org).signature or "").strip()
    return sig or f"Accounts payable\n{org.name}"


def compose(dispute: Dispute) -> tuple[str, str]:
    """Subject and body from the dispute's evidence. Same input, same text."""
    org = dispute.organization
    inv = dispute.invoice.data() if dispute.invoice_id else {}
    cur = dispute.currency
    number = dispute.invoice_number or str(inv.get("invoice_number") or "")
    items = list(dispute.items.all())
    amount = dispute.amount_disputed

    invoice_words = f"invoice {number}" if number else "your invoice"
    if inv.get("invoice_date"):
        invoice_words += f" dated {inv['invoice_date']}"
    if inv.get("total_amount") not in (None, ""):
        invoice_words += f" for {money(inv['total_amount'], cur)}"

    lines = [_greeting(dispute), ""]
    problems = "a problem" if len(items) == 1 else f"{len(items)} problems"
    lines.append(f"We checked {invoice_words} against our shipment records and found {problems}:")
    lines.append("")
    for n, item in enumerate(items, 1):
        sentence = f"{n}. {item.title}. {item.explanation}"
        if item.amount:
            sentence += f" Amount in question: {money(item.amount, item.currency or cur)}."
        lines += [sentence, ""]

    details = []
    shipment = dispute.shipment
    bl = (shipment.bl_number if shipment else "") or inv.get("bl_number") or ""
    containers = (shipment.container_numbers if shipment else None) or inv.get("container_numbers") or []
    if bl:
        details.append(f"Bill of lading: {bl}")
    if containers:
        details.append(f"Containers: {', '.join(containers)}")
    quotes = sorted({(i.data or {}).get("quote") for i in items if (i.data or {}).get("quote")})
    if quotes:
        details.append(f"Your quote: {', '.join(quotes)}")
    ours = dispute.reference + (f", shipment {dispute.shipment_reference}" if dispute.shipment_reference else "")
    details.append(f"Our reference: {ours}")
    lines += ["Shipment details:"] + details + [""]

    if amount and amount > 0:
        lines.append(f"Total in dispute: {money(amount, cur)}")
        lines.append("")
        lines.append(f"Please send a credit note for {money(amount, cur)}, or a corrected invoice, quoting "
                     f"{'invoice ' + number if number else 'this invoice'} and our reference {dispute.reference}. "
                     "We have put the disputed amount on hold until then.")
    else:
        lines.append(f"Please send a corrected invoice, or a credit note for any amount billed in error, quoting "
                     f"{'invoice ' + number if number else 'this invoice'} and our reference {dispute.reference}.")
    lines += ["", "A copy of the invoice is attached. Reply to this email if anything above is unclear.", "",
              "Thank you,", signature(org)]

    if amount and amount > 0:
        subject = f"Invoice {number or 'query'}: please send a credit note for {money(amount, cur)} ({dispute.reference})"
    else:
        subject = f"Invoice {number or 'query'}: please send a corrected invoice ({dispute.reference})"
    return subject[:SUBJECT_MAX], "\n".join(lines).strip() + "\n"


def must_keep(dispute: Dispute) -> list[str]:
    """Strings the AI version has to contain word for word."""
    keep = [dispute.reference]
    if dispute.invoice_number:
        keep.append(dispute.invoice_number)
    if dispute.amount_disputed:
        keep.append(f"{dispute.amount_disputed:,.2f}")
    for item in dispute.items.all():
        if item.amount:
            keep.append(f"{item.amount:,.2f}")
    shipment = dispute.shipment
    if shipment:
        if shipment.bl_number:
            keep.append(shipment.bl_number)
        keep.extend(shipment.container_numbers or [])
    return list(dict.fromkeys(k for k in keep if k))


def polish(dispute: Dispute, subject: str, body: str) -> tuple[str, str]:
    """Ask the AI to reword the draft. Raises PolishRejected or llm.LLMError; callers keep the template."""
    if not llm.is_enabled():
        raise PolishRejected("AI wording is not switched on for this server.")
    keep = must_keep(dispute)
    user = json.dumps({
        "draft_subject": subject, "draft_body": body, "keep_exactly": keep,
        "vendor": dispute.vendor_name,
    }, ensure_ascii=False)
    out = llm.structured_call(POLISH_SYSTEM, user, POLISH_SCHEMA, name="dispute_email", purpose="dispute_polish")
    new_subject = re.sub(r"\s+", " ", str(out.get("subject") or "")).strip()[:SUBJECT_MAX]
    new_body = str(out.get("body") or "").replace("\r\n", "\n").strip()
    if not new_subject or not new_body:
        raise PolishRejected("The AI returned an empty email.")
    if len(new_body) > BODY_MAX:
        raise PolishRejected("The AI version was too long.")
    missing = [k for k in keep if k not in new_body]
    if dispute.reference not in new_subject and dispute.reference not in new_body:
        missing.append(dispute.reference)
    if missing:
        raise PolishRejected(f"The AI version left out {', '.join(missing[:3])}.")
    return new_subject, new_body + "\n"
