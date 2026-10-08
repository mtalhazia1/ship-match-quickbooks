"""Self-serve sign-ups, the organization's subscription (Stripe) and the onboarding checklist."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import Organization


class BillingAccount(models.Model):
    """One organization's plan and subscription. Stripe is the source of truth; webhooks keep this in sync.

    Organizations without a BillingAccount (created by an operator, the demo, try-page sandboxes) are billed
    outside ShipMatch: no plan limits apply to them.
    """

    class Status(models.TextChoices):
        TRIALING = "trialing", "Free trial"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Payment overdue"
        CANCELED = "canceled", "Canceled"

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="billing")
    plan = models.CharField(max_length=30, blank=True, help_text="Key in BILLING_PLANS")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TRIALING, db_index=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=64, blank=True, db_index=True)
    stripe_subscription_id = models.CharField(max_length=64, blank=True, db_index=True)
    stripe_status = models.CharField(max_length=30, blank=True, help_text="Subscription status as Stripe reports it")
    stripe_synced_at = models.DateTimeField(
        null=True, blank=True, help_text="Time of the newest Stripe subscription state applied (older events are ignored)")
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    soft_notice_for = models.DateTimeField(null=True, blank=True,
                                           help_text="Start of the billing month whose 100% email was sent")
    hard_notice_for = models.DateTimeField(null=True, blank=True,
                                           help_text="Start of the billing month whose paused-intake email was sent")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.organization} ({self.plan or 'no plan'}, {self.status})"

    @property
    def has_subscription(self) -> bool:
        return bool(self.stripe_subscription_id)

    @property
    def is_billed(self) -> bool:
        """Do plan limits apply? Not for an organization billed outside ShipMatch whose admin has only opened
        Checkout (no trial, no subscription yet)."""
        return (self.has_subscription or self.trial_ends_at is not None
                or self.status in (self.Status.ACTIVE, self.Status.PAST_DUE))

    @property
    def trial_over(self) -> bool:
        return (self.status == self.Status.TRIALING and not self.has_subscription
                and self.trial_ends_at is not None and self.trial_ends_at <= timezone.now())

    @property
    def trial_days_left(self) -> int | None:
        if self.status != self.Status.TRIALING or self.trial_ends_at is None:
            return None
        seconds = (self.trial_ends_at - timezone.now()).total_seconds()
        return max(0, int(-(-seconds // 86400)))  # round up: 1.2 days left reads as 2

    @property
    def intake_open(self) -> bool:
        """May new documents come in at all (before the usage limit)? Review, approval and posting never depend
        on this."""
        if self.status in (self.Status.ACTIVE, self.Status.PAST_DUE):
            return True
        if self.status == self.Status.TRIALING:
            return not self.trial_over
        return False

    @property
    def status_label(self) -> str:
        if self.trial_over:
            return "Trial ended"
        if self.status == self.Status.ACTIVE and self.cancel_at_period_end:
            return "Active until the end of the period"
        return self.get_status_display()


class StripeEvent(models.Model):
    """Every Stripe webhook event processed, by id, so a resent event is never applied twice."""

    event_id = models.CharField(max_length=80, unique=True)
    type = models.CharField(max_length=80)
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="stripe_events")
    created = models.DateTimeField(help_text="When Stripe created the event")
    livemode = models.BooleanField(default=False)
    outcome = models.CharField(max_length=300, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    LABELS = {
        "checkout.session.completed": "Checkout finished",
        "customer.subscription.created": "Subscription started",
        "customer.subscription.updated": "Subscription changed",
        "customer.subscription.deleted": "Subscription ended",
        "customer.subscription.paused": "Subscription paused",
        "customer.subscription.resumed": "Subscription resumed",
        "invoice.payment_failed": "Payment failed",
    }

    class Meta:
        ordering = ["-received_at", "-id"]

    def __str__(self) -> str:
        return f"{self.event_id} {self.type}"

    @property
    def label(self) -> str:
        return self.LABELS.get(self.type, self.type)


class PendingSignup(models.Model):
    """A sign-up waiting for its email to be verified. Nothing is created until the link is opened."""

    company_name = models.CharField(max_length=200)
    full_name = models.CharField(max_length=150)
    email = models.EmailField(db_index=True)
    password_hash = models.CharField(max_length=128)
    nonce = models.CharField(max_length=32, help_text="Part of the emailed link; a new sign-up gets a new one")
    ip_hash = models.CharField(max_length=40, blank=True)
    emails_sent = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.email} ({'verified' if self.verified_at else 'pending'})"


class Onboarding(models.Model):
    """The getting-started checklist shown on the dashboard of organizations created by self-serve sign-up."""

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="onboarding")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Onboarding of {self.organization}"
