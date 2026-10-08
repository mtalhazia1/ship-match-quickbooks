from django.conf import settings
from django.core.management.base import BaseCommand

from apps.core.evaluation import run


class Command(BaseCommand):
    help = "Process the synthetic dataset in a fresh organization and score it against ground truth."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", default="datasets/synthetic")
        parser.add_argument("--org", default=None, help="Organization slug to use (recreated). Default: eval-<timestamp>")
        parser.add_argument("--keep", action="store_true", help="Keep the evaluation organization for inspection in the UI")
        parser.add_argument("--provider", choices=["rules", "anthropic", "openai"], help="Override EXTRACTION_PROVIDER")
        parser.add_argument("--llm-input", choices=["text", "pdf", "auto"], help="Override LLM_INPUT")
        parser.add_argument("--model", help="Override LLM_MODEL, e.g. claude-haiku-4-5")
        parser.add_argument("--ocr", choices=["auto", "text", "anthropic", "textract"], help="Override OCR_PROVIDER")

    def handle(self, *args, **opts):
        for opt, name in (("provider", "EXTRACTION_PROVIDER"), ("llm_input", "LLM_INPUT"), ("model", "LLM_MODEL"),
                          ("ocr", "OCR_PROVIDER")):
            if opts.get(opt):
                setattr(settings, name, opts[opt])
        self.stdout.write(f"Provider {settings.EXTRACTION_PROVIDER}, model {settings.LLM_MODEL or 'default'}, "
                          f"LLM input {settings.LLM_INPUT}, OCR {settings.OCR_PROVIDER}")
        r = run(opts["dataset"], opts["org"], opts["keep"])
        w = self.stdout.write
        c, g = r["classification"], r["grouping"]
        w(f"\nDocuments: {r['documents']}  processed in {r['seconds']}s  needs OCR: {len(r['needs_ocr'])}")
        w(f"Classification: {c['correct']}/{c['total']}")
        w(f"Field accuracy: {r['field_accuracy']:.1%}")
        for name, v in r["fields"].items():
            flag = "" if v["correct"] == v["total"] else "  <--"
            w(f"   {name:20s} {v['correct']:4d}/{v['total']:<4d}{flag}")
        w(f"Grouping: pair precision {g['pair_precision']:.1%}, pair recall {g['pair_recall']:.1%}, "
          f"exact shipments {g['shipments_exact']}/{g['shipments_total']} (predicted {g['predicted_shipments']})")
        w(f"Planted errors caught: {r['error_recall']:.1%}")
        for code, v in sorted(r["error_detection"].items()):
            w(f"   {code:22s} {v['caught']}/{v['planted']}")
        w(f"False alarms: {len(r['false_alarms'])}  {r['false_alarms'] or ''}")
        w(f"Shipments ready without review: {r['auto_ready_rate']:.1%}  {r['shipment_status']}")
        ai = r["ai"]
        w(f"AI: {ai['provider']}  text from {ai['text_sources']}  tokens in/out {ai['input_tokens']}/{ai['output_tokens']}"
          f"  est. cost ${ai['cost_usd']:.4f} (${ai['cost_per_document_usd']:.4f} per document)")
        if r["field_misses"]:
            w("First misses:")
            for m in r["field_misses"][:15]:
                w(f"   {m['file']}: {m['field']} expected {m['expected']!r} got {m['got']!r}")
        w(self.style.SUCCESS(f"Report: {r['report_file']}"))
