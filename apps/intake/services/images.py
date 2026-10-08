"""Photos and scans (JPG, PNG, TIFF, WebP) become a PDF with one page per image frame.

The page is turned upright from the camera's EXIF orientation, sized like a paper page, and kept as an
image: the text is read later by the normal OCR path (Claude or Textract), exactly like a scanned PDF.
"""
from __future__ import annotations

import io
import warnings

from django.conf import settings
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

from apps.documents.services.ingest import RejectedFile

MAX_SIDE_PX = 3500          # ~300 dpi on A4: plenty for OCR, keeps the PDF small
MIN_SHORT_SIDE_PX = 250     # smaller is a logo or signature image, not a page
MIN_PIXELS = 150_000
A4_LONG_IN = 11.69


def image_to_pdf(filename: str, content: bytes) -> tuple[bytes, dict]:
    max_pixels = settings.INTAKE_MAX_IMAGE_MEGAPIXELS * 1_000_000
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(content))
            width, height = img.size
            if width * height > max_pixels:
                raise RejectedFile(f"{filename}: the image is too large ({width * height / 1e6:.0f} megapixels; "
                                   f"the limit is {settings.INTAKE_MAX_IMAGE_MEGAPIXELS}). Resize it and send it again.")
            # Pages are written out one at a time and only the PDF bytes are kept, so a many-frame TIFF
            # never holds every decoded frame in memory; the frame sizes are checked before decoding.
            parts, rotated, total, first_size = [], False, 0, None
            source_format = img.format
            dpi = img.info.get("dpi")
            max_total = settings.INTAKE_MAX_IMAGE_TOTAL_MEGAPIXELS * 1_000_000
            # Only TIFF frames are pages; an animated WebP or PNG contributes its first frame.
            frames = ImageSequence.Iterator(img) if img.format == "TIFF" else [img]
            for n, frame in enumerate(frames):
                if n >= settings.INTAKE_MAX_IMAGE_PAGES:
                    raise RejectedFile(f"{filename}: more than {settings.INTAKE_MAX_IMAGE_PAGES} pages. "
                                       "Send it in smaller parts.")
                fw, fh = frame.size
                total += fw * fh
                if fw * fh > max_pixels or total > max_total:
                    raise RejectedFile(f"{filename}: the image is too large ({total / 1e6:.0f} megapixels in "
                                       f"{n + 1} page(s)). Resize it or send it in smaller parts.")
                orientation = frame.getexif().get(0x0112, 1)
                upright = ImageOps.exif_transpose(frame) if orientation not in (None, 1) else frame.copy()
                rotated = rotated or orientation not in (None, 1)
                page = _page_image(upright)
                del upright
                if first_size is None:
                    first_size = page.size
                    if min(page.size) < MIN_SHORT_SIDE_PX or page.size[0] * page.size[1] < MIN_PIXELS:
                        raise RejectedFile(f"{filename}: the image is too small to be a document page "
                                           f"({page.size[0]} x {page.size[1]} pixels). It may be a logo or "
                                           "signature image.")
                buf = io.BytesIO()
                page.save(buf, format="PDF", resolution=_resolution(page.size, dpi), title=filename, quality=88)
                parts.append(buf.getvalue())
                del page
    except RejectedFile:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, TypeError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, EOFError) as e:
        raise RejectedFile(f"{filename}: the image can't be opened ({e.__class__.__name__}). "
                           "It may be damaged; take the photo or scan again.") from e
    if not parts:
        raise RejectedFile(f"{filename}: the image has no pages")
    pdf = parts[0] if len(parts) == 1 else _merge(parts)
    info = {"source": source_format, "pages": len(parts), "width": width, "height": height,
            "rotated": rotated, "dpi": round(_resolution(first_size, dpi))}
    return pdf, info


def _merge(parts: list[bytes]) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for part in parts:
        for page in PdfReader(io.BytesIO(part)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _page_image(frame: Image.Image) -> Image.Image:
    """RGB or grayscale, transparent areas on white, at most MAX_SIDE_PX on the long side."""
    mode = frame.mode
    if mode in ("I;16", "I;16B", "I;16L", "I;16N", "I"):
        frame = frame.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    elif mode == "F":
        frame = frame.convert("L")
    elif mode in ("RGBA", "LA", "PA") or (mode == "P" and "transparency" in frame.info):
        rgba = frame.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        frame = background
    elif mode == "1":
        frame = frame.convert("L")
    elif mode not in ("RGB", "L"):
        frame = frame.convert("RGB")
    if max(frame.size) > MAX_SIDE_PX:
        frame = frame.copy()
        frame.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX), Image.Resampling.LANCZOS)
    return frame


def _resolution(size: tuple[int, int], dpi) -> float:
    """Page size: the scan's own DPI when it is believable, otherwise fit the long side to A4."""
    try:
        x_dpi = float(dpi[0]) if dpi else 0.0
    except (TypeError, ValueError, IndexError):
        x_dpi = 0.0
    long_px = max(size)
    if 100 <= x_dpi <= 1200 and 5 <= long_px / x_dpi <= 17:
        return x_dpi
    return max(36.0, long_px / A4_LONG_IN)
