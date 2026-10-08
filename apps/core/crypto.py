"""Symmetric encryption for secrets stored in the database (OAuth tokens)."""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


def _fernet() -> Fernet:
    key = os.environ.get("FIELD_ENCRYPTION_KEY")
    if not key:
        if not settings.DEBUG:
            raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY must be set when DJANGO_DEBUG=0")
        # Dev fallback derived from SECRET_KEY. Set FIELD_ENCRYPTION_KEY in production.
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest()).decode()
    return Fernet(key.encode())


class EncryptedTextField(models.TextField):
    """Stores text encrypted at rest; reads back plaintext."""

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            return ""

    def get_prep_value(self, value):
        if not value:
            return value
        return _fernet().encrypt(str(value).encode()).decode()
