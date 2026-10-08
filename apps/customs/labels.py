"""Audit log wording for customs and free time. Added to apps.shipments.labels.ACTIONS at start-up."""

ACTIONS = {
    "free_time.picked_up": "set the pickup date of {container} to {new} (was {old})",
    "free_time.returned": "set the empty return date of {container} to {new} (was {old})",
    "free_time.lfd_soon": "flagged {container}: last free day {last_free_day} is {days} day(s) away",
    "free_time.accruing": "flagged {container}: free time passed on {last_free_day}, {stage} is accruing",
    "customs_settings.updated": "changed the free time settings",
}

AUDIT_GROUPS = [("free_time.", "Free time"), ("customs_settings.", "Customs settings")]
