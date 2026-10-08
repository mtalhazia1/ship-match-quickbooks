"""LLM client returning structured JSON, for Anthropic (Claude) or OpenAI.

Plain HTTP (httpx), so there is no SDK version lock-in.

Anthropic: JSON output is constrained with `output_config.format` (structured outputs), and the
original PDF can be sent as a `document` block so Claude sees the layout, tables and scans.
Docs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
      https://platform.claude.com/docs/en/build-with-claude/pdf-support

Every call records tokens, latency and an estimated cost in the active `track_usage()` collector,
so each document stores what it cost to read.
"""
from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import logging
import random
import time
from dataclasses import asdict, dataclass

import httpx
from django.conf import settings

log = logging.getLogger(__name__)

DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-4o-mini"}
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504, 529}
MAX_ATTEMPTS = 4

# USD per million tokens (input, output), from https://platform.claude.com/docs/en/models/overview
# (checked 2 Oct 2026). Used only for cost estimates shown to admins.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------- usage tracking


@dataclass
class Call:
    purpose: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    ms: int = 0
    pdf: bool = False
    attempts: int = 1

    @property
    def cost_usd(self) -> float:
        inp, out = PRICES.get(self.model, (0.0, 0.0))
        return round((self.input_tokens * inp + self.output_tokens * out) / 1_000_000, 6)

    def as_dict(self) -> dict:
        return {**asdict(self), "cost_usd": self.cost_usd}


_usage: contextvars.ContextVar[list | None] = contextvars.ContextVar("llm_usage", default=None)


@contextlib.contextmanager
def track_usage():
    """Collect every LLM call made inside the block: `with track_usage() as calls: ...`."""
    calls: list[Call] = []
    token = _usage.set(calls)
    try:
        yield calls
    finally:
        _usage.reset(token)


def summarize(calls: list[Call]) -> dict:
    return {
        "calls": [c.as_dict() for c in calls],
        "input_tokens": sum(c.input_tokens for c in calls),
        "output_tokens": sum(c.output_tokens for c in calls),
        "cost_usd": round(sum(c.cost_usd for c in calls), 6),
        "ms": sum(c.ms for c in calls),
    }


def _record(call: Call) -> None:
    calls = _usage.get()
    if calls is not None:
        calls.append(call)
    log.info("llm call purpose=%s model=%s in=%s out=%s ms=%s cost=$%.4f", call.purpose, call.model,
             call.input_tokens, call.output_tokens, call.ms, call.cost_usd)


# --------------------------------------------------------------------------- public API


def provider() -> str:
    return settings.EXTRACTION_PROVIDER


def is_enabled() -> bool:
    return provider() in {"anthropic", "openai"}


def model_name() -> str:
    return settings.LLM_MODEL or DEFAULT_MODELS.get(provider(), "")


def can_read_pdfs() -> bool:
    return provider() == "anthropic" and bool(settings.ANTHROPIC_API_KEY)


def structured_call(system: str, user: str, schema: dict, name: str = "record", timeout: float = 120.0,
                    client: httpx.Client | None = None, pdf: bytes | None = None, purpose: str = "extract") -> dict:
    """Send one prompt (optionally with the PDF) and return a dict shaped by `schema`."""
    prov = provider()
    http = client or httpx.Client(timeout=timeout)
    try:
        if prov == "anthropic":
            return _anthropic_json(http, model_name(), system, user, schema, pdf, purpose)
        if prov == "openai":
            return _openai(http, model_name(), system, user, schema, name, purpose)
        raise LLMError(f"EXTRACTION_PROVIDER={prov!r} is not an LLM provider")
    finally:
        if client is None:
            http.close()


def transcribe_pdf(pdf: bytes, client: httpx.Client | None = None, timeout: float = 180.0) -> str:
    """OCR with Claude: return the text of a scanned PDF, page by page, as printed."""
    if not settings.ANTHROPIC_API_KEY:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    http = client or httpx.Client(timeout=timeout)
    try:
        body = {
            "model": model_name() if provider() == "anthropic" else DEFAULT_MODELS["anthropic"],
            "max_tokens": 16000,
            "system": ("You transcribe scanned business documents. Output only the text printed on each page, "
                       "top to bottom, keeping 'Label: value' pairs and table rows on one line. Separate pages "
                       "with a line containing only ----PAGE----. Do not summarize, translate or correct anything."),
            "messages": [{"role": "user", "content": [_pdf_block(pdf), {"type": "text", "text": "Transcribe this document."}]}],
        }
        data, call = _anthropic_post(http, body, purpose="ocr", pdf=True)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        if data.get("stop_reason") == "max_tokens":
            log.warning("Transcription hit max_tokens; text may be incomplete")
        return text.replace("----PAGE----", "\f").strip()
    finally:
        if client is None:
            http.close()


