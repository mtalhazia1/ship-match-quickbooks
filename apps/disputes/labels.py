"""Audit log wording for dispute actions. Added to apps.shipments.labels.ACTIONS at start-up."""

ACTIONS = {
    "dispute.drafted": "drafted dispute {reference} for {vendor}",
    "dispute.updated": "edited the draft of dispute {reference}",
    "dispute.discarded": "discarded the draft dispute {reference}",
    "dispute.sent": "sent dispute {reference} to {vendor} for {amount}",
    "dispute.send_failed": "could not send dispute {reference}",
    "dispute.reply_logged": "logged a reply from {vendor} on {reference}",
    "dispute.note_added": "added a note to dispute {reference}",
    "dispute.follow_up_changed": "set the follow-up date of {reference} to {date}",
    "dispute.credit_matched": "matched credit note {credit_note} ({amount}) to {reference}; waiting for an approver",
    "dispute.credit_received": "recorded {recovered} credited by {vendor} on {reference}",
    "dispute.resolved": "resolved dispute {reference} ({recovered} recovered)",
    "dispute.closed": "closed dispute {reference} without recovery",
    "dispute.hold_released": "let {shipment} be approved without waiting for {reference}",
    "dispute.overdue": "flagged {reference} as past its follow-up date",
    "dispute_settings.updated": "changed the dispute email settings",
}

AUDIT_GROUPS = [("dispute", "Disputes")]
