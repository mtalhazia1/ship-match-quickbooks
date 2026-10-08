from __future__ import annotations

from decimal import Decimal

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from apps.accounting.models import vendor_key

from . import charges, lanes
from .models import ApprovedAccessorial, Quote, QuoteCharge, RateSettings

MAX_CHARGE = Decimal("1000000")   # no single freight, port or customs charge is expected above this; a typo is


def _currency(value: str) -> str:
    cur = (value or "").strip().upper()
    if len(cur) != 3 or not cur.isalpha():
        raise forms.ValidationError("Use a three-letter currency code such as USD.")
    return cur


def _vendor(value: str) -> str:
    name = (value or "").strip()
    if not vendor_key(name):
        raise forms.ValidationError("Enter the vendor's name as it appears on its invoices.")
    return name


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class QuoteForm(forms.ModelForm):
    class Meta:
        model = Quote
        fields = ["vendor_name", "reference", "origin", "destination", "equipment", "valid_from", "valid_to",
                  "currency", "all_in", "notes"]
        widgets = {
            "vendor_name": forms.TextInput(attrs={"list": "vendor-names", "autocomplete": "off", "class": "w-full"}),
            "reference": forms.TextInput(attrs={"class": "w-full"}),
            "origin": forms.TextInput(attrs={"list": "port-names", "autocomplete": "off", "class": "w-full"}),
            "destination": forms.TextInput(attrs={"list": "port-names", "autocomplete": "off", "class": "w-full"}),
            "valid_from": DateInput(),
            "valid_to": DateInput(),
            "currency": forms.TextInput(attrs={"maxlength": 3, "size": 5}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {"vendor_name": "Vendor", "reference": "Quote reference", "origin": "Origin (port of loading)",
                  "destination": "Destination (port of discharge)", "all_in": "All-in rate"}

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization or getattr(self.instance, "organization", None)

    def clean_vendor_name(self):
        return _vendor(self.cleaned_data.get("vendor_name"))

    def clean_currency(self):
        return _currency(self.cleaned_data.get("currency"))

    def clean(self):
        data = super().clean()
        start, end = data.get("valid_from"), data.get("valid_to")
        if start and end and end < start:
            self.add_error("valid_to", "The end date is before the start date.")
        if self.organization is not None and data.get("vendor_name") and start:
            same = Quote.objects.filter(
                organization=self.organization, vendor_key=vendor_key(data["vendor_name"]),
                reference__iexact=(data.get("reference") or ""), origin__iexact=(data.get("origin") or ""),
                destination__iexact=(data.get("destination") or ""), equipment=(data.get("equipment") or ""),
                valid_from=start).exclude(pk=self.instance.pk)
            if same.exists():
                raise forms.ValidationError(
                    "This quote already exists (same vendor, lane, equipment and start date). Open it instead, or "
                    f"change the dates to enter a new period. Existing quote: #{same.first().pk}.")
        return data


class ChargeForm(forms.ModelForm):
    class Meta:
        model = QuoteCharge
        fields = ["code", "description", "amount", "basis"]
        widgets = {"description": forms.TextInput(attrs={"placeholder": "As the vendor writes it (optional)"}),
                   "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0", "class": "num"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].choices = [("", "Choose a charge")] + charges.CODE_CHOICES

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and amount < 0:
            raise forms.ValidationError("Use a positive amount. Enter discounts as a lower rate.")
        if amount is not None and amount == 0:
            raise forms.ValidationError("Enter an amount above zero. A charge the quote doesn't list is "
                                        "already treated as zero, so leave the line out instead.")
        if amount is not None and amount > MAX_CHARGE:
            raise forms.ValidationError(f"Check the amount: no single charge is expected to be more than "
                                        f"{MAX_CHARGE:,.0f}.")
        return amount


class UniqueChargesFormSet(BaseInlineFormSet):
    """The same charge priced the same way twice in one quote makes it unclear which price applies."""

    def clean(self):
        super().clean()
        seen = set()
        for form in self.forms:
            if not getattr(form, "cleaned_data", None) or form.cleaned_data.get("DELETE"):
                continue
            code, basis = form.cleaned_data.get("code"), form.cleaned_data.get("basis")
            if not code:
                continue
            if (code, basis) in seen:
                raise forms.ValidationError(
                    f"{charges.label(code)} is listed twice with the same basis. Keep one line, or use a "
                    "different basis if they are priced differently.")
            seen.add((code, basis))


ChargeFormSet = inlineformset_factory(Quote, QuoteCharge, form=ChargeForm, formset=UniqueChargesFormSet, extra=3,
                                      can_delete=True, min_num=1, validate_min=True, max_num=60)


class AccessorialForm(forms.ModelForm):
    class Meta:
        model = ApprovedAccessorial
        fields = ["vendor_name", "code", "unit", "free_units", "max_per_unit", "max_amount", "currency",
                  "valid_from", "valid_to", "notes"]
        widgets = {
            "vendor_name": forms.TextInput(attrs={"list": "vendor-names", "autocomplete": "off", "class": "w-full"}),
            "free_units": forms.NumberInput(attrs={"min": "0", "step": "1"}),
            "max_per_unit": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "max_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "currency": forms.TextInput(attrs={"maxlength": 3, "size": 5}),
            "valid_from": DateInput(),
            "valid_to": DateInput(),
            "notes": forms.TextInput(attrs={"class": "w-full", "placeholder": "Where this was agreed, e.g. contract clause 4.2"}),
        }
        labels = {"vendor_name": "Vendor", "code": "Extra charge", "unit": "Charged", "free_units": "Free time",
                  "max_per_unit": "Most per day, hour or time", "max_amount": "Most per invoice"}

    def clean_vendor_name(self):
        return _vendor(self.cleaned_data.get("vendor_name"))

    def clean_currency(self):
        return _currency(self.cleaned_data.get("currency"))

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization or getattr(self.instance, "organization", None)

    def clean(self):
        data = super().clean()
        for name in ("max_per_unit", "max_amount"):
            if data.get(name) is not None and data[name] < 0:
                self.add_error(name, "Use a positive amount, or leave it empty for no cap.")
            elif data.get(name) is not None and data[name] > MAX_CHARGE:
                self.add_error(name, f"Check the amount: more than {MAX_CHARGE:,.0f} is unlikely.")
        if data.get("valid_from") and data.get("valid_to") and data["valid_to"] < data["valid_from"]:
            self.add_error("valid_to", "The end date is before the start date.")
        if self.organization is not None and data.get("vendor_name") and data.get("code"):
            same = ApprovedAccessorial.objects.filter(
                organization=self.organization, vendor_key=vendor_key(data["vendor_name"]), code=data["code"],
                unit=data.get("unit"), valid_from=data.get("valid_from")).exclude(pk=self.instance.pk)
            if same.exists():
                raise forms.ValidationError("This approval already exists (same vendor, charge, unit and start "
                                            "date). Edit it instead of adding it again.")
        return data


class RateSettingsForm(forms.ModelForm):
    class Meta:
        model = RateSettings
        fields = ["tolerance_percent", "tolerance_amount", "warn_no_quote", "check_unlisted_vendors", "ai_classify"]
        widgets = {"tolerance_percent": forms.NumberInput(attrs={"step": "0.1", "min": "0", "max": "50"}),
                   "tolerance_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"})}

    def clean_tolerance_percent(self):
        v = self.cleaned_data.get("tolerance_percent")
        if v is None or not Decimal("0") <= v <= Decimal("50"):
            raise forms.ValidationError("Use a percentage from 0 to 50.")
        return v

    def clean_tolerance_amount(self):
        v = self.cleaned_data.get("tolerance_amount")
        if v is None or v < 0:
            raise forms.ValidationError("Use zero or a positive amount.")
        return v


class AliasForm(forms.Form):
    example = forms.CharField(max_length=200)
    code = forms.ChoiceField(choices=charges.CODE_CHOICES)

    def clean_example(self):
        text = self.cleaned_data["example"].strip()
        if not charges.alias_key(text):
            raise forms.ValidationError("Enter the charge name as it appears on the invoice, with words in it.")
        return text


EQUIPMENT_FILTER = [(c, label) for c, label in lanes.EQUIPMENT_CHOICES if c]
