"""Public "Try it on your invoice" page (TRY_ENABLED=1). No sign-in; results only via a secret link."""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from apps.core.context import client_ip_var
from apps.documents.models import Document
from apps.documents.services import llm

from .services import tryit

log = logging.getLogger(__name__)
HONEYPOT = "website"


def _require_enabled():
    if not settings.TRY_ENABLED:
        raise Http404("The try page is not enabled on this server.")


def _private(response):
    # The site-wide Referrer-Policy (same-origin) already keeps the result URL from leaking to other
    # sites. "no-referrer" would make browsers send "Origin: null" and fail the CSRF check on POST.
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Cache-Control"] = "no-store, private"
    return response


def _page_context() -> dict:
    return {"max_mb": tryit.format_mb(tryit.max_bytes()), "max_pages": settings.TRY_MAX_PAGES,
            "retention_hours": settings.TRY_RETENTION_HOURS, "ai_reading": llm.is_enabled(),
            "contact_url": settings.DEMO_CONTACT_URL, "honeypot": HONEYPOT}


@never_cache
@require_http_methods(["GET", "POST"])
def try_page(request):
    _require_enabled()
    if request.method == "POST":
        return _submit(request)
    return _private(render(request, "demo/try.html", _page_context()))


def _submit(request):
    iph = tryit.ip_hash(client_ip_var.get())
    limited = tryit.rate_limit_message(iph)
    if limited:
        return _private(render(request, "demo/try.html", {**_page_context(), "error": limited}, status=429))
    tryit.count_attempt(iph)
    if request.POST.get(HONEYPOT):
        log.info("Try page: honeypot field filled, upload ignored")
        messages.error(request, "We couldn't take that upload. Reload the page and try again.")
        return redirect("demo:try")
    if tryit.daily_cap_reached():
        return _private(render(request, "demo/try.html", {**_page_context(), "error": (
            "Today's free tries are used up. Come back tomorrow, or contact us and we'll run your documents "
            "with you.")}, status=503))
    try:
        _sub, token = tryit.submit(request.FILES.get("file"), iph)
    except tryit.TryRejected as e:
        messages.error(request, str(e))
        return redirect("demo:try")
    return redirect("demo:try_result", token=token)


@never_cache
def try_result(request, token):
    _require_enabled()
    sub = tryit.find(token)
    if sub is None or sub.document is None:
        return _private(render(request, "demo/try_gone.html", _page_context(), status=404))
    doc = sub.document
    processing = doc.status == Document.Status.RECEIVED
    rows = [] if processing else tryit.result_rows(doc)
    issues = [] if processing else tryit.document_issues(doc)
    return _private(render(request, "demo/try_result.html", {
        **_page_context(), "sub": sub, "doc": doc, "token": token, "processing": processing,
        "rows": rows, "found": sum(1 for r in rows if r.state != "missing"),
        "line_items": [] if processing else (doc.field("line_items") or []),
        "issues": issues, "errors": sum(1 for i in issues if i["severity"] == "error"),
        "readable": doc.doc_type in ("commercial_invoice", "freight_invoice", "bill_of_lading"),
    }))


@require_POST
def try_delete(request, token):
    _require_enabled()
    sub = tryit.find(token)
    if sub is not None:
        tryit.delete_submission(sub)
    messages.success(request, "Your document and everything read from it were deleted.")
    return redirect("demo:try")
