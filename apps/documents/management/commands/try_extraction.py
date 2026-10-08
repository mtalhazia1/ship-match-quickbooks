"""Read one file and print what ShipMatch extracts, with grounding, tokens and cost. Saves nothing.

Accepts what ShipMatch accepts: PDFs, photos and scans (converted to a PDF first) and spreadsheets.

    python manage.py try_extraction real_docs\\invoice.pdf
    python manage.py try_extraction real_docs\\invoice.pdf --input pdf --model claude-haiku-4-5
"""
import json
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.documents.services import llm
from apps.documents.services.classify import classify
from apps.documents.services.extract import extract
from apps.documents.services.ocr import TextResult, read_text


class Command(BaseCommand):
    help = "Show what ShipMatch reads from one PDF, photo or spreadsheet (nothing is saved)."

    def add_arguments(self, parser):
        parser.add_argument("pdf")
        parser.add_argument("--provider", choices=["rules", "anthropic", "openai"])
        parser.add_argument("--input", choices=["text", "pdf"], help="Send only the text layer, or the PDF too")
        parser.add_argument("--model")
        parser.add_argument("--json", action="store_true", help="Print JSON instead of a table")

    def handle(self, *args, **o):
        path = Path(o["pdf"])
        if not path.exists():
            raise CommandError(f"{path} not found")
        if o["provider"]:
            settings.EXTRACTION_PROVIDER = o["provider"]
        if o["model"]:
            settings.LLM_MODEL = o["model"]
        data = path.read_bytes()
        started = time.monotonic()
        sheet_text = None
        if not data.startswith(b"%PDF"):  # a photo or a spreadsheet: convert it the way intake does
            from apps.documents.services.ingest import RejectedFile
            from apps.intake.services import formats

            try:
                kind = formats.detect(path.name, data)
                if kind.kind == formats.ARCHIVE:
                    raise CommandError("This is a ZIP archive. Unpack it and try one file at a time.")
                original, (data, _) = data, formats.convert(path.name, data, kind)
                if kind.kind == formats.SPREADSHEET:
                    from apps.intake.services.sheets import text_for

                    sheet_text = text_for(path.name, original, kind.subtype)
            except RejectedFile as e:
                raise CommandError(str(e)) from e
        with llm.track_usage() as calls:
            tr = read_text(data) if sheet_text is None else TextResult(sheet_text, 1, False, "spreadsheet")
            if tr.needs_ocr:
                raise CommandError("No text layer and no OCR available. Set EXTRACTION_PROVIDER=anthropic and "
                                   "ANTHROPIC_API_KEY, or OCR_PROVIDER=textract.")
            doc_type, conf = classify(tr.text)
            send_pdf = o["input"] == "pdf" or (o["input"] is None and tr.method not in ("text_layer", "spreadsheet"))
            fields, provider = extract(doc_type, tr.text, pdf=data if send_pdf else None)
        usage = llm.summarize(calls)
        result = {
            "file": path.name, "pages": tr.page_count, "text_source": tr.method, "doc_type": doc_type,
            "type_confidence": conf, "provider": provider, "model": llm.model_name() if provider != "rules" else "",
            "pdf_sent": bool(send_pdf and provider == "anthropic"), "seconds": round(time.monotonic() - started, 1),
            "fields": [{"name": f.name, "value": f.value, "confidence": f.confidence, "grounded": f.grounded}
                       for f in fields],
            "usage": usage,
        }
        if o["json"]:
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return
        w = self.stdout.write
        w(f"{path.name}: {doc_type} ({conf:.2f}), {tr.page_count} page(s), text from {tr.method}")
        w(f"Read by {provider}{' ' + result['model'] if result['model'] else ''}"
          f"{' with the PDF attached' if result['pdf_sent'] else ''} in {result['seconds']}s")
        for f in fields:
            mark = "ok   " if f.grounded else "CHECK"
            value = f.value if not isinstance(f.value, list) or f.name != "line_items" else f"{len(f.value)} lines"
            w(f"  {mark} {f.name:18s} {value}")
        if usage["calls"]:
            w(f"Tokens in/out {usage['input_tokens']}/{usage['output_tokens']}, est. cost ${usage['cost_usd']:.4f}")
        w("CHECK = value not found word-for-word in the document text, so a reviewer must confirm it.")
