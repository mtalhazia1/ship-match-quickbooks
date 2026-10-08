"""Ports and lanes: turn what people type ("Shenzhen (Yantian)", "LGB", "Long Beach, CA") into
UN/LOCODEs and compare places loosely.

Matching levels, best first:
  1.0  same UN/LOCODE                       ("Yantian" and "CNYTN")
  0.8  same port area                       ("Shenzhen" and "Yantian"; "Los Angeles" and "Long Beach")
  0.7  names nearly identical (fuzzy >= 88) for ports not in the table
An empty place on a quote means "any".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

# code, name, port area (ports in one area are interchangeable for quotes), aliases
_PORTS: list[tuple[str, str, str, tuple[str, ...]]] = [
    # China
    ("CNSHA", "Shanghai", "shanghai", ("yangshan", "waigaoqiao")),
    ("CNNGB", "Ningbo", "ningbo", ("ningbo zhoushan", "beilun", "zhoushan")),
    ("CNSZX", "Shenzhen", "shenzhen", ()),
    ("CNYTN", "Yantian", "shenzhen", ("shenzhen yantian",)),
    ("CNSHK", "Shekou", "shenzhen", ("shenzhen shekou",)),
    ("CNCWN", "Chiwan", "shenzhen", ("shenzhen chiwan",)),
    ("CNNSA", "Nansha", "guangzhou", ("guangzhou nansha",)),
    ("CNCAN", "Guangzhou", "guangzhou", ("canton", "huangpu")),
    ("CNTAO", "Qingdao", "qingdao", ("tsingtao",)),
    ("CNXMN", "Xiamen", "xiamen", ("amoy",)),
    ("CNTXG", "Tianjin", "tianjin", ("xingang", "tianjin xingang")),
    ("CNDLC", "Dalian", "dalian", ()),
    ("HKHKG", "Hong Kong", "hong kong", ("hongkong", "kwai chung")),
    ("TWKHH", "Kaohsiung", "kaohsiung", ()),
    # Rest of Asia
    ("SGSIN", "Singapore", "singapore", ()),
    ("KRPUS", "Busan", "busan", ("pusan",)),
    ("JPTYO", "Tokyo", "tokyo", ()),
    ("JPYOK", "Yokohama", "tokyo", ()),
    ("VNSGN", "Ho Chi Minh City", "ho chi minh", ("saigon", "hcmc")),
    ("VNCLI", "Cat Lai", "ho chi minh", ("ho chi minh city cat lai", "hcm cat lai")),
    ("VNHPH", "Haiphong", "haiphong", ("hai phong",)),
    ("THLCH", "Laem Chabang", "laem chabang", ()),
    ("MYPKG", "Port Klang", "port klang", ("klang",)),
    ("IDTPP", "Tanjung Priok", "jakarta", ("jakarta",)),
    ("INNSA", "Nhava Sheva", "mumbai", ("jnpt", "jawaharlal nehru", "mumbai", "nhava sheva jnpt")),
    ("INMUN", "Mundra", "mundra", ()),
    ("PKKHI", "Karachi", "karachi", ()),
    ("PKBQM", "Port Qasim", "karachi", ("qasim", "muhammad bin qasim")),
    ("LKCMB", "Colombo", "colombo", ()),
    ("BDCGP", "Chittagong", "chittagong", ("chattogram",)),
    # Middle East
    ("AEJEA", "Jebel Ali", "dubai", ("dubai", "jebel ali dubai")),
    ("SAJED", "Jeddah", "jeddah", ("jeddah islamic port",)),
    ("SADMM", "Dammam", "dammam", ()),
    ("OMSLL", "Salalah", "salalah", ()),
    # Turkey and Europe
    ("TRAMB", "Ambarli", "istanbul", ("istanbul ambarli",)),
    ("TRIST", "Istanbul", "istanbul", ("haydarpasa",)),
    ("TRMER", "Mersin", "mersin", ()),
    ("TRIZM", "Izmir", "izmir", ()),
    ("NLRTM", "Rotterdam", "rotterdam", ("maasvlakte",)),
    ("BEANR", "Antwerp", "antwerp", ("antwerpen",)),
    ("DEHAM", "Hamburg", "hamburg", ()),
    ("DEBRV", "Bremerhaven", "bremerhaven", ("bremen",)),
    ("GBFXT", "Felixstowe", "felixstowe", ()),
    ("GBSOU", "Southampton", "southampton", ()),
    ("GBLGP", "London Gateway", "london", ("london",)),
    ("FRLEH", "Le Havre", "le havre", ()),
    ("ESVLC", "Valencia", "valencia", ()),
    ("ESALG", "Algeciras", "algeciras", ()),
    ("ESBCN", "Barcelona", "barcelona", ()),
    ("ITGOA", "Genoa", "genoa", ("genova",)),
    ("GRPIR", "Piraeus", "piraeus", ("athens",)),
    # North America
    ("USLGB", "Long Beach", "los angeles long beach", ("long beach ca", "lgb")),
    ("USLAX", "Los Angeles", "los angeles long beach", ("los angeles ca", "lax", "san pedro", "la lb", "la long beach")),
    ("USOAK", "Oakland", "oakland", ("oakland ca",)),
    ("USSEA", "Seattle", "seattle tacoma", ("seattle wa",)),
    ("USTIW", "Tacoma", "seattle tacoma", ("tacoma wa",)),
    ("USNYC", "New York", "new york new jersey", ("new york ny", "nyc", "ny nj")),
    ("USEWR", "Newark", "new york new jersey", ("newark nj", "port newark", "port elizabeth", "elizabeth nj")),
    ("USSAV", "Savannah", "savannah", ("savannah ga",)),
    ("USCHS", "Charleston", "charleston", ("charleston sc",)),
    ("USORF", "Norfolk", "norfolk", ("norfolk va", "virginia")),
    ("USHOU", "Houston", "houston", ("houston tx", "barbours cut", "bayport")),
    ("USMIA", "Miami", "miami", ("miami fl",)),
    ("USBAL", "Baltimore", "baltimore", ("baltimore md",)),
    ("USJAX", "Jacksonville", "jacksonville", ("jacksonville fl",)),
    ("USMOB", "Mobile", "mobile", ("mobile al",)),
    ("CAVAN", "Vancouver", "vancouver", ("vancouver bc",)),
    ("CAPRR", "Prince Rupert", "prince rupert", ()),
    ("CAMTR", "Montreal", "montreal", ()),
    ("MXZLO", "Manzanillo", "manzanillo", ()),
    # South America and Oceania
    ("BRSSZ", "Santos", "santos", ()),
    ("AUSYD", "Sydney", "sydney", ("port botany",)),
    ("AUMEL", "Melbourne", "melbourne", ()),
]

_NOISE = re.compile(r"\b(port|of|the|terminal|harbou?r|container|ctr|city|international|intl|"
                    r"usa|us|china|prc|vietnam|turkey|turkiye|india|uae)\b")
_US_STATE = re.compile(r",\s*[A-Z]{2}\b")


def _strip_noise(s: str) -> str:
    return re.sub(r"\s+", " ", _NOISE.sub(" ", s)).strip()


def _clean(text: str) -> str:
    s = (text or "").strip()
    s = _US_STATE.sub(lambda m: " " + m.group(0)[1:].strip().lower(), s)  # "Long Beach, CA" -> "Long Beach ca"
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


@dataclass(frozen=True)
class Port:
    code: str
    name: str
    area: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.code})"


PORTS: dict[str, Port] = {}
_BY_NAME: dict[str, Port] = {}
for _code, _name, _area, _aliases in _PORTS:
    _p = Port(_code, _name, _area)
    PORTS[_code] = _p
    for _n in (_name, *_aliases):
        _BY_NAME.setdefault(_clean(_n), _p)
        if _strip_noise(_clean(_n)):
            _BY_NAME.setdefault(_strip_noise(_clean(_n)), _p)

_CODE_RE = re.compile(r"\b([A-Z]{2})\s?([A-Z2-9]{3})\b")


def resolve(text: str) -> Port | None:
    """The port a place name or UN/LOCODE refers to, or None if not in the table."""
    if not text or not text.strip():
        return None
    raw = text.strip()
    compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if len(compact) == 5 and compact in PORTS:
        return PORTS[compact]
    for m in _CODE_RE.finditer(raw):  # case-sensitive: UN/LOCODEs are written in capitals
        code = m.group(1) + m.group(2)
        if code in PORTS:
            return PORTS[code]
    c = _clean(raw)
    if c in _BY_NAME:
        return _BY_NAME[c]
    stripped = _strip_noise(c)
    if stripped in _BY_NAME:
        return _BY_NAME[stripped]
    # "Shenzhen (Yantian)": the part in brackets is the specific port, the rest the city
    inner = re.findall(r"\(([^)]+)\)", raw)
    for part in inner:
        p = resolve(part)
        if p:
            return p
    outer = re.sub(r"\([^)]*\)", " ", raw)
    if inner and outer.strip():
        p = resolve(outer)
        if p:
            return p
    # A known name inside a longer text ("Port of Long Beach, CA, USA")
    for size in (3, 2, 1):
        words = stripped.split()
        for i in range(len(words) - size + 1):
            p = _BY_NAME.get(" ".join(words[i:i + size]))
            if p:
                return p
    return None


def place_key(text: str) -> str:
    """Stored next to a quote's origin/destination: the UN/LOCODE when known, else cleaned text."""
    p = resolve(text)
    return p.code if p else _strip_noise(_clean(text))[:60]


