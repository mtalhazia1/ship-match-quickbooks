"""Audit log wording for accounting connections and payment status. Added to apps.shipments.labels.ACTIONS
at start-up (AccountingConfig.ready), so feature branches don't all edit the same file."""

ACTIONS = {
    "xero.connected": "connected Xero ({company})",
    "xero.disconnected": "disconnected Xero",
    "xero.needs_reconnect": "found that Xero needs to be connected again",
    "xero.settings_updated": "changed the Xero settings (default account {account}, new bills {bill_status})",
    "xero.contact_created": "created the Xero contact {vendor}",
    "qbo.vendor_created": "created the QuickBooks vendor {vendor}",
    "bill.payment_updated": "{system} shows {what} {number} as {status}",
    "bill.voided_in_accounting": "found that {what} {number} is {status} in {system}",
    "accounting.payments_checked": "checked payments in {system}",
}

AUDIT_GROUPS = [("xero.", "Xero"), ("accounting.", "Payment checks")]
