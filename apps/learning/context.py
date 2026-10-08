"""The vendor notes for the document being read right now, picked up by the AI extraction prompt.

A context variable keeps extract() free of a learning parameter: the pipeline sets the notes with
`use_hint(...)` around the extract() call, and extract adds `current_hint()` to its prompt.
"""
from __future__ import annotations

import contextlib
import contextvars

_hint: contextvars.ContextVar[str] = contextvars.ContextVar("learning_hint", default="")


def current_hint() -> str:
    return _hint.get()


@contextlib.contextmanager
def use_hint(text: str):
    token = _hint.set(text or "")
    try:
        yield
    finally:
        _hint.reset(token)
