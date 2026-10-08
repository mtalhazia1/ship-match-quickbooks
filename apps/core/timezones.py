"""The time zones offered in pickers.

`zoneinfo` knows about 500+ names, including every deprecated alias (Asia/Calcutta, America/Buenos_Aires, the
Australia/ACT family) next to its modern name, which made the pickers long and confusing. Pickers offer the
canonical names from the tz database's own list (zone1970.tab); any name already saved, even a deprecated one,
is still accepted and shown so an existing setting never disappears."""
from __future__ import annotations

import functools
import importlib.resources
import zoneinfo


@functools.lru_cache(maxsize=1)
def _canonical() -> frozenset[str]:
    try:
        text = (importlib.resources.files("tzdata.zoneinfo") / "zone1970.tab").read_text(encoding="utf-8")
        names = {line.split("\t")[2] for line in text.splitlines() if line and not line.startswith("#")
                 and len(line.split("\t")) > 2}
    except Exception:   # tzdata not installed (zoneinfo uses the system database): fall back to everything
        return frozenset()
    return frozenset(names & zoneinfo.available_timezones())


def choices(*keep: str) -> list[str]:
    """Sorted zone names for a picker, plus any names in `keep` (the values already saved)."""
    pool = _canonical() or {z for z in zoneinfo.available_timezones()
                            if "/" in z and not z.startswith(("Etc/", "SystemV/"))}
    zones = {z for z in pool if "/" in z}
    zones.update(k for k in keep if k and k in zoneinfo.available_timezones())
    return sorted(zones)
