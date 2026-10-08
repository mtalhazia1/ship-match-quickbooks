"""Landed cost per product and invoices shared by several shipments.

Landed cost
  * LandedSettings: how an organization spreads charges over the products of a shipment (by value,
    quantity, weight or volume, optionally per type of charge).
  * ShipmentLandedOverride: the same choice for one shipment.
  * LandedCostRun / LandedCostLine: the result frozen when a shipment is approved (exchange rates of
    that day), so the per-product report does not change when rates or settings change later.

Shared invoices
  * SharedInvoice: one freight invoice that covers several shipments (the invoice stays matched to one
    shipment, its MatchLink; the "primary" shipment), with the split's basis and its confirmation.
  * InvoiceAllocation: each shipment's share of that invoice. The shares add up to the invoice total.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.models import Organization
from apps.documents.models import Document
from apps.shipments.models import Shipment


class Method(models.TextChoices):
    VALUE = "value", "By value"
    QUANTITY = "quantity", "By quantity"
    WEIGHT = "weight", "By weight"
    VOLUME = "volume", "By volume"


class Category(models.TextChoices):
    FREIGHT = "freight", "Freight"
    DUTY = "duty", "Duties and taxes"
    INSURANCE = "insurance", "Insurance"
    HANDLING = "handling", "Handling and port"
    OTHER = "other", "Other charges"


class LandedSettings(models.Model):
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="landed_settings")
    method = models.CharField(max_length=12, choices=Method.choices, default=Method.VALUE)
    by_category = models.JSONField(default=dict, blank=True,
                                   help_text='Method per type of charge, e.g. {"freight": "volume", "duty": "value"}')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_org(cls, org: Organization) -> LandedSettings:
        found = cls.objects.filter(organization=org).first()
        if found:
            return found
        default = settings.LANDED_DEFAULT_METHOD if settings.LANDED_DEFAULT_METHOD in Method.values else Method.VALUE
        return cls(organization=org, method=default, by_category={})


class ShipmentLandedOverride(models.Model):
    shipment = models.OneToOneField(Shipment, on_delete=models.CASCADE, related_name="landed_override")
    method = models.CharField(max_length=12, choices=Method.choices, default=Method.VALUE)
    by_category = models.JSONField(default=dict, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)


class LandedCostRun(models.Model):
    """Landed cost of an approved shipment, frozen at approval."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="landed_runs")
    shipment = models.OneToOneField(Shipment, on_delete=models.CASCADE, related_name="landed_run")
    as_of = models.DateField(help_text="Date of the goods (commercial invoice date, else B/L date)")
    currency = models.CharField(max_length=3)
    goods_total = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    charges_total = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    landed_total = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    complete = models.BooleanField(default=True, help_text="False when products or exchange rates were missing")
    policy = models.JSONField(default=dict, blank=True)
    notes = models.JSONField(default=list, blank=True)
    charges = models.JSONField(default=list, blank=True, help_text="Each charge and how it was spread")
    computed_at = models.DateTimeField(auto_now_add=True)


class LandedCostLine(models.Model):
    run = models.ForeignKey(LandedCostRun, on_delete=models.CASCADE, related_name="lines")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="+")
    position = models.PositiveIntegerField(default=0)
    product_key = models.CharField(max_length=260, db_index=True, help_text="Supplier + SKU (or description)")
    sku = models.CharField(max_length=60, blank=True)
    description = models.CharField(max_length=300, blank=True)
    hs_code = models.CharField(max_length=20, blank=True)
    vendor_name = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    volume_cbm = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    goods_value = models.DecimalField(max_digits=16, decimal_places=2)
    charges = models.DecimalField(max_digits=16, decimal_places=2)
    by_category = models.JSONField(default=dict, blank=True)
    landed_total = models.DecimalField(max_digits=16, decimal_places=2)
    per_unit = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    as_of = models.DateField(db_index=True)

    class Meta:
        ordering = ["run_id", "position"]


class SharedInvoice(models.Model):
    class Basis(models.TextChoices):
        LINES = "lines", "By the lines that name each shipment"
        EQUAL = "equal", "Equally"
        CONTAINERS = "containers", "By number of containers"
        WEIGHT = "weight", "By weight"
        VOLUME = "volume", "By volume"
        MANUAL = "manual", "Amounts typed by a reviewer"

    class Status(models.TextChoices):
        ACTIVE = "active", "Shared"
        DISMISSED = "dismissed", "Not shared"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="shared_invoices")
    document = models.OneToOneField(Document, on_delete=models.CASCADE, related_name="shared_invoice")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    basis = models.CharField(max_length=12, choices=Basis.choices, default=Basis.CONTAINERS)
    total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    detected = models.JSONField(default=dict, blank=True,
                                help_text="References found on the invoice per shipment, approved shipments it names, "
                                          "notes from the split")
    confirmed_hash = models.CharField(max_length=40, blank=True, help_text="The split a person confirmed")
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class InvoiceAllocation(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="invoice_allocations")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="allocations")
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="invoice_shares")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3)
    basis = models.CharField(max_length=12, choices=SharedInvoice.Basis.choices)
    reason = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "shipment"], name="landed_one_share_per_shipment")]
        ordering = ["document_id", "id"]
