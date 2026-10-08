"""Decide what kind of document a PDF is.

Keyword scoring first (free, explainable). If it is unsure and an LLM is configured,
ask the LLM to choose from the same fixed list of labels.
"""
from __future__ import annotations

import logging
import re

from . import llm

log = logging.getLogger(__name__)

LABELS = ["commercial_invoice", "bill_of_lading", "freight_invoice", "credit_note", "customs_entry", "arrival_notice",
          "other"]

TITLE_PATTERNS = {
    "commercial_invoice": [r"commercial invoice"],
    "bill_of_lading": [r"bill of lading"],
    "freight_invoice": [r"freight invoice", r"freight (?:&|and) logistics", r"freight charges"],
    "credit_note": [r"credit note", r"credit memo", r"credit advice"],
    "customs_entry": [r"entry summary", r"cbp form 7501", r"customs (?:import )?declaration", r"import declaration",
                      r"customs entry"],
    "arrival_notice": [r"arrival notice", r"notice of arrival", r"delivery order", r"cargo arrival"],
}
BODY_KEYWORDS = {
    "commercial_invoice": ["commercial invoice", "unit price", "qty", "buyer", "fob", "purchase order"],
    "bill_of_lading": ["shipped on board", "port of loading", "port of discharge", "notify party", "seal no",
                       "consignee", "vessel"],
    "freight_invoice": ["ocean freight", "terminal handling", "thc", "drayage", "customs clearance", "chassis",
                        "isf filing", "documentation fee", "amount due", "total due", "remit"],
    "credit_note": ["credit note", "credit memo", "original invoice", "credited", "total credit", "credit amount",
                    "amount credited", "against invoice", "refund"],
    # Words of the entry form itself; duty, MPF and HMF are left out because broker invoices bill them too.
    "customs_entry": ["entry summary", "entry type", "importer of record", "entered value", "htsus", "hts no",
                      "country of origin", "port code", "summary date", "surety", "customs and border protection",
                      "declaration no", "customs value"],
    "arrival_notice": ["arrival notice", "last free day", "free time", "free days", "estimated time of arrival",
                       "available for pick", "pick up no", "pickup no", "delivery order", "freight release",
                       "customs release", "before release"],
}
# Papers that are never titled "invoice": an invoice naming one near the top ("Invoice for customs entry ...",
# "Arrival notice and freight invoice") doesn't get its title bonus.
NEVER_INVOICE = ("customs_entry", "arrival_notice")


def classify(text: str) -> tuple[str, float]:
    low = (text or "").lower()
    head = "\n".join(low.splitlines()[:4])
    scores = {label: 0.0 for label in LABELS if label != "other"}
    titled = {label for label, pats in TITLE_PATTERNS.items() if any(re.search(p, head) for p in pats)}
    if "credit_note" in titled:  # 'Credit note for freight charges' is a credit note, not an invoice
        titled -= {"commercial_invoice", "freight_invoice"}
    for label in titled:
        scores[label] += 5
    for label, words in BODY_KEYWORDS.items():
        scores[label] += sum(1 for w in words if w in low)
    # A B/L is never titled 'invoice': an invoice that mentions 'Bill of Lading No.' near the top
    # must not get the B/L title bonus. A generic 'INVOICE' title is decided by body keywords.
    if "invoice" in head and scores["bill_of_lading"] >= 5:
        scores["bill_of_lading"] -= 5
    for label in NEVER_INVOICE:
        if "invoice" in head and label in titled:
            scores[label] -= 5
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top < 3:
        label, conf = "other", 0.4
    else:
        label, conf = best, round(min(0.99, 0.5 + (top - second) / (2 * top)), 2)
    if conf < 0.7 and llm.is_enabled():
        try:
            return classify_with_llm(text)
        except llm.LLMError as e:
            log.warning("LLM classification failed: %s", e)
    return label, conf


def classify_with_llm(text: str) -> tuple[str, float]:
    schema = {
        "type": "object",
        "properties": {"doc_type": {"type": "string", "enum": LABELS}},
        "required": ["doc_type"],
        "additionalProperties": False,
    }
    out = llm.structured_call(
        system="Classify shipping and accounting documents. Answer with one label only.",
        user=(
            "Labels: commercial_invoice (supplier invoice for goods), bill_of_lading (carrier B/L), "
            "freight_invoice (forwarder, trucking, customs or port charges), credit_note (credit note or credit "
            "memo that reduces an earlier invoice), customs_entry (customs entry summary such as CBP Form 7501, or "
            "an import declaration; a broker's invoice for duty is a freight_invoice), arrival_notice (carrier or "
            "forwarder arrival notice or delivery order with ETA and free time), other.\n\n"
            f"<document>\n{text[:6000]}\n</document>"
        ),
        schema=schema,
        name="classify_document",
        purpose="classify",
    )
    label = out.get("doc_type", "other")
    return (label if label in LABELS else "other"), 0.8
