"""Audit log wording for month-end close actions. Added to apps.shipments.labels.ACTIONS at start-up."""

ACTIONS = {
    "close.period_locked": "locked month-end accruals for {period} as version {version} ({total})",
    "close.accruals_exported": "exported month-end accruals for {period} ({format})",
    "close.accrual_adjusted": "set the {group} accrual for {shipment}: {what}",
    "close.accrual_adjustment_removed": "removed the {group} accrual adjustment for {shipment}",
    "close.settings_updated": "changed the month-end settings",
    "close.statement_uploaded": "uploaded a statement from {vendor} ({filename})",
    "close.statement_updated": "corrected the details of the statement from {vendor}",
    "close.statement_rematched": "matched the statement from {vendor} again",
    "close.statement_deleted": "deleted the statement from {vendor} ({filename})",
    "close.statement_exported": "exported the reconciliation of the statement from {vendor}",
    "close.item_resolved": "marked “{item}” resolved on the statement from {vendor}",
    "close.item_reopened": "reopened “{item}” on the statement from {vendor}",
    "close.payment_recorded": "recorded a payment of {amount} to {vendor}",
    "close.payment_deleted": "deleted a payment of {amount} to {vendor}",
    "close.payments_synced": "read payments from QuickBooks: {created} new, {updated} updated",
}