def describe(text: str) -> str:
    p = resolve(text)
    return p.label if p else (text or "").strip()


def match_score(quote_place: str, actual_place: str | None) -> float | None:
    """How well a quote's place fits the shipment's place.

    Returns 0.5 for a quote with no place (any), None when they don't match, or the level above.
    The caller treats an unknown shipment place separately.
    """
    if not (quote_place or "").strip():
        return 0.5
    if not (actual_place or "").strip():
        return None
    a, b = resolve(quote_place), resolve(actual_place)
    if a and b:
        if a.code == b.code:
            return 1.0
        if a.area == b.area:
            return 0.8
        return None
    # At least one side isn't a known port: compare names.
    ca, cb = _strip_noise(_clean(quote_place)), _strip_noise(_clean(actual_place))
    if a and not b:
        ca = _clean(a.name)
    if b and not a:
        cb = _clean(b.name)
    if ca and cb and (ca == cb or fuzz.token_set_ratio(ca, cb) >= 88):
        return 0.7
    return None


def suggestions() -> list[str]:
    """Port names for form autocompletion."""
    return [p.label for p in PORTS.values()]


# --------------------------------------------------------------------------- equipment

EQUIPMENT_CHOICES = [
    ("", "Any equipment"),
    ("20GP", "20' standard (20GP)"),
    ("40GP", "40' standard (40GP)"),
    ("40HC", "40' high cube (40HC)"),
    ("45HC", "45' high cube (45HC)"),
    ("20RF", "20' reefer (20RF)"),
    ("40RF", "40' reefer (40RF)"),
    ("LCL", "Less than container load (LCL)"),
]
EQUIPMENT_CODES = {c for c, _ in EQUIPMENT_CHOICES if c}

