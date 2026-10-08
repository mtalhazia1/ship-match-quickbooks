"""CSV cells that spreadsheet programs would run as formulas.

A file name or vendor name chosen by an outsider (e.g. "=HYPERLINK(...)") becomes a live formula when
finance opens an export in Excel. Such cells get a leading apostrophe, which spreadsheets show as text.
Plain numbers (including negative amounts like -12.50) are left alone.
"""
from __future__ import annotations

import re

TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
_NUMBER = re.compile(r"^[+-]?\d[\d,]*(\.\d+)?$")


def cell(value):
    if isinstance(value, str) and value.startswith(TRIGGERS) and not _NUMBER.match(value):
        return "'" + value
    return value


def row(values) -> list:
    return [cell(v) for v in values]


def unquote(value: str) -> str:
    """Undo cell() when a file exported here is imported again."""
    if isinstance(value, str) and len(value) > 1 and value[0] == "'" and value[1:].startswith(TRIGGERS):
        return value[1:]
    return value
