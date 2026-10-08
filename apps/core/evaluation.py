"""Score the pipeline against the synthetic ground truth.

Measures four things, so every prompt or rule change can be compared with a number:
  1. classification accuracy
  2. field extraction accuracy (per field)
  3. shipment grouping (pairwise precision/recall + fully correct shipments)
  4. planted-error detection (recall per error, plus false alarms)
"""
from __future__ import annotations

import itertools
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from apps.core.models import Organization
from apps.documents.models import Document, IngestedEmail
from apps.documents.services.ingest import ingest_bytes
from apps.documents.services.normalize import norm_ref
from apps.shipments.models import Shipment, ValidationIssue

DOC_LEVEL_ERRORS = {"total_mismatch", "invalid_container", "container_not_on_bl", "duplicate_invoice", "amount_outlier"}
SHIPMENT_LEVEL_ERRORS = {"missing_bl"}
SKIP_FIELDS = {"layout_variant"}


def _same(name: str, expected, got) -> bool:
    if name == "line_items":
        if not isinstance(got, list) or len(got) != len(expected):
            return False
        return all(Decimal(str(a.get("amount"))) == Decimal(str(b["amount"])) for a, b in zip(got, expected))
    if isinstance(expected, list):
        return sorted(norm_ref(x) for x in got or []) == sorted(norm_ref(x) for x in expected)
    if name in {"total_amount"}:
        try:
            return Decimal(str(got)) == Decimal(str(expected))
        except Exception:
            return False
    return str(got or "").strip().lower() == str(expected).strip().lower()


def _pdf_dir(root: Path) -> Path:
    return root / "pdf" if (root / "pdf").is_dir() else root


