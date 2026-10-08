"""Django system checks: a broken CUSTOMS_MPF_TABLE or CUSTOMS_HMF_RATE is reported by `manage.py check` (and at
start-up) instead of being ignored."""
from __future__ import annotations

from django.core.checks import Error, register

from .fees import FeeTableError, hmf_percent, parse_override


@register()
def customs_fee_settings(app_configs=None, **kwargs):
    from django.conf import settings

    errors = []
    try:
        parse_override(getattr(settings, "CUSTOMS_MPF_TABLE", ""))
    except FeeTableError as e:
        errors.append(Error(str(e), hint="Fix or remove CUSTOMS_MPF_TABLE in .env (see .env.example).",
                            id="customs.E001"))
    try:
        hmf_percent()
    except FeeTableError as e:
        errors.append(Error(str(e), hint="CUSTOMS_HMF_RATE is a percentage such as 0.125.", id="customs.E002"))
    return errors
