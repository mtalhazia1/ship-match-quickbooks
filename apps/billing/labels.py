"""Audit log wording for sign-up and billing. Added to apps.shipments.labels.ACTIONS at start-up."""

ACTIONS = {
    "signup.completed": "signed up {company} ({email})",
    "billing.checkout_started": "opened Stripe checkout for the {plan} plan",
    "billing.checkout_completed": "subscribed to the {plan} plan ({status})",
    "billing.portal_opened": "opened the Stripe billing portal",
    "billing.stripe_event": "Stripe {event}: {outcome}",
    "billing.payment_failed": "Stripe couldn't take a payment of {amount}",
    "billing.intake_paused": "refused a new document: {reason}",
    "onboarding.dismissed": "hid the getting-started list",
}

AUDIT_GROUPS = [("billing.", "Billing"), ("signup.", "Sign-up")]
