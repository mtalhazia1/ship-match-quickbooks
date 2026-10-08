"""ISO 6346 container numbers.

Format: 3-letter owner code + category letter (U, J or Z) + 6-digit serial + 1 check digit,
e.g. CSQU3054383. The check digit catches most OCR and typing mistakes deterministically.
"""
from __future__ import annotations

import re

_LETTER_VALUES: dict[str, int] = {}
_v = 10
for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    if _v % 11 == 0:  # values that are multiples of 11 are skipped by the standard
        _v += 1
    _LETTER_VALUES[_ch] = _v
    _v += 1

CONTAINER_RE = re.compile(r"\b([A-Z]{3}[UJZ])\s?-?\s?(\d{6})\s?-?\s?(\d)\b")


def normalize_container(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def check_digit(first10: str) -> int:
    """Compute the check digit for the first 10 characters (owner+category+serial)."""
    total = 0
    for i, ch in enumerate(first10):
        value = _LETTER_VALUES[ch] if ch.isalpha() else int(ch)
        total += value * (2**i)
    return total % 11 % 10


def is_valid_container(raw: str) -> bool:
    c = normalize_container(raw)
    if not re.fullmatch(r"[A-Z]{3}[UJZ]\d{7}", c):
        return False
    return check_digit(c[:10]) == int(c[10])


def make_container(owner: str, serial: int) -> str:
    """Build a valid container number, e.g. make_container('OSL', 123456) -> 'OSLU1234565'."""
    first10 = f"{owner.upper()}U{serial:06d}"
    return f"{first10}{check_digit(first10)}"


def find_containers(text: str) -> list[str]:
    """All container-like strings in text, normalized, in order of appearance, without duplicates."""
    seen: list[str] = []
    for m in CONTAINER_RE.finditer((text or "").upper()):
        c = "".join(m.groups())
        if c not in seen:
            seen.append(c)
    return seen
