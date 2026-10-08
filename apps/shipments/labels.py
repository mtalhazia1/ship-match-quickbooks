"""Human-readable names and guidance for validation issues and audit actions."""

ISSUES = {
    "missing_field": ("Missing required field", "Type the missing value from the PDF."),
    "total_mismatch": ("Total doesn't match line items", "Check the printed total against the lines. Ask the vendor for a corrected invoice if it is wrong."),
    "invalid_container": ("Container number has a typo", "The check digit fails. Compare with the bill of lading and correct it."),
    "container_not_on_bl": ("Container not on the bill of lading", "This invoice may belong to another shipment, or the vendor billed the wrong container."),
    "duplicate_invoice": ("Possible duplicate invoice", "If this is a resend of the same invoice, move it out or override with a note. Never post both."),
    "amount_outlier": ("Unusual amount for this vendor", "Cost per container is far from this vendor's normal range. Confirm before approving."),
    "missing_bl": ("Bill of lading not received", "Wait for the B/L, or accept if this shipment has none."),
    "missing_commercial_invoice": ("Commercial invoice not received", "Wait for the supplier invoice, or accept if it is not needed."),
    "low_confidence": ("Please confirm extracted values", "Some values were read with low confidence. Check them against the PDF."),
    "fuzzy_match": ("Matched by a near-identical reference", "Confirm this document belongs to this shipment, or move it."),
    # credit notes
    "credit_no_original": ("Credit note doesn't name an invoice", "Type the number of the invoice it reduces, from the PDF, so it can be checked against that invoice."),
    "credit_original_not_found": ("Credited invoice not received", "Check the original invoice number against the PDF. If the invoice hasn't arrived yet, wait for it or accept this warning."),
    "credit_original_elsewhere": ("Credited invoice is in another shipment", "Move the credit note to the shipment of the invoice it reduces, or accept if it belongs here."),
    "credit_exceeds_invoice": ("Credit is larger than the invoice", "Check the credit amount with the vendor before approving."),
    "duplicate_credit_note": ("Possible duplicate credit note", "The same credit was received before. Don't claim it twice: move it out or override with a note."),
    # Rates (apps/rates/checks.py)
    "over_quote": ("Charged more than the quote", "Ask the vendor for an invoice at the quoted rate, or reject the shipment. Override only if the new rate was agreed in writing, and update the quote in Rates."),
    "unapproved_accessorial": ("Extra charge not approved", "This charge isn't in the quote or the vendor's approved extra charges, or it's above the agreed cap. Ask the vendor for proof (gate times, terminal receipts) before accepting it."),
    "accessorial_unchecked": ("Extra charge couldn't be checked", "The charge is approved, but the invoice line doesn't show the days or hours needed to check its cap. Compare it with the vendor's supporting documents."),
    "no_quote": ("No matching quote", "The vendor has quotes on file, but none fits this lane, date and equipment. Add or extend the quote in Rates, or accept if this was a spot rate."),
    "quote_currency": ("Quote in a different currency", "Add an exchange rate in Settings so the invoice can be compared with the quote."),
    # Shared invoices (apps/landed)
    "shared_invoice_split": ("Shared invoice: check the split", "This invoice covers several shipments. Check each shipment's share under Shared invoices on this page, then confirm the split or change it."),
    "shared_split_mismatch": ("Shared invoice split doesn't add up", "The shares must add up to the invoice total exactly. Change the split under Shared invoices, or go back to the automatic split."),
    "shared_invoice_locked_ref": ("Invoice names an approved shipment", "Reopen that shipment if it should carry part of this invoice, or accept this warning to keep the whole invoice here."),
    # Customs entries and arrival notices (apps/customs/services/checks.py)
    "duty_line_mismatch": ("Duty on a line doesn't match its rate", "Rate times entered value gives a different duty. Ask the customs broker to check the line and file a correction (post summary correction) if the duty was overpaid."),
    "duty_total_mismatch": ("Total duty doesn't match the lines", "The duty of the lines doesn't add up to the total duty. Ask the broker which figure was paid."),
    "duty_fees_total_mismatch": ("Entry total doesn't match duty plus fees", "Check the total against the duty, MPF, HMF and other fees on the entry."),
    "entered_value_total_mismatch": ("Entered values don't add up", "The lines' entered values don't add up to the total entered value. Check them against the PDF."),
    "mpf_incorrect": ("Merchandise processing fee is wrong", "MPF is 0.3464% of the entered value, kept between the minimum and maximum in force on the entry date (they change every 1 October). Ask the broker to correct it."),
    "hmf_incorrect": ("Harbor maintenance fee is wrong", "HMF is 0.125% of the entered value for cargo unloaded at a US port. Ask the broker to correct it."),
    "hts_code_format": ("Tariff number looks wrong", "US tariff numbers have 10 digits (8 for a Chapter 99 line). Compare with the PDF and correct it, or ask the broker."),
    "hts_description_doubt": ("Tariff number may not fit the goods", "The AI check doubts this classification. Ask the broker to confirm it; a wrong number can mean the wrong duty rate."),
    "duplicate_customs_entry": ("Possible duplicate customs entry", "The same entry with the same total was received before. Don't pay the duty twice: move it out or override with a note."),
    "customs_entry_changed": ("Customs entry received again with other amounts", "Probably a corrected entry. Check which version the broker filed and paid, and keep only that one in the shipment."),
    "entered_value_low": ("Entered value below the commercial invoice", "Declaring less than the price paid can lead to penalties. Ask the broker why (for example freight deducted from a CIF price) and correct the entry if needed."),
    "entered_value_high": ("Entered value above the commercial invoice", "Duty and fees were paid on more than the invoice value. Ask the broker to check the value and the exchange rate, and file a correction to get the difference back."),
    "origin_mismatch": ("Country of origin differs from the invoice", "The duty rate, Section 301 duties and trade preferences depend on origin. Confirm the origin with the supplier and the broker."),
    "free_time_dates": ("Free time dates don't fit", "The last free day is before the discharge date. Check the arrival notice."),
    # Month-end close (apps/close/rules.py)
    "looks_like_statement": ("Looks like a vendor statement", "A statement lists invoices that arrive on their own. Don't pay it: upload it under Month-end > Vendor statements to reconcile it, and move it out of this shipment."),
}