def run(dataset: str | Path, org_slug: str | None = None, keep: bool = False) -> dict:
    """Works for the synthetic set (with emails.json) and for real documents labelled by hand
    (ground_truth.json only; PDFs in the folder or in pdf/, processed in file-name order)."""
    root = Path(dataset)
    truth = json.loads((root / "ground_truth.json").read_text())
    truth.setdefault("shipments", [])
    for d in truth["documents"]:
        d.setdefault("planted_errors", [])
        d.setdefault("scanned", False)
    slug = org_slug or f"eval-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    Organization.objects.filter(slug=slug).delete()
    org = Organization.objects.create(slug=slug, name=f"Evaluation {slug}")

    started = datetime.now(timezone.utc)
    pdf_dir = _pdf_dir(root)
    if (root / "emails.json").exists():
        for e in json.loads((root / "emails.json").read_text()):
            email = IngestedEmail.objects.create(organization=org, message_id=e["message_id"], subject=e["subject"],
                                                 sender=e["from"], received_at=datetime.fromisoformat(e["received_at"]))
            ingest_bytes(org, e["file"], (pdf_dir / e["file"]).read_bytes(), source=Document.Source.EMAIL,
                         email=email, process="sync")
    else:
        for d in sorted(truth["documents"], key=lambda d: d["file"]):
            ingest_bytes(org, d["file"], (pdf_dir / d["file"]).read_bytes(), source=Document.Source.FOLDER,
                         process="sync")
    seconds = (datetime.now(timezone.utc) - started).total_seconds()

    docs = {d.original_filename: d for d in Document.objects.filter(organization=org).prefetch_related("fields")}
    gt_docs = {d["file"]: d for d in truth["documents"]}
    report: dict = {"org": slug, "documents": len(gt_docs), "seconds": round(seconds, 1)}
    usage = [d.llm_usage or {} for d in docs.values()]
    report["ai"] = {
        "provider": sorted({d.extraction_provider for d in docs.values() if d.extraction_provider}),
        "text_sources": dict(Counter(d.text_source for d in docs.values())),
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
        "cost_usd": round(sum(u.get("cost_usd", 0) for u in usage), 4),
        "cost_per_document_usd": round(sum(u.get("cost_usd", 0) for u in usage) / max(1, len(usage)), 4),
    }

    # 1-2. classification and fields (text-readable documents only)
    readable = [f for f, g in gt_docs.items() if docs[f].status != Document.Status.NEEDS_OCR]
    report["needs_ocr"] = sorted(f for f in gt_docs if docs[f].status == Document.Status.NEEDS_OCR)
    cls_ok = sum(docs[f].doc_type == gt_docs[f]["doc_type"] for f in readable)
    report["classification"] = {"correct": cls_ok, "total": len(readable)}
    per_field = defaultdict(lambda: [0, 0])
    misses = []
    for f in readable:
        got = docs[f].data()
        for name, expected in gt_docs[f]["fields"].items():
            if name in SKIP_FIELDS:
                continue
            ok = _same(name, expected, got.get(name))
            per_field[name][0] += ok
            per_field[name][1] += 1
            if not ok:
                misses.append({"file": f, "field": name, "expected": expected, "got": got.get(name)})
    report["fields"] = {k: {"correct": v[0], "total": v[1]} for k, v in sorted(per_field.items())}
    report["field_accuracy"] = round(sum(v[0] for v in per_field.values()) / max(1, sum(v[1] for v in per_field.values())), 4)
    report["field_misses"] = misses[:50]

    # 3. grouping
    pred = {f: (docs[f].match.shipment_id if hasattr(docs[f], "match") else f"unmatched:{f}") for f in readable}
    tp = fp = fn = 0
    for a, b in itertools.combinations(readable, 2):
        same_gt = gt_docs[a]["shipment_id"] == gt_docs[b]["shipment_id"]
        same_pred = pred[a] == pred[b]
        tp += same_gt and same_pred
        fp += (not same_gt) and same_pred
        fn += same_gt and not same_pred
    by_gt = defaultdict(set)
    by_pred = defaultdict(set)
    for f in readable:
        by_gt[gt_docs[f]["shipment_id"]].add(f)
        by_pred[pred[f]].add(f)
    exact = sum(1 for files in by_gt.values() if len({pred[f] for f in files}) == 1 and by_pred[pred[next(iter(files))]] == files)
    report["grouping"] = {
        "pair_precision": round(tp / max(1, tp + fp), 4), "pair_recall": round(tp / max(1, tp + fn), 4),
        "shipments_exact": exact, "shipments_total": len(by_gt), "predicted_shipments": Shipment.objects.filter(organization=org).count(),
    }

    # 4. planted errors
    issues = list(ValidationIssue.objects.filter(organization=org).select_related("document"))
    issues_by_doc = defaultdict(set)
    issues_by_ship = defaultdict(set)
    for i in issues:
        if i.document:
            issues_by_doc[i.document.original_filename].add(i.code)
        if i.shipment_id:
            issues_by_ship[i.shipment_id].add(i.code)
    # A duplicate pair counts as caught if either copy is flagged: whichever arrives later is the duplicate.
    dup_partner = {}
    for f, g in gt_docs.items():
        if "duplicate_invoice" in g["planted_errors"]:
            for f2, g2 in gt_docs.items():
                if f2 != f and g2["shipment_id"] == g["shipment_id"] and \
                        g2["fields"].get("invoice_number") == g["fields"].get("invoice_number"):
                    dup_partner[f], dup_partner[f2] = f2, f
    detection = defaultdict(lambda: {"planted": 0, "caught": 0})
    for f, g in gt_docs.items():
        for code in g["planted_errors"]:
            detection[code]["planted"] += 1
            files = {f, dup_partner.get(f, f)} if code == "duplicate_invoice" else {f}
            detection[code]["caught"] += any(code in issues_by_doc.get(x, set()) for x in files)
    scanned_bl_shipments = {g["shipment_id"] for g in gt_docs.values() if g["scanned"] and g["doc_type"] == "bill_of_lading"}
    false_alarms = []
    for s in truth["shipments"]:
        pred_ships = {pred[f] for f in by_gt.get(s["shipment_id"], set()) if not str(pred[f]).startswith("unmatched")}
        codes = set().union(*(issues_by_ship.get(p, set()) for p in pred_ships)) if pred_ships else set()
        if "missing_bl" in s["planted_errors"]:
            detection["missing_bl"]["planted"] += 1
            detection["missing_bl"]["caught"] += "missing_bl" in codes
        elif "missing_bl" in codes and s["shipment_id"] not in scanned_bl_shipments:
            false_alarms.append({"shipment": s["shipment_id"], "code": "missing_bl"})
    for f, codes in issues_by_doc.items():
        planted = set(gt_docs[f]["planted_errors"]) if f in gt_docs else set()
        if f in dup_partner:
            planted.add("duplicate_invoice")
        for code in (codes & DOC_LEVEL_ERRORS) - planted:
            false_alarms.append({"file": f, "code": code})
    report["error_detection"] = dict(detection)
    report["error_recall"] = round(sum(v["caught"] for v in detection.values()) / max(1, sum(v["planted"] for v in detection.values())), 4)
    report["false_alarms"] = false_alarms
    report["issue_counts"] = dict(Counter(i.code for i in issues))
    statuses = Counter(Shipment.objects.filter(organization=org).values_list("status", flat=True))
    report["shipment_status"] = dict(statuses)
    report["auto_ready_rate"] = round(statuses.get("ready", 0) / max(1, sum(statuses.values())), 4)

    out_dir = Path("eval_reports")
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{slug}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    report["report_file"] = str(path)
    if not keep:
        org.delete()
    return report
