"""Webhook signatures, and a reference check for the systems that receive them.

Every request carries:
    ShipMatch-Timestamp: 1767225600
    ShipMatch-Signature: v1=5257a869e7ec...[,v1=<signature with the previous secret, during a rotation>]
where v1 = hex(HMAC-SHA256(endpoint secret, "<timestamp>.<raw request body>")).

A receiver recomputes v1 with its secret over the raw body (before parsing the JSON), compares it in constant
time with each v1 in the header, and rejects timestamps more than a few minutes old (replays). Events can be
delivered more than once (retries, "Replay"): de-duplicate on the event "id".
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

SIGNATURE_HEADER = "ShipMatch-Signature"
TIMESTAMP_HEADER = "ShipMatch-Timestamp"


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def signature_header(secrets_: list[str], timestamp: int, body: bytes) -> str:
    return ",".join(f"v1={sign(s, timestamp, body)}" for s in secrets_)


def verify(secret: str, body: bytes, timestamp_header: str, signature_header_value: str, tolerance: int = 300,
           now: float | None = None) -> bool:
    """What a receiving system should do (Python). True when the request came from ShipMatch and is fresh."""
    try:
        ts = int(timestamp_header)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    if abs(now - ts) > tolerance:
        return False
    expected = sign(secret, ts, body)
    candidates = [p.strip()[3:] for p in (signature_header_value or "").split(",") if p.strip().startswith("v1=")]
    return any(hmac.compare_digest(expected, c) for c in candidates)