# --------------------------------------------------------------------------- Anthropic


def _pdf_block(pdf: bytes) -> dict:
    return {"type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf).decode()}}


def _anthropic_json(http, model, system, user, schema, pdf, purpose) -> dict:
    if not settings.ANTHROPIC_API_KEY:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    content = ([_pdf_block(pdf)] if pdf else []) + [{"type": "text", "text": user}]
    body = {
        "model": model,
        "max_tokens": 8192,
        "temperature": 0,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    data, _ = _anthropic_post(http, body, purpose=purpose, pdf=bool(pdf))
    if data.get("stop_reason") == "refusal":
        raise LLMError("Claude declined to process this document")
    if data.get("stop_reason") == "max_tokens":
        raise LLMError("Claude's answer was cut off (max_tokens); the document may be too long")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"Claude returned invalid JSON: {e}") from e


def _anthropic_post(http: httpx.Client, body: dict, purpose: str, pdf: bool) -> tuple[dict, Call]:
    headers = {"x-api-key": settings.ANTHROPIC_API_KEY, "anthropic-version": ANTHROPIC_VERSION,
               "content-type": "application/json"}
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            r = http.post(ANTHROPIC_URL, headers=headers, json=body)
        except httpx.HTTPError as e:
            if attempt >= MAX_ATTEMPTS:
                raise LLMError(f"Anthropic request failed: {e}") from e
            _sleep(attempt, None)
            continue
        if r.status_code == 400 and "temperature" in body and "temperature" in r.text:
            body = {k: v for k, v in body.items() if k != "temperature"}  # model does not accept it
            continue
        if r.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
            _sleep(attempt, r.headers.get("retry-after"))
            continue
        if r.status_code >= 400:
            raise LLMError(f"Anthropic {r.status_code}: {_error_message(r)}")
        data = r.json()
        usage = data.get("usage") or {}
        call = Call(purpose=purpose, provider="anthropic", model=data.get("model") or body["model"],
                    input_tokens=int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0) or 0)
                    + int(usage.get("cache_creation_input_tokens", 0) or 0),
                    output_tokens=int(usage.get("output_tokens", 0)),
                    ms=int((time.monotonic() - started) * 1000), pdf=pdf, attempts=attempt)
        _record(call)
        return data, call


def _error_message(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or {}
        return f"{err.get('type', '')}: {err.get('message', '')}".strip(": ") or r.text[:300]
    except ValueError:
        return r.text[:300]


def _sleep(attempt: int, retry_after: str | None) -> None:
    try:
        delay = float(retry_after) if retry_after else 0.0
    except ValueError:
        delay = 0.0
    delay = max(delay, min(30.0, 1.5 ** attempt + random.random()))
    time.sleep(delay)


# --------------------------------------------------------------------------- OpenAI


def _openai(http, model, system, user, schema, name, purpose) -> dict:
    if not settings.OPENAI_API_KEY:
        raise LLMError("OPENAI_API_KEY is not set")
    started = time.monotonic()
    try:
        r = http.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={
                "model": model,
                "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema}},
            },
        )
    except httpx.HTTPError as e:
        raise LLMError(f"OpenAI request failed: {e}") from e
    if r.status_code >= 400:
        raise LLMError(f"OpenAI {r.status_code}: {r.text[:500]}")
    data = r.json()
    usage = data.get("usage") or {}
    _record(Call(purpose=purpose, provider="openai", model=model, input_tokens=int(usage.get("prompt_tokens", 0)),
                 output_tokens=int(usage.get("completion_tokens", 0)), ms=int((time.monotonic() - started) * 1000)))
    try:
        return json.loads(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise LLMError(f"OpenAI response was not valid JSON: {e}") from e
