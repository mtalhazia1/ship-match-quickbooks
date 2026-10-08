"""Charge names on freight invoices, mapped to canonical charge codes.

Vendors write the same charge many ways ("THC", "Terminal Handling Charge", "DTHC 40HC"). Quotes
and checks work on canonical codes, so a quote line `thc_destination` can be compared with an
invoice line "Terminal handling (dest)".

Lookup order for one description:
  1. names this organization taught ShipMatch (ChargeAlias rows, set by a person or saved from AI),
  2. the keyword table below (first matching pattern wins; specific patterns come first),
  3. optional AI classification through `llm.structured_call` for lines still unknown; the answer
     is saved as a ChargeAlias so each new name is sent to the AI once,
  4. "other".

Conventions (documented in the README):
  * A plain "THC" or "terminal handling" line is destination THC: on an importer's invoice the
    origin THC is labelled as such ("OTHC", "origin THC", "export THC").
  * Ocean bunker, low-sulphur and emissions surcharges (BAF, LSS, ETS) share the `baf` code; a
    trucking "fuel surcharge" is `fuel_surcharge`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

BASE, ACCESSORIAL = "base", "accessorial"


@dataclass(frozen=True)
class Charge:
    code: str
    label: str
    kind: str          # base | accessorial
    unit: str = "each"  # default unit for caps: day | hour | each


CHARGES: dict[str, Charge] = {c.code: c for c in [
    Charge("ocean_freight", "Ocean freight", BASE),
    Charge("baf", "Bunker or fuel adjustment (BAF, LSS, ETS)", BASE),
    Charge("caf", "Currency adjustment (CAF)", BASE),
    Charge("thc_origin", "Terminal handling at origin", BASE),
    Charge("thc_destination", "Terminal handling at destination", BASE),
    Charge("documentation", "Documentation fee", BASE),
    Charge("bl_fee", "Bill of lading fee", BASE),
    Charge("security_filing", "Security filing (ISF, AMS, ENS)", BASE),
    Charge("customs_clearance", "Customs clearance", BASE),
    Charge("trucking", "Trucking or drayage", BASE),
    Charge("chassis", "Chassis", BASE),
    Charge("fuel_surcharge", "Trucking fuel surcharge", BASE),
    Charge("insurance", "Cargo insurance", BASE),
    Charge("detention", "Detention", ACCESSORIAL, "day"),
    Charge("demurrage", "Demurrage", ACCESSORIAL, "day"),
    Charge("storage", "Storage", ACCESSORIAL, "day"),
    Charge("per_diem", "Per diem", ACCESSORIAL, "day"),
    Charge("waiting_time", "Waiting time", ACCESSORIAL, "hour"),
    Charge("redelivery", "Re-delivery or dry run", ACCESSORIAL),
    Charge("exam", "Customs exam or inspection", ACCESSORIAL),
    Charge("congestion", "Congestion or peak season surcharge", ACCESSORIAL),
    Charge("admin_fee", "Admin fee", ACCESSORIAL),
    Charge("pre_pull", "Pre-pull", ACCESSORIAL),
    Charge("chassis_split", "Chassis split", ACCESSORIAL),
    Charge("overweight", "Overweight surcharge", ACCESSORIAL),
    Charge("hazmat", "Hazardous cargo surcharge", ACCESSORIAL),
    Charge("other", "Other charge", ACCESSORIAL),
]}

CODE_CHOICES = [(c.code, c.label) for c in CHARGES.values()]
ACCESSORIAL_CODES = {c.code for c in CHARGES.values() if c.kind == ACCESSORIAL}
ACCESSORIAL_CHOICES = [(c.code, c.label) for c in CHARGES.values() if c.kind == ACCESSORIAL]

# First match wins, so specific names come before general ones ("chassis split" before "chassis",
# "re-delivery" before "delivery", "customs exam" before "customs", "origin THC" before "THC").
# Patterns run on normalized text: lower case, punctuation turned into spaces ("B/L" -> "b l").
_PATTERNS: list[tuple[str, str]] = [
    ("chassis_split", r"\bchassis split|\bsplit chassis\b"),
    ("pre_pull", r"\bpre ?pull"),
    ("redelivery", r"\bre ?deliver|\bdry run\b|\bfailed delivery\b"),
    ("waiting_time", r"\bwait(ing)?\b|\bdriver (detention|wait)|\btruck detention\b|\blive (un)?load"),
    ("per_diem", r"\bper ?diem\b"),
    ("demurrage", r"\bdemurrage\b|\bdem\b|\bd (and|n) d\b"),
    ("detention", r"\bdetention\b|\bdet\b|\blate return\b"),
    ("storage", r"\bstorage\b|\bwarehousing\b"),
    ("exam", r"\bexam(ination)?s?\b|\binspection\b|\bcet\b|\bvacis\b|\bx ?ray\b|\btailgate\b|\bnii\b"),
    ("congestion", r"\bcongestion\b|\bpeak season\b|\bpss\b|\bgri\b|\bgeneral rate increase\b"),
    ("overweight", r"\boverweight\b|\bheavy ?weight\b|\btri ?axle\b"),
    ("hazmat", r"\bhazmat\b|\bhazardous\b|\bdangerous goods\b|\bdg (surcharge|fee)\b|\bimo\b|\bimdg\b"),
    ("thc_origin", r"\bothc\b|\borigin (terminal|thc|handling)|\b(terminal handling|thc)( charges?)?( at)? origin\b"
                   r"|\bexport (thc|terminal)"),
    ("thc_destination", r"\bdthc\b|\bthd\b|\bthc\b|\bterminal handling\b|\bterminal (fee|charge)"),
    ("baf", r"\bbaf\b|\bbunker\b|\blss\b|\blow sul(ph|f)ur\b|\bebs\b|\bets\b|\bemissions?\b|\bfuel adjustment\b"),
    ("caf", r"\bcaf\b|\bcurrency adjustment\b"),
    ("fuel_surcharge", r"\bfuel\b|\bfsc\b"),
    ("security_filing", r"\bisf\b|\bams\b|\bens\b|\baci\b|\bsecurity filing\b|\badvance (manifest|filing)\b|\b10 2\b"),
    ("bl_fee", r"\bb l (fee|issu|charge)|\bbill of lading (fee|charge)|\bobl\b|\btelex\b|\bexpress release\b"
               r"|\b[hm]bl fee"),
    ("documentation", r"\bdoc(s|ument(s|ation)?)?\b|\bedi\b"),
    ("customs_clearance", r"\bcustoms\b|\bbroker(age)?\b|\bclearance\b|\bentry (fee|filing)\b|\bbond\b"),
    ("chassis", r"\bchassis\b"),
    ("trucking", r"\bdrayage\b|\bdray\b|\btrucking\b|\bcartage\b|\bhaulage\b|\bdelivery\b|\binland\b"
                 r"|\bline ?haul\b|\bpick ?up\b|\btransport(ation)?\b|\bport to (warehouse|door)\b"),
    ("insurance", r"\binsurance\b"),
    ("admin_fee", r"\badmin(istration|istrative)?\b|\bprocessing fee\b|\bhandling fee\b|\bservice fee\b"),
    ("ocean_freight", r"\bocean\b|\bsea freight\b|\bo f\b|\bbasic freight\b|\bbase rate\b|\ball in\b|\bfreight\b"),
]
PATTERNS = [(code, re.compile(rx)) for code, rx in _PATTERNS]

_UNIT_WORDS = re.compile(r"\b(days?|hrs?|hours?|x|per|each|usd|eur|gbp|cny|aed|at|qty|units?|containers?|boxe?s?|"
                         r"20gp|40gp|40hc|45hc|20rf|40rf|20|40|45|hc|gp|dv)\b")


def normalize(text: str) -> str:
    """Lower case, '&' as 'and', punctuation as spaces, single spaces."""
    s = (text or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9+]+", " ", s).replace("+", " ")
    return re.sub(r"\s+", " ", s).strip()


def alias_key(text: str) -> str:
    """Key for remembering a charge name: normalized, without amounts, counts and units."""
    s = re.sub(r"\d+(?:[.,]\d+)*", " ", normalize(text))
    s = _UNIT_WORDS.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()[:200]


def label(code: str) -> str:
    c = CHARGES.get(code)
    return c.label if c else (code or "").replace("_", " ").capitalize()


def kind(code: str) -> str:
    c = CHARGES.get(code)
    return c.kind if c else ACCESSORIAL


def is_accessorial(code: str) -> bool:
    return kind(code) == ACCESSORIAL


def default_unit(code: str) -> str:
    c = CHARGES.get(code)
    return c.unit if c else "each"


def match_keywords(text: str) -> str | None:
    s = normalize(text)
    if not s:
        return None
    for code, rx in PATTERNS:
        if rx.search(s):
            return code
    return None


def code_for(text: str) -> str | None:
    """A canonical code from a code, a label or a charge name (used for CSV import and forms)."""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.lower() in CHARGES:
        return raw.lower()
    by_label = {normalize(c.label): c.code for c in CHARGES.values()}
    if normalize(raw) in by_label:
        return by_label[normalize(raw)]
    return match_keywords(raw)


# --------------------------------------------------------------------------- classification


def classify(description: str, org=None, use_ai: bool = True) -> str:
    return classify_many([description], org=org, use_ai=use_ai).get(description, "other")


def classify_many(descriptions: list[str], org=None, use_ai: bool = True) -> dict[str, str]:
    """Map each description to a canonical code. Never raises; unknown names become "other"."""
    from .models import ChargeAlias

    out: dict[str, str] = {}
    todo = [d for d in dict.fromkeys(descriptions) if d is not None]
    learned = {}
    if org is not None and todo:
        keys = {alias_key(d) for d in todo}
        learned = dict(ChargeAlias.objects.filter(organization=org, key__in=keys).values_list("key", "code"))
    unknown = []
    for d in todo:
        code = learned.get(alias_key(d)) or match_keywords(d)
        if code:
            out[d] = code
        else:
            unknown.append(d)
    if unknown and use_ai and org is not None and _ai_allowed(org):
        out.update(_classify_with_ai(org, unknown))
    for d in todo:
        out.setdefault(d, "other")
    return out


def _ai_allowed(org) -> bool:
    from apps.documents.services import llm

    from .models import RateSettings

    return bool(settings.RATE_AI_CLASSIFY and llm.is_enabled() and RateSettings.for_org(org).ai_classify)


AI_SYSTEM = (
    "You classify charge lines from freight and logistics invoices into fixed charge codes. "
    "Answer with the single best code for each line. Use 'other' when no code fits; never guess a "
    "specific code for a line you don't recognize."
)


def ai_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "Index of the line in the list"},
                        "code": {"type": "string", "enum": list(CHARGES)},
                    },
                    "required": ["index", "code"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["lines"],
        "additionalProperties": False,
    }


def _classify_with_ai(org, descriptions: list[str]) -> dict[str, str]:
    from apps.documents.services import llm

    from .models import ChargeAlias

    failed_key = f"rates:ai-classify-failed:{org.pk}"
    if cache.get(failed_key):  # the provider failed recently; don't slow every validation down
        return {}
    codes = "\n".join(f"- {c.code}: {c.label}" for c in CHARGES.values())
    lines = "\n".join(f"{i}. {d[:200]}" for i, d in enumerate(descriptions))
    user = f"Charge codes:\n{codes}\n\nInvoice lines:\n{lines}"
    try:
        answer = llm.structured_call(AI_SYSTEM, user, ai_schema(), name="charge_codes", purpose="classify_charges",
                                     timeout=30.0)
    except Exception as e:  # LLMError, network, bad JSON: fall back to "other" and try again later
        log.warning("AI charge classification failed: %s", e)
        cache.set(failed_key, 1, 3600)
        return {}
    out = {}
    for row in (answer or {}).get("lines") or []:
        try:
            idx, code = int(row.get("index")), str(row.get("code"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= idx < len(descriptions) and code in CHARGES:
            d = descriptions[idx]
            out[d] = code
            key = alias_key(d)
            if key:
                ChargeAlias.objects.get_or_create(organization=org, key=key,
                                                  defaults={"code": code, "example": d[:200],
                                                            "source": ChargeAlias.Source.AI})
    return out


# --------------------------------------------------------------------------- quantities on a line

_DAYS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:calendar\s+|working\s+|chargeable\s+|billable\s+)?(days?|d)\b", re.I)
_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h)\b", re.I)
_TIMES = re.compile(r"(?:\bx\s*(\d+(?:\.\d+)?)\b(?!\s*(?:days?|hrs?|hours?))|\b(\d+(?:\.\d+)?)\s*x\b)", re.I)
_FREE = re.compile(r"(\d+)\s*(?:days?\s*)?free|free\s*(?:time|days?)?\s*:?\s*(\d+)", re.I)
_CHARGEABLE = re.compile(r"\b(chargeable|billable|excess|beyond free|after free|over free)\b", re.I)


@dataclass
class LineUnits:
    units: float | None = None   # days, hours or occurrences printed on the line
    unit: str = ""               # day | hour | each
    free_on_line: int | None = None
    already_chargeable: bool = False


def units_on_line(description: str, quantity=None, unit: str = "each") -> LineUnits:
    """How many days / hours / occurrences a charge line bills.

    The quantity column wins when present. Otherwise the description is read: "6 days",
    "3 hrs", "x 2". A line that says its days are chargeable ("4 chargeable days", "after free
    time") is marked so free days are not subtracted twice.
    """
    text = description or ""
    out = LineUnits(unit=unit, already_chargeable=bool(_CHARGEABLE.search(text)))
    free = _FREE.search(text)
    if free:
        out.free_on_line = int(free.group(1) or free.group(2))
    try:
        q = float(quantity) if quantity not in (None, "") else None
    except (TypeError, ValueError):
        q = None
    if q is not None and q > 0:
        out.units = q
        return out
    rx = {"day": _DAYS, "hour": _HOURS}.get(unit)
    if rx:
        cleaned = _FREE.sub(" ", text)
        m = rx.search(cleaned)
        if m:
            out.units = float(m.group(1))
            return out
    m = _TIMES.search(text)
    if m:
        out.units = float(m.group(1) or m.group(2))
    elif unit == "each":
        out.units = 1.0
    return out
