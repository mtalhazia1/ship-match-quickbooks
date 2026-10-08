"""Free time: when each container must be picked up (demurrage) and returned empty (detention).

Rules, also in the README ("Customs entries and free time"):
  * Dates on an arrival notice are local dates at the port. "Today" is the organization's date in its time zone.
  * Demurrage free time starts the day after discharge (or after the actual arrival; when neither is printed, after
    the ETA, marked as estimated). Day 1 is the first counted day after that; the last free day is day N.
  * Detention free time starts the day after the container is picked up (a person enters that date), unless the
    notice says detention counts from discharge.
  * Saturdays, Sundays and the organization's holidays are not counted, unless the notice says the days are calendar
    days (or the organization chose to count them when a notice doesn't say). The rule used is shown per container.
  * A last free day printed on the notice always wins over a computed one. Several notices for one shipment: the
    latest notice wins for each date it prints.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from apps.documents.models import Document
from apps.documents.services.normalize import norm_ref, parse_date

from ..models import ContainerFreeTime, CustomsSettings

log = logging.getLogger(__name__)
NOTICE = Document.DocType.ARRIVAL_NOTICE

CALENDAR = re.compile(r"calendar|including (?:saturdays|weekends)|incl\.? weekends|7 days", re.I)
WORKING = re.compile(r"working|business|excluding|excl\.|not counted|monday", re.I)
FROM_DISCHARGE = re.compile(r"detention[^.]*from (?:the )?(?:date of )?discharge", re.I)


# --------------------------------------------------------------------------- dates


def org_zone(org) -> ZoneInfo:
    try:
        return ZoneInfo(org.timezone or "UTC")
    except (ValueError, KeyError, OSError):
        return ZoneInfo("UTC")


def org_now(org, now: datetime | None = None) -> datetime:
    return timezone.localtime(now or timezone.now(), org_zone(org))


def org_today(org, now: datetime | None = None) -> date:
    return org_now(org, now).date()


def is_free_day(day: date, count_weekends: bool, holidays: set[date]) -> bool:
    return count_weekends or (day.weekday() < 5 and day not in holidays)


def last_free_day(start: date, free_days: int, count_weekends: bool, holidays: set[date] = frozenset()) -> date:
    """The last free day when free time starts the day after `start`: the free_days-th counted day.
    0 free days: charges start the day after `start`, so `start` is the last free day."""
    day, counted = start, 0
    while counted < free_days:
        day += timedelta(days=1)
        if is_free_day(day, count_weekends, holidays):
            counted += 1
    return day


def counts_weekends(basis: str | None, default: bool) -> tuple[bool, str]:
    """(count weekends and holidays?, why) from the notice's wording, else the organization's default."""
    text = basis or ""
    if CALENDAR.search(text):
        return True, "calendar days, as the notice says"
    if WORKING.search(text):
        return False, "working days (weekends and holidays not counted), as the notice says"
    if default:
        return True, "calendar days (the notice doesn't say; organization setting)"
    return False, "working days (the notice doesn't say, so weekends and holidays are not counted)"


# --------------------------------------------------------------------------- plan from notices


@dataclass
class Plan:
    container: str
    document: Document
    carrier: str = ""
    terminal: str = ""
    eta: date | None = None
    discharge: date | None = None
    discharge_estimated: bool = False
    dem_days: int | None = None
    det_days: int | None = None
    basis: str | None = None
    det_from_discharge: bool = False
    lfd_dem: date | None = None
    lfd_det: date | None = None
    seen: set = field(default_factory=set)


def _int(v) -> int | None:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 365 else None


def plans(notices: list[Document]) -> dict[str, Plan]:
    """Per container, the dates from the shipment's arrival notices, oldest notice first so the latest wins."""
    out: dict[str, Plan] = {}
    for doc in sorted(notices, key=lambda d: (d.received_at, d.pk)):
        data = doc.data()
        rows = {norm_ref(r.get("container_number")): r for r in data.get("container_dates") or []
                if isinstance(r, dict) and r.get("container_number")}
        containers = list(dict.fromkeys([norm_ref(c) for c in data.get("container_numbers") or [] if c] + list(rows)))
        header_discharge = parse_date(data.get("discharge_date")) or parse_date(data.get("actual_arrival_date"))
        eta = parse_date(data.get("estimated_arrival_date"))
        for c in containers:
            if not c:
                continue
            row = rows.get(c, {})
            p = out.get(c) or Plan(c, doc)
            p.document = doc
            p.carrier = str(data.get("carrier_name") or p.carrier)[:200]
            p.terminal = str(data.get("terminal") or p.terminal)[:200]
            p.eta = eta or p.eta
            discharge = parse_date(row.get("discharge_date")) or header_discharge
            if discharge:
                p.discharge, p.discharge_estimated = discharge, False
            elif p.discharge is None and eta:
                p.discharge, p.discharge_estimated = eta, True
            elif p.discharge_estimated and eta:
                p.discharge = eta
            p.dem_days = _int(data.get("demurrage_free_days")) if data.get("demurrage_free_days") not in (None, "") \
                else p.dem_days
            p.det_days = _int(data.get("detention_free_days")) if data.get("detention_free_days") not in (None, "") \
                else p.det_days
            p.basis = data.get("free_time_basis") or p.basis
            p.det_from_discharge = bool(FROM_DISCHARGE.search(doc.text or "")) or p.det_from_discharge
            p.lfd_dem = parse_date(row.get("demurrage_last_free_day")) or parse_date(
                data.get("demurrage_last_free_day")) or p.lfd_dem
            p.lfd_det = parse_date(row.get("detention_last_free_day")) or parse_date(
                data.get("detention_last_free_day")) or p.lfd_det
            out[c] = p
    return out


