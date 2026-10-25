"""Brute-force protection: lock a username (and an IP) after repeated failed sign-ins.

Counters live in the Django cache. Use Redis (CACHE_URL) in production so all web
workers share them; the local-memory default is per process.

If the cache is unreachable, sign-in carries on without the counters (the password and 2FA are still checked)
rather than failing for everyone (QA-094); the outage is logged.
"""
from __future__ import annotations

import contextlib
import logging

from django.conf import settings
from django.core.cache import cache

from apps.core.resilience import CACHE_ERRORS

log = logging.getLogger(__name__)


def _keys(username: str, ip: str | None) -> tuple[str, str]:
    return f"login-fail:user:{(username or '').strip().lower()}", f"login-fail:ip:{ip or '-'}"


def is_locked(username: str, ip: str | None) -> bool:
    user_key, ip_key = _keys(username, ip)
    try:
        return (cache.get(user_key, 0) >= settings.LOGIN_MAX_FAILURES_PER_USER
                or cache.get(ip_key, 0) >= settings.LOGIN_MAX_FAILURES_PER_IP)
    except CACHE_ERRORS:
        log.warning("Sign-in lockout not checked: cache unreachable")
        return False


def register_failure(username: str, ip: str | None) -> int:
    """Count one failure. Returns the failure count for this username."""
    user_key, ip_key = _keys(username, ip)
    window = settings.LOGIN_LOCKOUT_SECONDS
    try:
        for key in (user_key, ip_key):
            if cache.add(key, 1, window) is False:
                try:
                    cache.incr(key)
                except ValueError:
                    cache.set(key, 1, window)
        return cache.get(user_key, 0)
    except CACHE_ERRORS:
        log.warning("Failed sign-in not counted: cache unreachable")
        return 0


def allow_password_reset(email: str, ip: str | None) -> bool:
    """Count a password-reset request; False once this address or IP asked too often this hour (QA-097).
    The caller shows the same page either way, so the limit reveals nothing about the address."""
    hour = 3600
    limits = ((f"reset:email:{(email or '').strip().lower()}", settings.PASSWORD_RESET_PER_EMAIL_PER_HOUR),
              (f"reset:ip:{ip or '-'}", settings.PASSWORD_RESET_PER_IP_PER_HOUR))
    allowed = True
    try:
        for key, limit in limits:
            if cache.add(key, 1, hour) is False:
                try:
                    n = cache.incr(key)
                except ValueError:
                    cache.set(key, 1, hour)
                    n = 1
                allowed = allowed and n <= limit
    except CACHE_ERRORS:
        log.warning("Password-reset limit not checked: cache unreachable")
    return allowed


def reset(username: str, ip: str | None) -> None:
    user_key, _ = _keys(username, ip)
    with contextlib.suppress(*CACHE_ERRORS):
        cache.delete(user_key)
