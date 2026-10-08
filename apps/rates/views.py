"""Rates: quotes, approved extra charges, charge names and checking rules.

Permissions:
  * view    - see quotes, approved extra charges, charge names and checking rules; export CSV.
  * approve - add, change, copy, archive, delete and import quotes and approved extra charges, teach
              charge names, re-check open shipments. Approvers and admins already decide whether an
              overcharge gets paid; reviewers, who prepare shipments, can't move the bar their own
              shipments are checked against (segregation of duties).
  * manage  - change the checking rules (tolerance and switches), like other organization settings.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.forms import inlineformset_factory
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounting.models import vendor_key
from apps.core.models import AuditEvent
from apps.core.paging import Paginator
from apps.core.permissions import has_perm, require
from apps.core.utils import audit, current_org
from apps.documents.models import Document, ExtractedField
from apps.shipments.labels import describe_action
from apps.shipments.models import ValidationIssue

from . import charges, csvio, lanes
from .forms import (
    EQUIPMENT_FILTER,
    AccessorialForm,
    AliasForm,
    ChargeForm,
    ChargeFormSet,
    QuoteForm,
    RateSettingsForm,
    UniqueChargesFormSet,
)
from .models import ApprovedAccessorial, ChargeAlias, Quote, QuoteCharge, RateSettings
from .services import changes, recheck, snapshot

STATUS_TABS = [("active", "Current and upcoming"), ("expired", "Expired"), ("archived", "Archived"), ("all", "All")]
PER_PAGE = 50
IMPORT_ERRORS_KEY = "rates_import_errors"


# ---------------------------------------------------------------- helpers


def _vendor_names(org) -> list[str]:
    names = set(Quote.objects.filter(organization=org).values_list("vendor_name", flat=True))
    names |= set(ApprovedAccessorial.objects.filter(organization=org).values_list("vendor_name", flat=True))
    seen = (ExtractedField.objects.filter(document__organization=org, name="vendor_name",
                                          document__doc_type=Document.DocType.FREIGHT_INVOICE)
            .values_list("value", flat=True).distinct()[:300])
    names |= {str(v).strip() for v in seen if v}
    by_key = {}
    for n in sorted(names):
        by_key.setdefault(vendor_key(n), n)
    return sorted(v for k, v in by_key.items() if k)[:300]


def _status_filter(qs, status: str):
    today = timezone.localdate()
    if status == "active":
        return qs.filter(archived=False).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=today))
    if status == "expired":
        return qs.filter(archived=False, valid_to__lt=today)
    if status == "archived":
        return qs.filter(archived=True)
    return qs


def _filtered_quotes(request, org):
    f = request.GET
    status = f.get("status", "active")
    status = status if status in dict(STATUS_TABS) else "active"
    qs = Quote.objects.filter(organization=org)
    if f.get("vendor"):
        qs = qs.filter(vendor_key=f["vendor"])
    if f.get("equipment") in lanes.EQUIPMENT_CODES:
        qs = qs.filter(equipment=f["equipment"])
    q = (f.get("q") or "").strip()
    if q:
        place = lanes.resolve(q)
        cond = (Q(vendor_name__icontains=q) | Q(reference__icontains=q) | Q(origin__icontains=q)
                | Q(destination__icontains=q))
        if place:
            cond |= Q(origin_key=place.code) | Q(destination_key=place.code)
        qs = qs.filter(cond)
    return qs, status


def _query_without(request, *keys) -> str:
    params = request.GET.copy()
    for k in keys:
        params.pop(k, None)
    return params.urlencode()


def _tab_counts(org) -> dict:
    return {"quotes": _status_filter(Quote.objects.filter(organization=org), "active").count(),
            "extras": ApprovedAccessorial.objects.filter(organization=org).count(),
            "names": ChargeAlias.objects.filter(organization=org).count()}


def _ctx(request, org, tab: str, **extra) -> dict:
    return {"tab": tab, "counts": _tab_counts(org), "can_edit": has_perm(request.user, org, "approve"), **extra}


def _recheck_message(request, n: int) -> None:
    if n:
        messages.info(request, f"Checked {n} open shipment{'s' if n != 1 else ''} again with the new rates.")


def _quote_for(request, pk) -> Quote:
    org = current_org(request)
    return get_object_or_404(Quote.objects.filter(organization=org).prefetch_related("charges"), pk=pk)


# ---------------------------------------------------------------- quotes


@login_required
def quote_list(request):
    org = current_org(request)
    require(request.user, org, "view")
    base = Quote.objects.filter(organization=org)
    qs, status = _filtered_quotes(request, org)
    counts = {k: _status_filter(qs, k).count() for k, _ in STATUS_TABS}
    qs = _status_filter(qs, status).prefetch_related("charges").order_by("vendor_name", "origin", "destination",
                                                                          "-valid_from", "-id")
    page = Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))
    today = timezone.localdate()
    for quote in page:
        quote.state = quote.status(today)
        per_box = [c for c in quote.charges.all() if c.basis == QuoteCharge.Basis.CONTAINER]
        quote.per_container = sum((c.amount for c in per_box), 0) if per_box else None
        quote.n_charges = len(quote.charges.all())
    vendors = (base.values("vendor_key").annotate(n=Count("id")).order_by("vendor_key"))
    names = dict(base.values_list("vendor_key", "vendor_name"))
    return render(request, "rates/list.html", _ctx(
        request, org, "quotes", page=page, status=status,
        tabs=[(k, v, counts[k]) for k, v in STATUS_TABS], f=request.GET,
        vendors=[(v["vendor_key"], names.get(v["vendor_key"], v["vendor_key"]), v["n"]) for v in vendors],
        equipment=EQUIPMENT_FILTER, query=_query_without(request, "page"),
        tab_query=_query_without(request, "page", "status"), export_query=_query_without(request, "page"),
        any_quotes=base.exists()))


def _formset_class(extra: int):
    return inlineformset_factory(Quote, QuoteCharge, form=ChargeForm, formset=UniqueChargesFormSet, extra=extra,
                                 can_delete=True, min_num=1, validate_min=True, max_num=60)


def _render_form(request, org, form, formset, quote=None, copy_from=None):
    return render(request, "rates/quote_form.html", _ctx(
        request, org, "quotes", form=form, formset=formset, quote=quote, copy_from=copy_from,
        vendor_names=_vendor_names(org), ports=lanes.suggestions(), codes=charges.CODE_CHOICES,
        bases=QuoteCharge.Basis.choices))


@login_required
def quote_create(request):
    org = current_org(request)
    require(request.user, org, "approve")
    copy_from = None
    if request.GET.get("copy"):
        copy_from = get_object_or_404(Quote.objects.filter(organization=org).prefetch_related("charges"),
                                      pk=request.GET["copy"] if request.GET["copy"].isdigit() else 0)
    if request.method == "POST":
        form = QuoteForm(request.POST, organization=org)
        formset = ChargeFormSet(request.POST, instance=Quote(organization=org), prefix="charges")
        if form.is_valid() and formset.is_valid():
            quote = form.save(commit=False)
            quote.organization = org
            quote.created_by = quote.updated_by = request.user
            quote.save()
            formset.instance = quote
            formset.save()
            audit(org, "quote.created", quote, actor=request.user, vendor=quote.vendor_name,
                  name=quote.audit_name, reference=quote.reference, lane=quote.lane, quote=snapshot(quote),
                  **({"copied_from": copy_from.pk} if copy_from else {}))
            messages.success(request, f"Saved {quote.title} for {quote.vendor_name}.")
            _recheck_message(request, recheck(org, {quote.vendor_key}))
            return redirect("rates:detail", pk=quote.pk)
        messages.error(request, "The quote was not saved. Check the highlighted fields.")
        return _render_form(request, org, form, formset, copy_from=copy_from)
    initial = {"currency": org.home_currency, "valid_from": timezone.localdate()}
    charge_initial = []
    if copy_from:
        start = (copy_from.valid_to + timedelta(days=1)) if copy_from.valid_to else timezone.localdate()
        initial = {f: getattr(copy_from, f) for f in QuoteForm.Meta.fields}
        initial.update(valid_from=start, valid_to=None, reference="")
        charge_initial = [{"code": c.code, "description": c.description, "amount": c.amount, "basis": c.basis}
                          for c in copy_from.charges.all()]
    form = QuoteForm(initial=initial)
    formset = _formset_class(max(3, len(charge_initial) + 1))(instance=Quote(organization=org), prefix="charges",
                                                              initial=charge_initial)
    return _render_form(request, org, form, formset, copy_from=copy_from)


@login_required
def quote_edit(request, pk):
    org = current_org(request)
    require(request.user, org, "approve")
    quote = _quote_for(request, pk)
    if request.method == "POST":
        before, old_key = snapshot(quote), quote.vendor_key
        form = QuoteForm(request.POST, instance=quote, organization=org)
        formset = ChargeFormSet(request.POST, instance=quote, prefix="charges")
        if form.is_valid() and formset.is_valid():
            quote = form.save(commit=False)
            quote.updated_by = request.user
            quote.save()
            formset.save()
            quote = _quote_for(request, pk)
            diff = changes(before, snapshot(quote))
            if diff:
                audit(org, "quote.updated", quote, actor=request.user, vendor=quote.vendor_name,
                      name=quote.audit_name, reference=quote.reference, lane=quote.lane, changes=diff)
                messages.success(request, f"Saved changes to {quote.title}.")
                _recheck_message(request, recheck(org, {old_key, quote.vendor_key}))
            else:
                messages.info(request, "Nothing changed.")
            return redirect("rates:detail", pk=quote.pk)
        messages.error(request, "The changes were not saved. Check the highlighted fields.")
        return _render_form(request, org, form, formset, quote=quote)
    return _render_form(request, org, QuoteForm(instance=quote), ChargeFormSet(instance=quote, prefix="charges"),
                        quote=quote)


@login_required
def quote_detail(request, pk):
    org = current_org(request)
    require(request.user, org, "view")
    quote = _quote_for(request, pk)
    checks = list(ValidationIssue.objects.filter(organization=org, data__quote_id=quote.pk)
                  .select_related("shipment", "document").order_by("-created_at")[:20])
    events = []
    for e in AuditEvent.objects.filter(organization=org, object_type="Quote", object_id=str(quote.pk)
                                       ).select_related("actor")[:30]:
        events.append({"at": e.created_at, "who": e.actor, "text": describe_action(e.action, e.data),
                       "source": (e.data or {}).get("source", "")})
    extras = [a for a in ApprovedAccessorial.objects.filter(organization=org, vendor_key=quote.vendor_key)]
    siblings = list(Quote.objects.filter(organization=org, vendor_key=quote.vendor_key).exclude(pk=quote.pk)
                    .order_by("-valid_from", "origin", "destination"))
    same_lane = [q for q in siblings if q.origin_key == quote.origin_key and q.destination_key == quote.destination_key
                 and q.equipment == quote.equipment]
    others = (same_lane + [q for q in siblings if q not in same_lane])[:6]
    return render(request, "rates/detail.html", _ctx(
        request, org, "quotes", quote=quote, state=quote.status(), checks=checks, events=events, extras=extras,
        others=others, same_lane={q.pk for q in same_lane}, sibling_count=len(siblings), origin_port=lanes.resolve(quote.origin) if quote.origin else None,
        destination_port=lanes.resolve(quote.destination) if quote.destination else None))


@login_required
@require_POST
def quote_archive(request, pk):
    org = current_org(request)
    require(request.user, org, "approve")
    quote = _quote_for(request, pk)
    quote.archived = not quote.archived
    quote.updated_by = request.user
    quote.save(update_fields=["archived", "updated_by", "updated_at"])
    action = "quote.archived" if quote.archived else "quote.restored"
    audit(org, action, quote, actor=request.user, vendor=quote.vendor_name, name=quote.audit_name, reference=quote.reference, lane=quote.lane)
    messages.success(request, f"{'Archived' if quote.archived else 'Restored'} {quote.title}."
                     + (" It is no longer used to check invoices." if quote.archived else ""))
    _recheck_message(request, recheck(org, {quote.vendor_key}))
    return redirect("rates:detail", pk=quote.pk)


@login_required
@require_POST
def quote_delete(request, pk):
    org = current_org(request)
    require(request.user, org, "approve")
    quote = _quote_for(request, pk)
    if request.POST.get("confirm") != "delete":
        messages.error(request, "Tick the box to confirm, then delete. Archiving keeps the quote for reference.")
        return redirect("rates:detail", pk=quote.pk)
    title, key, data = quote.title, quote.vendor_key, snapshot(quote)
    audit(org, "quote.deleted", quote, actor=request.user, vendor=quote.vendor_name, name=quote.audit_name, reference=quote.reference,
          lane=quote.lane, quote=data)
    quote.delete()
    messages.success(request, f"Deleted {title}. The audit log keeps a copy of what it said.")
    _recheck_message(request, recheck(org, {key}))
    return redirect("rates:list")


# ---------------------------------------------------------------- CSV


@login_required
def quote_export(request):
    org = current_org(request)
    require(request.user, org, "view")
    qs, status = _filtered_quotes(request, org)
    qs = _status_filter(qs, status).prefetch_related("charges").order_by("vendor_name", "origin", "destination",
                                                                          "-valid_from")
    audit(org, "quote.exported", org, actor=request.user, filters=dict(request.GET.items()), quotes=qs.count())
    response = HttpResponse(csvio.export_csv(qs), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="shipmatch-quotes-{org.slug}-{timezone.localdate()}.csv"'
    return response


@login_required
def import_template(request):
    org = current_org(request)
    require(request.user, org, "view")
    response = HttpResponse(csvio.template_csv(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="shipmatch-quote-template.csv"'
    return response


@login_required
def import_quotes(request):
    org = current_org(request)
    require(request.user, org, "approve")
    result = None
    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Choose a CSV file to import.")
            return redirect("rates:import")
        if not upload.name.lower().endswith((".csv", ".txt")):
            messages.error(request, f"{upload.name} isn't a CSV file. In Excel, use Save as and choose CSV.")
            return redirect("rates:import")
        result = csvio.import_quotes(org, upload.read(), actor=request.user, filename=upload.name[:120])
        if result.ok:
            request.session.pop(IMPORT_ERRORS_KEY, None)
            messages.success(request, f"Imported {upload.name}: {len(result.created)} new "
                                      f"quote{'s' if len(result.created) != 1 else ''}, {len(result.updated)} updated "
                                      f"from {result.rows} row{'s' if result.rows != 1 else ''}.")
            keys = {q.vendor_key for q in result.created + result.updated}
            _recheck_message(request, recheck(org, keys))
            return redirect("rates:list")
        request.session[IMPORT_ERRORS_KEY] = [e.as_list() for e in result.errors[:1000]]
        messages.error(request, result.file_error or f"Nothing was imported. {len(result.errors)} problem"
                                                     f"{'s' if len(result.errors) != 1 else ''} to fix in "
                                                     f"{upload.name}, listed below.")
    return render(request, "rates/import.html", _ctx(
        request, org, "quotes", result=result, columns=csvio.COLUMNS, required=csvio.REQUIRED,
        codes=charges.CODE_CHOICES, kinds={c.code: c.kind for c in charges.CHARGES.values()},
        equipment=EQUIPMENT_FILTER))


@login_required
def import_errors(request):
    org = current_org(request)
    require(request.user, org, "approve")
    errors = request.session.get(IMPORT_ERRORS_KEY) or []
    if not errors:
        messages.info(request, "There is no error report. Import a file first.")
        return redirect("rates:import")
    response = HttpResponse(csvio.errors_csv(errors), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="shipmatch-quote-import-problems.csv"'
    return response


# ---------------------------------------------------------------- approved extra charges


@login_required
def extras(request):
    org = current_org(request)
    require(request.user, org, "view")
    rows = ApprovedAccessorial.objects.filter(organization=org).order_by("vendor_name", "code", "-valid_from")
    vendor = request.GET.get("vendor", "")
    if vendor:
        rows = rows.filter(vendor_key=vendor)
    groups: dict[str, dict] = {}
    for a in rows:
        g = groups.setdefault(a.vendor_key, {"name": a.vendor_name, "rows": []})
        g["rows"].append(a)
    vendors = sorted({(a.vendor_key, a.vendor_name) for a in ApprovedAccessorial.objects.filter(organization=org)},
                     key=lambda v: v[1].lower())
    return render(request, "rates/extras.html", _ctx(request, org, "extras", groups=list(groups.values()),
                                                     vendors=vendors, vendor=vendor, today=timezone.localdate()))


def _extra_form(request, org, instance=None):
    if request.method == "POST":
        # Snapshot first: validating a ModelForm writes the new values onto the instance.
        before = ({f: str(getattr(instance, f)) for f in AccessorialForm.base_fields} if instance else None)
        form = AccessorialForm(request.POST, instance=instance, organization=org)
        if form.is_valid():
            old_key = instance.vendor_key if instance else None
            extra = form.save(commit=False)
            extra.organization = org
            extra.updated_by = request.user
            if instance is None:
                extra.created_by = request.user
            extra.save()
            extra.refresh_from_db()   # so numbers read back the way "before" did (175 vs 175.00)
            after = {f: str(getattr(extra, f)) for f in form.fields}
            if before is None:
                audit(org, "accessorial.created", extra, actor=request.user, vendor=extra.vendor_name,
                      charge=extra.code_label, terms=extra.terms, values=after)
            else:
                diff = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
                if not diff:
                    messages.info(request, "Nothing changed.")
                    return redirect("rates:extras")
                audit(org, "accessorial.updated", extra, actor=request.user, vendor=extra.vendor_name,
                      charge=extra.code_label, terms=extra.terms, changes=diff)
            messages.success(request, f"Saved: {extra.vendor_name} may charge {extra.code_label.lower()}, "
                                      f"{extra.terms}.")
            _recheck_message(request, recheck(org, {k for k in (old_key, extra.vendor_key) if k}))
            return redirect("rates:extras")
        messages.error(request, "Not saved. Check the highlighted fields.")
    else:
        initial = {"currency": org.home_currency}
        if request.GET.get("vendor"):
            initial["vendor_name"] = request.GET["vendor"][:200]
        form = AccessorialForm(instance=instance, initial=initial if instance is None else None)
    units = {code: charges.default_unit(code) for code, _ in charges.ACCESSORIAL_CHOICES}
    return render(request, "rates/extra_form.html", _ctx(request, org, "extras", form=form, extra=instance,
                                                         vendor_names=_vendor_names(org), units=units))


@login_required
def extra_create(request):
    org = current_org(request)
    require(request.user, org, "approve")
    return _extra_form(request, org)


@login_required
def extra_edit(request, pk):
    org = current_org(request)
    require(request.user, org, "approve")
    return _extra_form(request, org, get_object_or_404(ApprovedAccessorial, pk=pk, organization=org))


@login_required
@require_POST
def extra_delete(request, pk):
    org = current_org(request)
    require(request.user, org, "approve")
    extra = get_object_or_404(ApprovedAccessorial, pk=pk, organization=org)
    key = extra.vendor_key
    audit(org, "accessorial.deleted", extra, actor=request.user, vendor=extra.vendor_name, charge=extra.code_label,
          terms=extra.terms)
    extra.delete()
    messages.success(request, f"Removed {extra.code_label.lower()} from {extra.vendor_name}'s approved extra charges.")
    _recheck_message(request, recheck(org, {key}))
    return redirect("rates:extras")


# ---------------------------------------------------------------- charge names


@login_required
def charge_names(request):
    org = current_org(request)
    require(request.user, org, "view")
    if request.method == "POST":
        require(request.user, org, "approve")
        form = AliasForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Enter a charge name and choose what it means.")
            return redirect("rates:charge_names")
        example, code = form.cleaned_data["example"], form.cleaned_data["code"]
        key = charges.alias_key(example)
        alias, created = ChargeAlias.objects.get_or_create(organization=org, key=key, defaults={
            "code": code, "example": example[:200], "source": ChargeAlias.Source.PERSON, "updated_by": request.user})
        old = None if created else alias.code
        if not created:
            alias.code, alias.source, alias.updated_by = code, ChargeAlias.Source.PERSON, request.user
            alias.example = alias.example or example[:200]
            alias.save()
        if created or old != code:
            audit(org, "charge_name.updated", alias, actor=request.user, name=key, old=charges.label(old) if old else "",
                  new=charges.label(code))
            messages.success(request, f"Invoice lines named “{key}” now count as {charges.label(code).lower()}.")
            _recheck_message(request, recheck(org))
        return redirect(f"{reverse('rates:charge_names')}?{urlencode({'test': example})}")
    test = (request.GET.get("test") or "").strip()[:200]
    tested = None
    if test:
        key = charges.alias_key(test)
        alias = ChargeAlias.objects.filter(organization=org, key=key).first()
        code = alias.code if alias else charges.match_keywords(test)
        units = charges.units_on_line(test, None, charges.default_unit(code or "other"))
        tested = {"text": test, "key": key, "code": code or "other", "label": charges.label(code or "other"),
                  "source": alias.get_source_display() if alias else ("Built-in names" if code else "Not recognized"),
                  "kind": charges.kind(code or "other"), "units": units}
    aliases = ChargeAlias.objects.filter(organization=org).select_related("updated_by").order_by("key")
    return render(request, "rates/charge_names.html", _ctx(
        request, org, "names", aliases=aliases, tested=tested, test=test, codes=charges.CODE_CHOICES,
        all_charges=list(charges.CHARGES.values())))


@login_required
@require_POST
def charge_name_delete(request, pk):
    org = current_org(request)
    require(request.user, org, "approve")
    alias = get_object_or_404(ChargeAlias, pk=pk, organization=org)
    audit(org, "charge_name.deleted", alias, actor=request.user, name=alias.key, old=charges.label(alias.code))
    alias.delete()
    messages.success(request, f"Forgot “{alias.key}”. The built-in names apply again.")
    _recheck_message(request, recheck(org))
    return redirect("rates:charge_names")


# ---------------------------------------------------------------- checking rules


@login_required
def rules(request):
    org = current_org(request)
    require(request.user, org, "view")
    current = RateSettings.for_org(org)
    if request.method == "POST":
        require(request.user, org, "manage")
        tracked = ["tolerance_percent", "tolerance_amount", "warn_no_quote", "check_unlisted_vendors", "ai_classify"]
        before = {k: str(getattr(current, k)) for k in tracked}
        form = RateSettingsForm(request.POST, instance=current)
        if form.is_valid():
            saved = form.save(commit=False)
            saved.organization = org
            saved.save()
            after = {k: str(getattr(saved, k)) for k in tracked}
            diff = {k: [before[k], after[k]] for k in tracked if before[k] != after[k]}
            if diff:
                audit(org, "rate_settings.updated", saved, actor=request.user, changes=diff)
                messages.success(request, "Checking rules saved.")
                _recheck_message(request, recheck(org))
            else:
                messages.info(request, "Nothing changed.")
            return redirect("rates:rules")
        messages.error(request, "Not saved. Check the highlighted fields.")
    else:
        form = RateSettingsForm(instance=current)
    from apps.documents.services import llm

    can_manage = has_perm(request.user, org, "manage")
    if not can_manage:
        for field in form.fields.values():
            field.disabled = True
    example = current.tolerance_for(Decimal("2400.00"))
    return render(request, "rates/rules.html", _ctx(
        request, org, "rules", form=form, settings_obj=current, can_manage=can_manage, ai_on=llm.is_enabled(),
        example=example, example_next=example + Decimal("0.01")))


@login_required
@require_POST
def recheck_all(request):
    org = current_org(request)
    require(request.user, org, "approve")
    n = recheck(org)
    audit(org, "rates.rechecked", org, actor=request.user, shipments=n)
    messages.success(request, f"Checked {n} open shipment{'s' if n != 1 else ''} against the current rates."
                     if n else "There are no open shipments to check.")
    return redirect(request.POST.get("next") if request.POST.get("next", "").startswith("/rates/") else "rates:rules")