ACTIONS = {
    "document.received": "received {filename}",
    "document.extracted": "read the document ({provider})",
    "document.matched": "matched a document to {shipment} by {method}",
    "document.moved": "moved a document to {to}",
    "document.needs_ocr": "flagged a scanned document for OCR",
    "document.error": "could not process a document",
    "document.duplicate_file": "received an identical copy of a file ({filename})",
    "field.corrected": "changed {field} from {old} to {new}",
    "document.type_changed": "changed the document type from {old} to {new}",
    "issue.resolved": "accepted “{title}”",
    "shipment.validated": "checked the shipment",
    "shipment.approved": "approved the shipment",
    "shipment.rejected": "rejected the shipment",
    "shipment.reopened": "reopened the shipment",
    "shipment.merged": "merged {merged_from} into this shipment",
    "shipment.post_requested": "started posting bills to {system}",
    "bill.posted": "posted bill {qbo_bill_id} to {system}",
    "bill.failed": "could not post a bill to {system}",
    "vendor_mapping.updated": "set the expense account for {vendor} to {name}",
    "auth.login": "signed in",
    "auth.logout": "signed out",
    "auth.login_failed": "failed to sign in (wrong username or password)",
    "auth.locked_out": "was blocked from signing in after too many failed attempts",
    "auth.mfa_failed": "entered a wrong two-factor code",
    "auth.mfa_enabled": "turned on two-factor authentication",
    "auth.mfa_disabled": "turned off two-factor authentication",
    "auth.recovery_code_used": "signed in with a recovery code ({remaining} left)",
    "auth.recovery_codes_created": "created new recovery codes",
    "auth.password_changed": "changed their password",
    "auth.password_set": "set a new password from an emailed link",
    "team.invited": "invited {email} as {role}",
    "team.updated": "changed {user} to {role} with approval limit {limit}",
    "team.removed": "removed {user} from the organization",
    "team.mfa_reset": "reset two-factor authentication for {user}",
    "team.password_link": "created a password link for {user}",
    "team.platform_added": "added {user} as {role} through the platform admin",
    "team.platform_updated": "changed {user} to {role} through the platform admin",
    "team.platform_removed": "removed {user} from the organization through the platform admin",
    "settings.updated": "changed organization settings: {fields}",
    "api_key.created": "created API key {name} ({role})",
    "api_key.revoked": "revoked API key {name}",
    "api.document_uploaded": "uploaded a document through the API",
    "audit.exported": "exported the audit log",
    "qbo.connected": "connected QuickBooks (company {realm_id})",
    "qbo.disconnected": "disconnected QuickBooks",
    "qbo.default_account": "set the default QuickBooks expense account",
    # intake: archives, split files, credit notes
    "document.archive_unpacked": "unpacked {filename}: {added} added, {skipped} not added",
    "document.split": "split {filename} into {parts} documents",
    "document.unsplit": "kept {filename} as one document and removed its {parts} parts",
    "vendor_credit.posted": "posted vendor credit {qbo_bill_id} to {system}",
    "vendor_credit.failed": "could not post a vendor credit to {system}",
    "quote.created": "added {name} for {vendor} ({lane})",
    "quote.updated": "changed {name} for {vendor} ({lane})",
    "quote.archived": "archived {name} for {vendor} ({lane})",
    "quote.restored": "restored {name} for {vendor} ({lane})",
    "quote.deleted": "deleted {name} for {vendor} ({lane})",
    "quote.imported": "imported quotes from {file}: {created} new, {updated} updated",
    "quote.exported": "exported {quotes} quotes",
    "accessorial.created": "approved {charge} for {vendor}: {terms}",
    "accessorial.updated": "changed the approved {charge} for {vendor}: {terms}",
    "accessorial.deleted": "removed the approved {charge} for {vendor}",
    "charge_name.updated": "set the charge name “{name}” to mean {new}",
    "charge_name.deleted": "removed the charge name “{name}”",
    "rate_settings.updated": "changed the rate checking rules",
    "rates.rechecked": "checked {shipments} open shipments against the current rates",
    "savings.exported": "exported savings for {period}",
}

