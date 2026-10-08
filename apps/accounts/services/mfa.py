"""Two-factor authentication with authenticator apps (TOTP, RFC 6238) and one-time recovery codes."""
from __future__ import annotations

import hashlib
import secrets

import pyotp
import segno
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import Profile

ISSUER = "ShipMatch"
RECOVERY_CODE_COUNT = 10


def profile_for(user) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def start_enrollment(user) -> tuple[str, str]:
    """Create a new secret (not active until confirmed). Returns (secret, QR code SVG data URI)."""
    profile = profile_for(user)
    secret = pyotp.random_base32()
    profile.mfa_secret = secret
    profile.mfa_enabled_at = None
    profile.save(update_fields=["mfa_secret", "mfa_enabled_at"])
    return secret, qr_data_uri(user, secret)


def qr_data_uri(user, secret: str) -> str:
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.get_username(), issuer_name=ISSUER)
    return segno.make(uri, error="m").svg_data_uri(scale=5, border=2)


def verify_totp(user, code: str) -> bool:
    profile = profile_for(user)
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not profile.mfa_secret or len(code) != 6:
        return False
    totp = pyotp.TOTP(profile.mfa_secret)
    for offset in (-1, 0, 1):  # allow 30 s clock drift either way
        at = timezone.now().timestamp() + offset * 30
        if secrets.compare_digest(totp.at(at), code):
            step = int(at // 30)
            key = f"mfa:last-step:{user.pk}"
            if cache.get(key, -1) >= step:  # a code can be used only once
                return False
            cache.set(key, step, 120)
            return True
    return False


def confirm_enrollment(user, code: str) -> list[str] | None:
    """Activate 2FA if the code from the app is correct. Returns new recovery codes, or None."""
    if not verify_totp(user, code):
        return None
    profile = profile_for(user)
    profile.mfa_enabled_at = timezone.now()
    profile.save(update_fields=["mfa_enabled_at"])
    return new_recovery_codes(user)


def new_recovery_codes(user) -> list[str]:
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(RECOVERY_CODE_COUNT)]
    profile = profile_for(user)
    profile.recovery_codes = [_hash(c) for c in codes]
    profile.save(update_fields=["recovery_codes"])
    return codes


def use_recovery_code(user, code: str) -> bool:
    profile = profile_for(user)
    hex_only = "".join(ch for ch in (code or "").lower() if ch in "0123456789abcdef")
    if len(hex_only) != 12:   # a code is 6 + 6 hex digits; spaces, dashes and capitals are not required
        return False
    h = _hash(f"{hex_only[:6]}-{hex_only[6:]}")
    if h in profile.recovery_codes:
        profile.recovery_codes = [c for c in profile.recovery_codes if c != h]
        profile.save(update_fields=["recovery_codes"])
        return True
    return False


def disable(user) -> None:
    profile = profile_for(user)
    profile.mfa_secret, profile.mfa_enabled_at, profile.recovery_codes = "", None, []
    profile.save(update_fields=["mfa_secret", "mfa_enabled_at", "recovery_codes"])


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()
