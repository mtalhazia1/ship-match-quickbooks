"""How accurate is the automatic reading on real documents? Measured from what reviewers changed.

For every document in a reviewed shipment (approved or posted), each field the machine read is
compared with the value the reviewer left. A field counts as an error when a reviewer changed it
(wrong value) or had to type it (missed value). Type changes and manual moves are counted too.
No separate labelling is needed: normal review work produces the ground truth.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone

from apps.core.models import AuditEvent
from apps.documents.models import Document
from apps.documents.services.normalize import norm_ref
from apps.shipments.labels import _short
from apps.shipments.models import Shipment

REVIEWED = [Shipment.Status.APPROVED, Shipment.Status.POSTED]
SKIP = {"line_items"}


def _norm(value) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, list):
        return ",".join(sorted(norm_ref(str(v)) for v in value))
    try:
        from decimal import Decimal

        return str(Decimal(str(value)).normalize())
    except Exception:
        return " ".join(str(value).lower().split())


@dataclass
class FieldStat:
    doc_type: str
    name: str
    total: int = 0
    wrong: int = 0
    missed: int = 0

    @property
    def correct(self) -> int:
        return self.total - self.wrong - self.missed

    @property
    def rate(self) -> float | None:
        return round(100 * self.correct / self.total, 1) if self.total else None


@dataclass
class Report:
    days: int
    documents: int = 0
    documents_all_correct: int = 0
    fields_total: int = 0
    fields_correct: int = 0
    type_fixes: int = 0
    moves: int = 0
    cost_usd: float = 0.0
    ai_documents: int = 0
    by_field: list[FieldStat] = field(default_factory=list)
    by_provider: dict = field(default_factory=dict)
    recent: list[dict] = field(default_factory=list)

    @property
    def field_rate(self) -> float | None:
        return round(100 * self.fields_correct / self.fields_total, 1) if self.fields_total else None

    @property
    def doc_rate(self) -> float | None:
        return round(100 * self.documents_all_correct / self.documents, 1) if self.documents else None

    @property
    def cost_per_document(self) -> float | None:
        return round(self.cost_usd / self.ai_documents, 4) if self.ai_documents else None


def build(org, days: int = 90, reviewed_only: bool = True) -> Report:
    since = timezone.now() - timedelta(days=days)
    docs = Document.objects.filter(organization=org, received_at__gte=since).prefetch_related("fields")
    if reviewed_only:
        docs = docs.filter(match__shipment__status__in=REVIEWED)
    docs = list(docs)
    ids = [str(d.pk) for d in docs]
    events = AuditEvent.objects.filter(object_type="Document", object_id__in=ids,
                                       action__in=["field.corrected", "document.type_changed", "document.moved"]
                                       ).order_by("created_at", "id")
    first_old: dict[tuple[str, str], object] = {}
    corrected_at: dict[tuple[str, str], AuditEvent] = {}
    type_fixed, moved = set(), set()
    for e in events:
        if e.action == "field.corrected":
            key = (e.object_id, e.data.get("field", ""))
            first_old.setdefault(key, e.data.get("old"))
            corrected_at[key] = e
        elif e.action == "document.type_changed":
            type_fixed.add(e.object_id)
        elif e.action == "document.moved":
            moved.add(e.object_id)

    r = Report(days=days, documents=len(docs), type_fixes=len(type_fixed), moves=len(moved))
    stats: dict[tuple[str, str], FieldStat] = {}
    provider_stats = defaultdict(lambda: [0, 0])
    for d in docs:
        usage = d.llm_usage or {}
        if usage.get("calls"):
            r.ai_documents += 1
            r.cost_usd += float(usage.get("cost_usd", 0))
        final = {f.name: f.value for f in d.fields.all()}
        names = (set(final) | {n for (doc_id, n) in first_old if doc_id == str(d.pk)}) - SKIP
        all_ok = str(d.pk) not in type_fixed
        for name in names:
            key = (str(d.pk), name)
            machine = first_old[key] if key in first_old else final.get(name)
            human = final.get(name)
            if _norm(machine) == "" and _norm(human) == "":
                continue
            s = stats.setdefault((d.doc_type, name), FieldStat(d.doc_type, name))
            s.total += 1
            if _norm(machine) == _norm(human):
                ok = True
            elif _norm(machine) == "":
                s.missed += 1
                ok = False
            else:
                s.wrong += 1
                ok = False
            all_ok &= ok
            prov = provider_stats[d.extraction_provider or "unknown"]
            prov[0] += ok
            prov[1] += 1
        r.documents_all_correct += all_ok
    r.by_field = sorted(stats.values(), key=lambda s: (s.rate if s.rate is not None else 101, s.doc_type, s.name))
    r.fields_total = sum(s.total for s in stats.values())
    r.fields_correct = sum(s.correct for s in stats.values())
    r.by_provider = {k: {"correct": v[0], "total": v[1], "rate": round(100 * v[0] / v[1], 1) if v[1] else None}
                     for k, v in provider_stats.items()}
    r.cost_usd = round(r.cost_usd, 4)
    names_by_id = {str(d.pk): d for d in docs}
    for (doc_id, name), e in sorted(corrected_at.items(), key=lambda kv: kv[1].created_at, reverse=True)[:25]:
        if _norm(first_old[(doc_id, name)]) == _norm(names_by_id[doc_id].field(name)):
            continue
        r.recent.append({"doc": names_by_id[doc_id], "field": name, "old": _short(first_old[(doc_id, name)]),
                         "new": _short(names_by_id[doc_id].field(name)), "at": e.created_at, "who": e.actor})
    return r