_EQUIPMENT_ALIASES = {
    "20GP": "20GP", "20DV": "20GP", "20DC": "20GP", "20ST": "20GP", "20SD": "20GP", "22G1": "20GP", "20": "20GP",
    "40GP": "40GP", "40DV": "40GP", "40DC": "40GP", "40ST": "40GP", "40SD": "40GP", "42G1": "40GP", "40": "40GP",
    "40HC": "40HC", "40HQ": "40HC", "40HCDV": "40HC", "45G1": "40HC", "40HIGHCUBE": "40HC",
    "45HC": "45HC", "45HQ": "45HC", "L5G1": "45HC", "45": "45HC",
    "20RF": "20RF", "20RE": "20RF", "20REEFER": "20RF", "22R1": "20RF",
    "40RF": "40RF", "40RH": "40RF", "40RQ": "40RF", "40REEFER": "40RF", "40HR": "40RF", "45R1": "40RF",
    "LCL": "LCL",
}
# Equipment written on a document: 40HC, 40'HC, 40 HQ, 20' DV, 40 REEFER, 45G1, LCL ...
_EQUIPMENT_RE = re.compile(r"\b(20|40|45)\s*'?\s*(GP|DV|DC|ST|SD|HC|HQ|RF|RE|RH|RQ|HR|REEFER|HIGH\s*CUBE)\b"
                           r"|\b(22G1|42G1|45G1|L5G1|22R1|45R1)\b|\b(LCL)\b", re.I)


def normalize_equipment(text: str) -> str | None:
    """'40HQ' -> '40HC', "20' DV" -> '20GP', 'reefer 40' -> '40RF'. None when not recognized."""
    if not text or not text.strip():
        return None
    s = re.sub(r"[^A-Z0-9]", "", text.upper())
    if s in _EQUIPMENT_ALIASES:
        return _EQUIPMENT_ALIASES[s]
    m = _EQUIPMENT_RE.search(text)
    if m:
        return _EQUIPMENT_ALIASES.get(re.sub(r"[^A-Z0-9]", "", m.group(0).upper()))
    if "REEFER" in s:
        return "40RF" if "40" in s else "20RF" if "20" in s else None
    return None


def equipment_in_text(text: str) -> dict[str, int]:
    """Equipment types written in a document's text, with how often each appears."""
    found: dict[str, int] = {}
    for m in _EQUIPMENT_RE.finditer(text or ""):
        code = _EQUIPMENT_ALIASES.get(re.sub(r"[^A-Z0-9]", "", m.group(0).upper()))
        if code:
            found[code] = found.get(code, 0) + 1
    return found
