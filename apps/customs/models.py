"""Customs entries are documents (apps.documents); this app stores what comes from them over time:
per-container free time from arrival notices, the organization's free time settings, and AI opinions
on tariff codes (asked once per code and description)."""
from __future__ import annotations

from datetime import date

from django.conf import settings
from django.db import models

from apps.core.models import Organization


def _default_lead_days() -> int:
    return int(getattr(settings, "CUSTOMS_LFD_ALERT_DAYS", 2))


class CustomsSettings(models.Model):
    """How free time is counted and when the team hears about it, per organization."""

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="customs_settings")
    lfd_alert_days = models.PositiveSmallIntegerField(
        default=_default_lead_days, help_text="Alert this many days before a container's last free day")
    count_weekends = models.BooleanField(
        default=False, help_text="Count Saturdays, Sundays and holidays as free days when a notice doesn't say how "
                                 "its free days are counted")
    holidays = models.JSONField(default=list, blank=True,
                                help_text="Public holidays (YYYY-MM-DD) that are not counted as free days")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_org(cls, org: Organization) -> CustomsSettings:
        """The organization's settings, or unsaved defaults."""
        return cls.objects.filter(organization=org).first() or cls(organization=org)

    def holiday_dates(self) -> set[date]:
        out = set()
        for raw in self.holidays or []:
            try:
                out.add(date.fromisoformat(str(raw)))
            except ValueError:
                continue
        return out


class ContainerFreeTime(models.Model):
    """Free time for one container of one shipment, read from its arrival notices (latest notice wins) plus
    the pickup and empty return dates a person entered. Dates are local dates at the port; "today" is the
    organization's date (its time zone)."""

    class DetentionFrom(models.TextChoices):
        PICKUP = "pickup", "Pickup from the terminal"
        DISCHARGE = "discharge", "Discharge from the vessel"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="free_time")
    shipment = models.ForeignKey("shipments.Shipment", on_delete=models.CASCADE, related_name="free_time")
    document = models.ForeignKey("documents.Document", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+", help_text="Latest arrival notice for this container")
    container_number = models.CharField(max_length=15)
    carrier_name = models.CharField(max_length=200, blank=True)
    terminal = models.CharField(max_length=200, blank=True)
    eta = models.DateField(null=True, blank=True)
    discharge_date = models.DateField(null=True, blank=True)
    discharge_estimated = models.BooleanField(
        default=False, help_text="No discharge or arrival date printed: counted from the ETA")
    demurrage_free_days = models.PositiveSmallIntegerField(null=True, blank=True)
    detention_free_days = models.PositiveSmallIntegerField(null=True, blank=True)
    count_weekends = models.BooleanField(default=False, help_text="Weekends and holidays count as free days")
    basis_note = models.CharField(max_length=200, blank=True, help_text="How free days are counted, and why")
    detention_from = models.CharField(max_length=10, choices=DetentionFrom.choices, default=DetentionFrom.PICKUP)
    lfd_demurrage = models.DateField(null=True, blank=True, help_text="Last free day at the terminal")
    lfd_demurrage_printed = models.BooleanField(default=False)
    lfd_detention = models.DateField(null=True, blank=True, help_text="Last free day to return the empty")
    lfd_detention_printed = models.BooleanField(default=False)
    picked_up_on = models.DateField(null=True, blank=True)
    picked_up_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    returned_on = models.DateField(null=True, blank=True)
    returned_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    # Alerts already sent, as the last free day they were about: a new date (updated notice) alerts again.
    alerted_demurrage_soon = models.DateField(null=True, blank=True)
    alerted_demurrage_late = models.DateField(null=True, blank=True)
    alerted_detention_soon = models.DateField(null=True, blank=True)
    alerted_detention_late = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("shipment", "container_number")]
        ordering = ["lfd_demurrage", "container_number", "id"]
        indexes = [models.Index(fields=["organization", "returned_on"])]

    def __str__(self) -> str:
        return f"{self.container_number} ({self.shipment_id})"

    @property
    def has_manual_dates(self) -> bool:
        return bool(self.picked_up_on or self.returned_on)

    @property
    def stage(self) -> str:
        """demurrage (waiting at the terminal), detention (picked up, empty not returned) or closed."""
        if self.returned_on:
            return "closed"
        return "detention" if self.picked_up_on else "demurrage"


class HtsReview(models.Model):
    """The AI's opinion on whether a tariff code fits a line's description (asked once per code and wording)."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="hts_reviews")
    hts_code = models.CharField(max_length=12)
    description_key = models.CharField(max_length=200)
    plausible = models.BooleanField(default=True)
    reason = models.CharField(max_length=300, blank=True)
    model = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("organization", "hts_code", "description_key")]

    def __str__(self) -> str:
        return f"{self.hts_code} {self.description_key[:30]} ({'ok' if self.plausible else 'doubtful'})"
