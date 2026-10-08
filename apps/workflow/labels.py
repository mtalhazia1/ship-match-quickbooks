"""Audit log wording for team workflow. Added to apps.shipments.labels.ACTIONS at start-up."""

ACTIONS = {
    "shipment.assigned": "assigned the shipment to {assignee_name}",
    "shipment.unassigned": "removed {previous_name} from the shipment",
    "shipment.bulk_skipped": "was not able to {bulk_verb} the shipment in a bulk action: {reasons}",
    "comment.created": "commented",
    "comment.edited": "edited a comment",
    "comment.deleted": "deleted a comment",
    "assignment_rules.updated": "changed the assignment rules to {mode}",
    "assignment_rules.vendor_set": "set shipments from {vendor} to go to {assignee}",
    "assignment_rules.vendor_removed": "removed the assignment rule for {vendor}",
    "org.created": "created the organization {name}",
}

AUDIT_GROUPS = [("comment.", "Comments"), ("assignment_rules.", "Assignment rules"), ("org.", "Organizations")]