SETTING_NAMES = {
    "require_mfa": "two-factor requirement", "maker_checker": "maker-checker rule", "fx_rates": "exchange rates",
    "review_threshold": "review threshold", "home_currency": "home currency", "name": "name", "timezone": "time zone",
}


def issue_title(code: str) -> str:
    return ISSUES.get(code, (code.replace("_", " ").capitalize(), ""))[0]


def issue_guidance(code: str) -> str:
    return ISSUES.get(code, ("", ""))[1]


class _Default(dict):
    def __missing__(self, key):
        return "—"


def describe_action(action: str, data: dict) -> str:
    template = ACTIONS.get(action)
    if not template:
        return action.replace(".", " ").replace("_", " ")
    values = _Default({k: _short(v) for k, v in (data or {}).items()})
    values.setdefault("system", "QuickBooks")   # rows written before Xero support don't name the system
    if action == "field.corrected" and (data or {}).get("field"):
        values["field"] = str(data["field"]).replace("_", " ").capitalize().replace("Bl ", "B/L ").replace("Po ", "PO ")
    if action == "issue.resolved":
        values["title"] = issue_title(data.get("code", ""))
        if data.get("severity") == "error":
            template = "overrode the error “{title}”"
    if action == "team.updated" and (data or {}).get("limit") in (None, ""):
        values["limit"] = "none"
    if action == "settings.updated":
        changed = list(((data or {}).get("changes") or {}).keys())
        values["fields"] = ", ".join(SETTING_NAMES.get(k, k) for k in changed) or "no changes"
    if action == "document.matched":
        values["method"] = {
            "exact_bl": "B/L number", "exact_container": "container number", "exact_po": "PO number",
            "fuzzy": "a near-identical reference", "new_shipment": "starting a new shipment",
            "manual": "a reviewer", "original_invoice": "the invoice it credits",
            "entry_number": "the customs entry number",
        }.get(data.get("method", ""), data.get("method", ""))
    return template.format_map(values)


def _short(value) -> str:
    if value in (None, "", []):
        return "empty"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."
