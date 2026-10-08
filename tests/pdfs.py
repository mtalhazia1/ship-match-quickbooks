"""Small, valid, reproducible PDFs for tests that only need *a* PDF.

Intake now refuses a file that merely starts with "%PDF" (QA-043), so tests can no longer stand in bytes like
b"%PDF-1.4 invoice". The same text gives the same bytes (the duplicate-detection tests rely on that); different
text gives different bytes."""
import io

from reportlab.pdfgen import canvas


def make_pdf(text: str) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, invariant=1)   # invariant: no timestamp or random id in the file
    pdf.drawString(72, 720, text)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
