from __future__ import annotations

from django import forms
from django.core.validators import MaxValueValidator, MinValueValidator

from .models import CloseSettings


class CloseSettingsForm(forms.ModelForm):
    class Meta:
        model = CloseSettings
        fields = ["accrued_account", "accrued_account_id", "freight_account", "goods_account", "include_goods",
                  "expect_freight", "expect_destination", "expect_delivery", "lookback_days", "min_history"]
        labels = {
            "accrued_account": "Accrued liabilities account",
            "accrued_account_id": "Its QuickBooks account ID",
            "freight_account": "Freight expense account",
            "goods_account": "Goods (inventory) account",
            "include_goods": "Accrue supplier invoices received but not booked",
            "expect_freight": "Ocean freight and surcharges",
            "expect_destination": "Destination port and customs",
            "expect_delivery": "Delivery (trucking)",
            "lookback_days": "Look-back window (days)",
            "min_history": "Past invoices needed for a median",
        }
        help_texts = {
            "accrued_account": "Credited by the accrual entry, for example Accrued liabilities or Accrued freight.",
            "accrued_account_id": "Optional. Fills the Account ID column of the journal entry template.",
            "freight_account": "Debited for vendors without an account of their own (Settings > QuickBooks, or the "
                               "account set on an invoice).",
            "goods_account": "Debited for supplier invoices without an account of their own.",
            "lookback_days": "Shipments that shipped longer ago than this before the period end are listed but not "
                             "accrued.",
            "min_history": "A vendor's or the organization's median is used only with at least this many past "
                           "invoices.",
        }

    RANGES = {"lookback_days": (7, 1095, "Use a window between 7 and 1,095 days."),
              "min_history": (1, 50, "Use a number between 1 and 50.")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The model's own "0 or more" rule fired first and gave a different message from the real range.
        for name, (low, high, message) in self.RANGES.items():
            field = self.fields[name]
            field.validators = [v for v in field.validators if not isinstance(v, (MinValueValidator, MaxValueValidator))]
            field.validators += [MinValueValidator(low, message), MaxValueValidator(high, message)]
            field.min_value, field.max_value = low, high
            field.widget.attrs.update(min=low, max=high)

    def clean_lookback_days(self):
        value = self.cleaned_data["lookback_days"]
        if not 7 <= value <= 1095:
            raise forms.ValidationError("Use a window between 7 and 1,095 days.")
        return value

    def clean_min_history(self):
        value = self.cleaned_data["min_history"]
        if not 1 <= value <= 50:
            raise forms.ValidationError("Use a number between 1 and 50.")
        return value

    def clean_accrued_account(self):
        value = (self.cleaned_data["accrued_account"] or "").strip()
        if not value:
            raise forms.ValidationError("Name the account the accrual is credited to.")
        return value
