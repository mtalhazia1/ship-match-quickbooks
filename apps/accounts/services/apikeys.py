"""API keys: shown once at creation, stored as SHA-256 hashes, looked up by a public prefix.

Token format: sm_<prefix>_<secret>  (prefix 8 chars, secret 40 chars)
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.utils import timezone

from apps.accounts.models import ApiKey


def create_key(organization, name: str, role: str, created_by, days_valid: int | None,
               scopes: list[str] | None = None) -> tuple[ApiKey, str]:
    prefix = secrets.token_hex(4)
    while ApiKey.objects.filter(prefix=prefix).exists():
        prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(30)
    key = ApiKey.objects.create(
        organization=organization, name=name[:100], prefix=prefix, key_hash=_hash(secret), role=role,
        created_by=created_by, scopes=list(scopes or []),
        expires_at=timezone.now() + timedelta(days=days_valid) if days_valid else None,
    )
    return key, f"sm_{prefix}_{secret}"


def verify(token: str) -> ApiKey | None:
    parts = (token or "").split("_", 2)
    if len(parts) != 3 or parts[0] != "sm":
        return None
    key = ApiKey.objects.select_related("organization").filter(prefix=parts[1]).first()
    if not key or not key.is_active or not secrets.compare_digest(key.key_hash, _hash(parts[2])):
        return None
    now = timezone.now()
    if not key.last_used_at or now - key.last_used_at > timedelta(minutes=1):
        ApiKey.objects.filter(pk=key.pk).update(last_used_at=now)
    return key


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()