def recompute(row: ContainerFreeTime, cfg: CustomsSettings | None = None, plan: Plan | None = None) -> None:
    """Fill the computed last free days of a row (printed ones are kept)."""
    cfg = cfg or CustomsSettings.for_org(row.organization)
    holidays = cfg.holiday_dates()
    if plan is not None:
        row.count_weekends, why = counts_weekends(plan.basis, cfg.count_weekends)
        row.basis_note = why[:200]
    if not row.lfd_demurrage_printed:
        row.lfd_demurrage = (last_free_day(row.discharge_date, row.demurrage_free_days, row.count_weekends, holidays)
                             if row.discharge_date and row.demurrage_free_days is not None else None)
    if not row.lfd_detention_printed:
        start = row.picked_up_on if row.detention_from == ContainerFreeTime.DetentionFrom.PICKUP else row.discharge_date
        row.lfd_detention = (last_free_day(start, row.detention_free_days, row.count_weekends, holidays)
                             if start and row.detention_free_days is not None else None)


def sync_shipment(shipment) -> list[ContainerFreeTime]:
    """Bring the shipment's free time rows in line with its arrival notices. Rows a person entered dates on are
    kept even when their notice left the shipment; other rows without a notice are removed."""
    notices = list(Document.objects.filter(match__shipment=shipment, doc_type=NOTICE).prefetch_related("fields"))
    existing = {r.container_number: r for r in ContainerFreeTime.objects.filter(shipment=shipment)}
    if not notices and not existing:
        return []
    cfg = CustomsSettings.for_org(shipment.organization)
    found = plans(notices)
    kept = []
    with transaction.atomic():
        for c, p in found.items():
            row = existing.pop(c, None) or ContainerFreeTime(organization=shipment.organization, shipment=shipment,
                                                             container_number=c)
            row.document = p.document
            row.carrier_name, row.terminal = p.carrier, p.terminal
            row.eta, row.discharge_date, row.discharge_estimated = p.eta, p.discharge, p.discharge_estimated
            row.demurrage_free_days, row.detention_free_days = p.dem_days, p.det_days
            row.detention_from = (ContainerFreeTime.DetentionFrom.DISCHARGE if p.det_from_discharge
                                  else ContainerFreeTime.DetentionFrom.PICKUP)
            row.lfd_demurrage_printed = p.lfd_dem is not None
            row.lfd_detention_printed = p.lfd_det is not None
            if p.lfd_dem:
                row.lfd_demurrage = p.lfd_dem
            if p.lfd_det:
                row.lfd_detention = p.lfd_det
            recompute(row, cfg, p)
            row.save()
            kept.append(row)
        for row in existing.values():  # no notice for this container in the shipment any more
            if row.has_manual_dates:
                row.document = None
                row.save(update_fields=["document", "updated_at"])
                kept.append(row)
            else:
                row.delete()
    return kept


def recompute_org(org) -> int:
    """After the organization's free time settings change: recompute every open row."""
    cfg = CustomsSettings.for_org(org)
    n = 0
    for row in ContainerFreeTime.objects.filter(organization=org, returned_on__isnull=True).select_related("document"):
        p = plans([row.document]).get(row.container_number) if row.document_id else None
        recompute(row, cfg, p)
        row.save()
        n += 1
    return n


# --------------------------------------------------------------------------- status for people


@dataclass
class Status:
    stage: str                  # demurrage | detention | closed
    tone: str                   # ok | soon | today | late | unknown | closed
    text: str                   # "3 days left", "Last free day today", "4 days late"
    lfd: date | None
    days_left: int | None
    days_late: int = 0
    daily: Decimal | None = None
    daily_currency: str = ""
    daily_source: str = ""
    accrued: Decimal | None = None

    @property
    def needs_action(self) -> bool:
        return self.tone in {"soon", "today", "late"}


