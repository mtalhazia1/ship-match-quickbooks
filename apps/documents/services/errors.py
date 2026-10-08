"""What a person is told when a document couldn't be processed.

The technical detail (exception text, traceback, file paths on the server) belongs in the server log, never on
a page: it is useless to a bookkeeper and shows how the server is built. `friendly` turns an exception into one
or two plain sentences, and `display` makes sure that whatever was stored on a document, including older
records that hold a raw traceback, is shown the same way."""
from __future__ import annotations

import re

PASSWORD = ("This PDF is protected with a password, so ShipMatch can't read it. Open it, choose Print > Save as "
            "PDF to make an unprotected copy, and upload that.")
DAMAGED = ("This file isn't a readable PDF. It may be damaged or cut short. Open the original, save it as PDF "
           "again, and upload that.")
GENERIC = ("ShipMatch couldn't read this file. Try processing it again; if it keeps failing, upload it again, "
           "as a PDF if you can.")

_PASSWORD_WORDS = ("password", "encrypt", "decrypt")
_DAMAGED_WORDS = ("no /root object", "is this really a pdf", "pdfsyntaxerror", "pseof", "pdfreaderror",
                  "emptyfileerror", "eof marker", "not a pdf", "stream has ended unexpectedly", "invalid pdf")
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:home|usr|var|srv|opt|app|tmp|Users)/)[^\s\"'<>|?*]*")


def _classify(text: str, exception_name: str = "") -> str:
    lowered = f"{exception_name} {text}".lower()
    if any(word in lowered for word in _PASSWORD_WORDS):
        return PASSWORD
    if any(word in lowered for word in _DAMAGED_WORDS):
        return DAMAGED
    return GENERIC


def friendly(exc: BaseException) -> str:
    """The sentence to store on a document whose processing raised `exc`."""
    return _classify(str(exc), type(exc).__name__)


def scrub(text: str) -> str:
    """Technical text with server paths removed (for audit rows an administrator may read)."""
    return _PATH.sub("<path>", text or "")


def display(stored: str) -> str:
    """What to show for a stored error. Anything that looks technical (a traceback, a path, several lines)
    is replaced by its plain-language equivalent; short human sentences pass through."""
    text = (stored or "").strip()
    if not text:
        return ""
    if "Traceback" in text or "\n" in text or _PATH.search(text) or " File \"" in text:
        # Older records are "<message>\n<traceback>". Only the message says what went wrong; the traceback's
        # source lines mention words like "password" all the time.
        message = text.split("Traceback", 1)[0].strip().splitlines()[0] if text.split("Traceback", 1)[0].strip() else ""
        return _classify(message)
    return text