def status_for(row: ContainerFreeTime, today: date, lead_days: int, with_cost: bool = True) -> Status:
    stage = row.stage
    if stage == "closed":
        return Status("closed", "closed", f"Returned empty {row.returned_on.day} {row.returned_on:%b}", None, None)
    lfd = row.lfd_detention if stage == "detention" else row.lfd_demurrage
    if lfd is None:
        what = "empty return date" if stage == "detention" else "last free day"
        return Status(stage, "unknown", f"No {what} on file", None, None)
    days_left = (lfd - today).days
    if days_left > 0:
        tone = "soon" if days_left <= lead_days else "ok"
        st = Status(stage, tone, f"{days_left} day{'s' if days_left != 1 else ''} left", lfd, days_left)
    elif days_left == 0:
        st = Status(stage, "today", "Last free day today", lfd, 0)
    else:
        late = -days_left
        what = "Detention" if stage == "detention" else "Demurrage"
        st = Status(stage, "late", f"{what} for {late} day{'s' if late != 1 else ''}", lfd, days_left, late)
    if with_cost:
        rate = daily_rate(row, stage, lfd)
        if rate:
            st.daily, st.daily_currency, st.daily_source = rate
            if st.days_late:
                st.accrued = (st.daily * st.days_late).quantize(Decimal("0.01"))
    return st


def _billing_vendors(row: ContainerFreeTime) -> list[str]:
    """Who may bill the days: the carrier on the notice, then the vendors of the shipment's freight invoices
    (forwarders and truckers pass demurrage and detention on)."""
    names = [row.carrier_name] if row.carrier_name else []
    invoices = Document.objects.filter(match__shipment_id=row.shipment_id, doc_type=Document.DocType.FREIGHT_INVOICE,
                                       fields__name="vendor_name").values_list("fields__value", flat=True)
    names += [str(v) for v in invoices if v]
    return list(dict.fromkeys(names))


def daily_rate(row: ContainerFreeTime, stage: str, day: date | None = None) -> tuple[Decimal, str, str] | None:
    """Estimated charge per day once free time is over, from a billing vendor's approved extra charges in Rates
    (demurrage or storage; detention or per diem), else a per-day line of a current quote. None when Rates has
    nothing for them (or isn't installed): the screens then say no rate is on file."""
    from django.apps import apps as django_apps

    if not django_apps.is_installed("apps.rates"):
        return None
    try:
        from apps.accounting.models import vendor_key
        from apps.rates.models import ApprovedAccessorial, QuoteCharge

        codes = ["demurrage", "storage"] if stage == "demurrage" else ["detention", "per_diem"]
        for name in _billing_vendors(row):
            vk = vendor_key(name)
            for code in codes:
                for rule in ApprovedAccessorial.objects.filter(
                        organization=row.organization, vendor_key=vk, code=code, unit=ApprovedAccessorial.Unit.DAY,
                        max_per_unit__isnull=False).order_by("-valid_from", "-id"):
                    if rule.is_valid_on(day):
                        return (rule.max_per_unit, rule.currency,
                                f"approved {rule.code_label.lower()} rate of {rule.vendor_name}")
            for code in codes:
                for qc in (QuoteCharge.objects.filter(quote__organization=row.organization, quote__vendor_key=vk,
                                                      quote__archived=False, code=code, basis=QuoteCharge.Basis.DAY)
                           .select_related("quote").order_by("-quote__valid_from")):
                    if day is None or qc.quote.is_valid_on(day):
                        return qc.amount, qc.quote.currency, f"{qc.quote.title} of {qc.quote.vendor_name}"
    except Exception:  # rates are a nice-to-have here; never break the free time screen
        log.exception("Could not look up the daily rate for container %s", row.container_number)
    return None


def rows_needing_action(org, today: date | None = None, limit: int | None = None):
    """Open containers whose last free day is within the lead days or passed, most urgent first."""
    cfg = CustomsSettings.for_org(org)
    today = today or org_today(org)
    out = []
    for row in (ContainerFreeTime.objects.filter(organization=org, returned_on__isnull=True)
                .select_related("shipment")):
        st = status_for(row, today, cfg.lfd_alert_days, with_cost=False)
        if st.needs_action:
            out.append((row, st))
    out.sort(key=lambda rs: (rs[1].days_left, rs[0].container_number))
    out = out[:limit] if limit else out
    return [(row, status_for(row, today, cfg.lfd_alert_days)) for row, _ in out]
