"""Evidence: find where each extracted value is printed on the PDF.

For every extracted field this returns the page and the boxes where the value was read, so a
reviewer can click a value and see it highlighted on the document. Boxes are fractions (0..1) of
the page as displayed (top-left origin, crop box, page rotation applied), which is what the
PDF.js viewer draws on, at any zoom.

How a value is found (plain code, no AI):
  * words and their positions come from the PDF text layer (pdfplumber), or for scanned pages from
    the OCR word boxes AWS Textract returned (see OcrLayout);
  * text values (invoice numbers, names, container numbers) are compared letter and digit only,
    across word breaks, so 'OSLU 123456-7' matches 'OSLU1234567' and a vendor name spanning
    several words becomes a run of consecutive words;
  * amounts are compared as numbers: 1,234.50 / 1234.5 / 1.234,50 / 1 234,50 / USD 1,234.50 / $1,234.50;
  * dates are compared as dates: 2026-03-04 / 03/04/2026 / 04.03.2026 / 4 Mar 2026 / March 4, 2026 / 04-Mar-26;
  * when a value is printed more than once, the copy next to a matching label wins ('Invoice No',
    'Total', 'B/L'), totals prefer the bottom of the last page, company names the top of page 1,
    and a box already used by another field is avoided.

A value that is not on the page (for example a value a reviewer typed) gets no box: a wrong box
is worse than none. Scanned pages without OCR word positions get the status 'scanned'.

Locating never fails processing: safe_locate() logs and swallows every error.
"""
from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

log = logging.getLogger(__name__)

VERSION = 1
FOUND, PARTIAL, NOT_FOUND, SCANNED = "found", "partial", "not_found", "scanned"
MIN_WORDS_PER_PAGE = 3          # fewer words than this on a page with an image: the page is a scan
LIST_FIELDS = {"container_numbers", "po_numbers"}
LINE_ITEMS = "line_items"

# Fields are located in this order; a box claimed by an earlier field is avoided by later ones.
FIELD_ORDER = [
    "invoice_number", "bl_number", "total_amount", "invoice_date", "due_date", "issue_date", "currency",
    "vendor_name", "carrier_name", "shipper", "consignee", "port_of_loading", "port_of_discharge",
    "vessel_voyage", "po_numbers", "container_numbers",
]

# Labels printed next to (left of, or above) a value. Lower-case regular expressions.
LABELS: dict[str, list[str]] = {
    "invoice_number": [r"invoice\s*(?:no|nr|number|num|#)", r"\binv\.?\s*(?:no|#)", r"\bbill\s*(?:no|number|#)",
                       r"document\s*(?:no|number)", r"invoice\s*:"],
    "bl_number": [r"\bb\s*/\s*l\b", r"bill\s+of\s+lading", r"\b[mh]\s*/?\s*b\s*/?\s*l\b", r"\bbol\b", r"\bb/l"],
    "total_amount": [r"\btotal\b", r"amount\s+due", r"balance\s+due", r"amount\s+payable", r"\bdue\b",
                     r"please\s+pay", r"net\s+payable"],
    "invoice_date": [r"invoice\s+date", r"\bdated?\b", r"\bissued\b", r"date\s+of\s+invoice"],
    "issue_date": [r"date\s+of\s+issue", r"issue\s+date", r"\bdated?\b", r"shipped\s+on\s+board", r"\bissued\b"],
    "due_date": [r"due\s+date", r"payment\s+due", r"\bdue\b", r"pay\s+by"],
    "container_numbers": [r"container", r"\bcntr", r"\bctnr", r"\bcont\.?\s*(?:no|#)", r"equipment"],
    "po_numbers": [r"\bp\.?\s*o\b", r"purchase\s+order", r"order\s+(?:no|number|#)", r"customer\s+ref",
                   r"shipper'?s\s+ref", r"your\s+ref"],
    "currency": [r"currency", r"\bccy\b", r"\bcur\b"],
    "shipper": [r"shipper", r"exporter", r"consignor"],
    "consignee": [r"consignee"],
    "port_of_loading": [r"port\s+of\s+loading", r"\bpol\b", r"place\s+of\s+receipt", r"loading"],
    "port_of_discharge": [r"port\s+of\s+discharge", r"\bpod\b", r"place\s+of\s+delivery", r"discharge"],
    "vessel_voyage": [r"vessel", r"voyage", r"\bvoy\b"],
    "vendor_name": [r"\bseller\b", r"supplier", r"\bvendor\b", r"exporter", r"beneficiary"],
    "carrier_name": [r"carrier"],
}
# Labels that mean "not this value", e.g. a subtotal printed with the same amount as the total.
NEGATIVE: dict[str, list[str]] = {
    "total_amount": [r"sub\s*-?\s*total", r"\btax\b", r"\bvat\b", r"\bgst\b", r"discount", r"deposit", r"\bpaid\b",
                     r"freight\s+total", r"line\s+total"],
    "invoice_date": [r"\bdue\b", r"\bship", r"b\s*/\s*l", r"delivery", r"\bpo\b", r"order"],
    "issue_date": [r"\bdue\b"],
    "due_date": [r"invoice\s+date", r"\bissued\b"],
    "invoice_number": [r"\bp\.?\s*o\b", r"order", r"b\s*/\s*l", r"customer"],
    "bl_number": [r"booking"],
    "shipper": [r"consignee", r"notify"],
    "consignee": [r"shipper", r"notify"],
}
TOP_FIELDS = {"vendor_name", "carrier_name"}            # usually the letterhead: top of page 1
TOTAL_FIELDS = {"total_amount"}                          # usually at the bottom of the last page
HEADER_FIELDS = {"invoice_number", "bl_number", "invoice_date", "issue_date", "due_date", "currency",
                 "po_numbers", "container_numbers"}
FUZZY_FIELDS = {"vendor_name", "carrier_name", "shipper", "consignee", "port_of_loading", "port_of_discharge",
                "vessel_voyage"}

CURRENCY_CODES = {
    "USD", "EUR", "GBP", "CNY", "RMB", "AED", "SAR", "PKR", "INR", "JPY", "CAD", "AUD", "TRY", "VND", "HKD", "SGD",
    "CHF", "MXN", "BRL", "KRW", "THB", "MYR", "IDR", "PHP", "ZAR", "NZD", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF",
    "ILS", "EGP", "QAR", "KWD", "BHD", "OMR", "NGN", "KES", "BDT", "LKR", "TWD", "CLP", "COP", "PEN", "ARS", "RON",
    "US", "RS", "RP",
}
CURRENCY_SYMBOLS = "$€£¥₹₩₺₫฿₱"


# --------------------------------------------------------------------------- text index


def norm_chars(text: str) -> tuple[str, list[int]]:
    """Letters and digits only, upper case, accents removed; with the index of each in `text`."""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text or ""):
        for c in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(c) or not c.isalnum():
                continue
            for u in c.upper():
                out.append(u)
                idx.append(i)
    return "".join(out), idx


def norm(text: Any) -> str:
    return norm_chars(str(text if text is not None else ""))[0]


@dataclass
class Word:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    page: int
    chars: list[tuple[float, float]] | None = None   # x extent of each character of `text`
    bold: bool = False
    size: float = 0.0
    line: int = -1          # index in PageIndex.lines
    pos: int = -1           # index within the line
    order: int = -1         # reading-order index within the page
    norm: str = ""
    norm_idx: list[int] = field(default_factory=list)

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def h(self) -> float:
        return max(self.bottom - self.top, 1e-4)

    @property
    def char_w(self) -> float:
        return (self.x1 - self.x0) / max(len(self.text), 1)

    def char_x(self, i: int) -> tuple[float, float]:
        """Horizontal extent of character i of the word text."""
        i = max(0, min(i, len(self.text) - 1))
        if self.chars and len(self.chars) == len(self.text):
            return self.chars[i]
        w = (self.x1 - self.x0) / max(len(self.text), 1)
        return self.x0 + w * i, self.x0 + w * (i + 1)


@dataclass
class Line:
    page: int
    index: int
    words: list[Word]
    text: str = ""
    spans: list[tuple[int, int]] = field(default_factory=list)   # (start, end) of each word in text

    @property
    def top(self) -> float:
        return min(w.top for w in self.words)

    @property
    def bottom(self) -> float:
        return max(w.bottom for w in self.words)

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2

    def word_at(self, char: int) -> tuple[int, int]:
        """(word position, character in word) for a character index of the line text."""
        for pos, (s, e) in enumerate(self.spans):
            if s <= char < e:
                return pos, char - s
            if char < s:  # the space before a word
                return pos, 0
        return len(self.words) - 1, len(self.words[-1].text) - 1


@dataclass
class PageIndex:
    number: int
    words: list[Word]
    lines: list[Line]
    stream: str = ""
    owner: list[tuple[int, int]] = field(default_factory=list)  # stream char -> (word order, char in word text)
    median_size: float = 0.0
    image_only: bool = False     # a scanned page: an image with no (or almost no) text layer

    @property
    def has_text(self) -> bool:
        return bool(self.words) and not self.image_only

    @classmethod
    def build(cls, number: int, words: list[Word], image_only: bool = False) -> PageIndex:
        lines: list[list[Word]] = []
        for w in sorted(words, key=lambda w: (w.cy, w.x0)):
            if lines:
                last = lines[-1]
                ref_cy = sum(x.cy for x in last) / len(last)
                ref_h = max(x.h for x in last)
                if abs(w.cy - ref_cy) <= max(ref_h, w.h) * 0.45:
                    last.append(w)
                    continue
            lines.append([w])
        page_lines: list[Line] = []
        ordered: list[Word] = []
        for li, lw in enumerate(lines):
            lw.sort(key=lambda w: w.x0)
            text, spans = "", []
            for pos, w in enumerate(lw):
                if text:
                    text += " "
                spans.append((len(text), len(text) + len(w.text)))
                text += w.text
                w.line, w.pos, w.order = li, pos, len(ordered)
                w.norm, w.norm_idx = norm_chars(w.text)
                ordered.append(w)
            page_lines.append(Line(number, li, lw, text, spans))
        stream, owner = [], []
        for w in ordered:
            stream.append(w.norm)
            owner.extend((w.order, i) for i in w.norm_idx)
        sizes = [w.size for w in ordered if w.size]
        return cls(number, ordered, page_lines, "".join(stream), owner, median(sizes) if sizes else 0.0, image_only)


@dataclass
class DocIndex:
    pages: list[PageIndex]
    source: str = "text_layer"     # text_layer | textract | none

    @property
    def has_text(self) -> bool:
        return any(p.has_text for p in self.pages)

    @property
    def has_image_pages(self) -> bool:
        return not self.pages or any(not p.has_text for p in self.pages)

    @property
    def last_page(self) -> int:
        return max((p.number for p in self.pages), default=1)


def index_pdf(pdf_bytes: bytes) -> DocIndex:
    """Words and positions from the PDF's own text layer."""
    import pdfplumber

    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for n, page in enumerate(pdf.pages, start=1):
            words = _plumber_words(page, n)
            # A scan may carry a stray word or two ('Scanned by ...'): with an image and so little text,
            # treat the page as an image.
            image_only = not words or (len(words) < MIN_WORDS_PER_PAGE and bool(page.images))
            pages.append(PageIndex.build(n, words, image_only))
    return DocIndex(pages, "text_layer")


def _plumber_words(page, n: int) -> list[Word]:
    # The page as viewers show it: the crop box, clipped to the media box (pdfplumber has already
    # applied /Rotate, so this is in display orientation).
    mx0, mtop, mx1, mbottom = page.mediabox
    cx0, ctop, cx1, cbottom = getattr(page, "cropbox", None) or page.mediabox
    x0, top, x1, bottom = max(mx0, cx0), max(mtop, ctop), min(mx1, cx1), min(mbottom, cbottom)
    width, height = x1 - x0, bottom - top
    if width <= 0 or height <= 0:
        return []
    try:
        page = page.dedupe_chars()
    except Exception:  # older pdfplumber, or odd fonts
        pass
    raw = page.extract_words(keep_blank_chars=False, use_text_flow=False,
                             extra_attrs=["fontname", "size"], return_chars=True)
    words = []
    for w in raw:
        text = w.get("text") or ""
        if not text.strip():
            continue
        nx0, nx1 = (w["x0"] - x0) / width, (w["x1"] - x0) / width
        ntop, nbottom = (w["top"] - top) / height, (w["bottom"] - top) / height
        if nx1 <= 0 or nx0 >= 1 or nbottom <= 0 or ntop >= 1:
            continue  # outside the visible page
        chars = w.get("chars") or []
        cx = [((c["x0"] - x0) / width, (c["x1"] - x0) / width) for c in chars] if len(chars) == len(text) else None
        font = str(w.get("fontname") or "").lower()
        words.append(Word(text, _clip(nx0), _clip(ntop), _clip(nx1), _clip(nbottom), n, cx,
                          bold=any(k in font for k in ("bold", "black", "heavy", "semibold")),
                          size=float(w.get("size") or 0)))
    return words


def index_ocr(words: list, page_count: int = 0) -> DocIndex:
    """Words and positions from OCR (Textract word boxes stored as [page, x0, top, x1, bottom, text])."""
    by_page: dict[int, list[Word]] = {}
    for item in words or []:
        try:
            p, x0, top, x1, bottom, text = item
            p = int(p)
        except (TypeError, ValueError):
            continue
        if not str(text).strip():
            continue
        by_page.setdefault(p, []).append(Word(str(text), _clip(x0), _clip(top), _clip(x1), _clip(bottom), p,
                                              size=(float(bottom) - float(top)) * 1000))
    n = max([page_count, *by_page.keys()]) if by_page or page_count else 0
    return DocIndex([PageIndex.build(p, by_page.get(p, []), not by_page.get(p)) for p in range(1, n + 1)], "textract")


def _clip(v) -> float:
    return min(1.0, max(0.0, float(v)))


# --------------------------------------------------------------------------- hits


@dataclass
class Hit:
    page: PageIndex
    words: list[Word]               # words the value covers, in reading order
    first_char: int                 # first character (in words[0].text)
    last_char: int                  # last character (in words[-1].text)
    quality: float                  # how exactly the printed text matches (0..1)
    negative: bool = False          # printed with a minus sign or in brackets
    score: float = 0.0              # ranking score (quality + label + position)
    labelled: bool = False

    @property
    def line(self) -> Line:
        return self.page.lines[self.words[0].line]

    @property
    def key(self) -> tuple:
        return (self.page.number, self.words[0].line, self.words[0].x0)

    @property
    def ids(self) -> set[tuple[int, int]]:
        return {(self.page.number, w.order) for w in self.words}

    @property
    def top(self) -> float:
        return min(w.top for w in self.words)

    @property
    def x0(self) -> float:
        return self.boxes()[0][0]

    @property
    def bold(self) -> bool:
        return any(w.bold for w in self.words)

    @property
    def size(self) -> float:
        return max((w.size for w in self.words), default=0.0)

    @property
    def line_char(self) -> int:
        """Index in the line text where the value starts."""
        w = self.words[0]
        return self.line.spans[w.pos][0] + self.first_char

    def boxes(self) -> list[list[float]]:
        """One rectangle per printed line the value covers."""
        groups: list[list[Word]] = []
        for w in self.words:
            if groups and groups[-1][-1].line == w.line:
                groups[-1].append(w)
            else:
                groups.append([w])
        out = []
        for gi, g in enumerate(groups):
            x0 = g[0].char_x(self.first_char)[0] if gi == 0 else g[0].x0
            x1 = g[-1].char_x(self.last_char)[1] if gi == len(groups) - 1 else g[-1].x1
            out.append([round(_clip(min(x0, x1)), 4), round(min(w.top for w in g), 4),
                        round(_clip(max(x0, x1)), 4), round(max(w.bottom for w in g), 4)])
        return out


def _gap_ok(a: Word, b: Word) -> bool:
    """b directly follows a in the same printed phrase (no column gap between them)."""
    if a.line == b.line:
        return b.x0 - a.x1 <= max(0.02, 3.2 * max(a.char_w, b.char_w))
    return False


def _continues(prev_line_words: list[Word], b: Word, page: PageIndex) -> bool:
    """b starts the next printed line of the same block (an address or name wrapped over lines)."""
    first = prev_line_words[0]
    if b.line <= first.line or b.line - prev_line_words[-1].line > 1:
        return False
    if b.top - prev_line_words[-1].bottom > 1.6 * max(first.h, b.h):
        return False
    return abs(b.x0 - first.x0) <= 0.04 or (b.pos == 0 and abs(b.x0 - first.x0) <= 0.1)


def _contiguous(words: list[Word], page: PageIndex) -> bool:
    if len(words) > 1 and words[-1].line - words[0].line > 3:
        return False
    current = [words[0]]
    for a, b in zip(words, words[1:]):
        if a.line == b.line:
            if not _gap_ok(a, b):
                return False
            current.append(b)
        elif _continues(current, b, page):
            current = [b]
        else:
            return False
    return True


def _boundary_ok(text: str, i: int, step: int) -> bool:
    """Inside a word, a value may only start or end where a separator is printed ('No.:INV-1')."""
    j = i + step
    if j < 0 or j >= len(text):
        return True
    return not text[j].isalnum()


def text_hits(page: PageIndex, value: Any) -> list[Hit]:
    """Every place the letters and digits of `value` are printed, across word breaks."""
    target = norm(value)
    if not target or not page.stream:
        return []
    raw = str(value).strip()
    hits, start = [], 0
    while True:
        s = page.stream.find(target, start)
        if s < 0:
            break
        start = s + 1
        e = s + len(target) - 1
        (wa, ca), (wb, cb) = page.owner[s], page.owner[e]
        words = page.words[wa:wb + 1]
        start_aligned = s == 0 or page.owner[s - 1][0] != wa
        end_aligned = e + 1 >= len(page.owner) or page.owner[e + 1][0] != wb
        if not start_aligned:
            if len(target) < 4 or not _boundary_ok(words[0].text, ca, -1):
                continue
        if not end_aligned:
            if len(target) < 4 or not _boundary_ok(words[-1].text, cb, +1):
                continue
        if not _contiguous(words, page):
            continue
        first_char = 0 if start_aligned else ca
        last_char = len(words[-1].text) - 1 if end_aligned else cb
        # Trim punctuation printed around the value ('(USD)', 'INV-1,') from the box, unless the value
        # itself starts or ends with it ('Charge (THC)', 'Co., Ltd.').
        if start_aligned and raw[:1].isalnum() and words[0].norm_idx:
            first_char = words[0].norm_idx[0]
        if end_aligned and raw[-1:].isalnum() and words[-1].norm_idx:
            last_char = words[-1].norm_idx[-1]
        quality = 1.0 if start_aligned and end_aligned else 0.8
        hits.append(Hit(page, words, first_char, last_char, quality))
    return hits


def block_hits(page: PageIndex, value: Any) -> list[Hit]:
    """A multi-word value printed as a block in a column (lines interleaved with another column)."""
    target = norm(value)
    if len(target) < 6 or not re.search(r"\s", str(value).strip()):
        return []
    hits = []
    for line in page.lines:
        for start in line.words:
            if not start.norm or not target.startswith(start.norm):
                continue
            seq, row, acc, cur = [start], [start], start.norm, line
            while len(acc) < len(target):
                nxt, pos = None, row[-1].pos + 1
                if pos < len(cur.words) and _gap_ok(row[-1], cur.words[pos]):
                    nxt = cur.words[pos]
                elif cur.index + 1 < len(page.lines):
                    below = page.lines[cur.index + 1]
                    col = [w for w in below.words if abs(w.x0 - row[0].x0) <= 0.03]
                    if col and _continues(row, col[0], page):
                        nxt, cur, row = col[0], below, []
                if nxt is None or not nxt.norm or not target.startswith(acc + nxt.norm):
                    break
                seq.append(nxt)
                row.append(nxt)
                acc += nxt.norm
            if acc == target and len(seq) > 1 and seq[-1].line != seq[0].line:
                hits.append(Hit(page, seq, seq[0].norm_idx[0], seq[-1].norm_idx[-1], 0.9))
    return hits


def fuzzy_hits(page: PageIndex, value: Any, threshold: float = 90.0) -> list[Hit]:
    """Names printed slightly differently ('Co., Ltd' vs 'Company Limited' is too far; 'Ltd' vs 'Ltd.' is fine)."""
    from rapidfuzz import fuzz

    target = norm(value)
    n_tokens = len(str(value).split())
    if len(target) < 8 or n_tokens < 2:
        return []
    hits = []
    for line in page.lines:
        ws = line.words
        for i in range(len(ws)):
            for k in range(max(1, n_tokens - 1), n_tokens + 2):
                run = ws[i:i + k]
                if len(run) < k or any(not _gap_ok(a, b) for a, b in zip(run, run[1:])):
                    break
                score = fuzz.ratio("".join(w.norm for w in run), target)
                if score >= threshold:
                    hits.append(Hit(page, run, run[0].norm_idx[0] if run[0].norm_idx else 0,
                                    run[-1].norm_idx[-1] if run[-1].norm_idx else len(run[-1].text) - 1,
                                    0.55 + (score - threshold) / 100))
    return hits


# ---- numbers

_NUM_PLAIN = re.compile(r"(?<![\d.,'])(?:\d{1,3}(?:[,.']\d{3})+(?:[.,]\d{1,4})?|\d+(?:[.,]\d{1,4})?)(?![\d])")
_NUM_SPACED = re.compile(r"(?<![\d.,'])\d{1,3}(?:[   ]\d{3})+(?:[.,]\d{1,4})?(?![\d])")
_US = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_EU = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?")


def number_values(s: str) -> set[Decimal]:
    """All readings of a printed number: '1,234' is 1234 (US) or 1.234 (Europe)."""
    s = s.replace(" ", " ").replace(" ", " ")
    out = set()
    us = s.replace(" ", ",").replace("'", ",")
    if _US.fullmatch(us):
        out.add(Decimal(us.replace(",", "")))
    eu = s.replace(" ", ".").replace("'", ".")
    if _EU.fullmatch(eu):
        out.add(Decimal(eu.replace(".", "").replace(",", ".")))
    return out


def to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    s = str(value).strip().replace(" ", "")
    neg = s.startswith(("-", "(", "−")) or s.endswith(")")
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    vals = number_values(s)
    if not vals:
        try:
            vals = {Decimal(s.replace(",", ""))}
        except InvalidOperation:
            return None
    # A stored value is normally '1234.50'; prefer the US reading when both exist.
    us = s.replace(",", "")
    try:
        v = Decimal(us) if Decimal(us) in vals else sorted(vals)[0]
    except InvalidOperation:
        v = sorted(vals)[0]
    return -v if neg else v


def _prefix_ok(before: str) -> tuple[bool, bool]:
    """Text printed before a number inside the same word may only be a currency, a sign or a 'Label:'
    ('USD1,234.50', '$1,234.50', '(1,234.50', 'Total:1,234.50'; not 'PO-2026'). Returns (ok, negative)."""
    s, negative = before.strip(), False
    while s:
        c = s[-1]
        if c in "(-−":
            negative = True
            s = s[:-1].rstrip()
            continue
        if c == "+" or c in CURRENCY_SYMBOLS:
            s = s[:-1].rstrip()
            continue
        m = re.search(r"([A-Za-z]{2,3})$", s)
        if m and m.group(1).upper() in CURRENCY_CODES:
            s = s[:m.start()].rstrip()
            continue
        break
    return (s == "" or s[-1] in ":#="), negative


def _suffix_ok(after: str) -> bool:
    s = after.strip().lstrip(")").strip()
    m = re.match(r"([A-Za-z]{3})", s)
    if m and m.group(1).upper() in CURRENCY_CODES:
        s = s[3:]
    s = s.strip(" .,;:*)" + CURRENCY_SYMBOLS)
    return s == "" or s.upper() in {"CR", "DR"}


def _span_hit(page: PageIndex, line: Line, start: int, end: int, quality: float) -> Hit:
    """Hit for characters [start, end) of a line's text."""
    pa, ca = line.word_at(start)
    pb, cb = line.word_at(end - 1)
    return Hit(page, line.words[pa:pb + 1], ca, cb, quality)


def number_hits(page: PageIndex, value: Any) -> list[Hit]:
    target = to_decimal(value)
    if target is None:
        return []
    hits = {}
    for line in page.lines:
        for rx in (_NUM_PLAIN, _NUM_SPACED):
            for m in rx.finditer(line.text):
                vals = number_values(m.group(0))
                if abs(target) not in vals:
                    continue
                hit = _span_hit(page, line, m.start(), m.end(), 1.0)
                if len(hit.words) > 1:
                    # '1 234,50': the words must sit as close as a thousands space, not in two columns.
                    if any(b.x0 - a.x1 > 1.2 * max(a.char_w, b.char_w) for a, b in zip(hit.words, hit.words[1:])):
                        continue
                first, last = hit.words[0], hit.words[-1]
                ok_before, neg = _prefix_ok(first.text[:hit.first_char])
                if not ok_before or not _suffix_ok(last.text[hit.last_char + 1:]):
                    continue
                neg = neg or last.text[hit.last_char + 1:].strip().startswith(")")
                if line.text[:m.start()].rstrip().endswith(("-", "−", "(")) and len(hit.words) == 1:
                    prev = line.words[first.pos - 1] if first.pos > 0 else None
                    if prev is not None and prev.text in {"-", "−", "("}:
                        neg = True
                hit.negative = neg
                if neg != (target < 0):
                    hit.quality -= 0.15
                hits[(line.index, m.start(), m.end())] = hit
    return list(hits.values())


# ---- dates

_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
        r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], start=1)}
_DATES = [
    (re.compile(r"(?<![\d])(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?![\d])"), "ymd"),
    (re.compile(r"(?<![\d])(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?![\d])"), "either"),
    (re.compile(rf"(?<![\w])(\d{{1,2}})(?:st|nd|rd|th)?[\s\-./]*{_MON}[\s\-./,]*(\d{{4}}|\d{{2}})(?![\d])", re.I), "dmy"),
    (re.compile(rf"(?<![\w]){_MON}[\s\-./]*(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*|[\s\-./]+)(\d{{4}}|\d{{2}})(?![\d])",
                re.I), "mdy"),
    (re.compile(r"(?<![\w])(\d{4})(\d{2})(\d{2})(?![\w])"), "ymd"),
]


def _year(y: str) -> int:
    n = int(y)
    return n if len(y) == 4 else (2000 + n if n < 70 else 1900 + n)


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def date_readings(m: re.Match, kind: str) -> set[date]:
    g = m.groups()
    out = set()
    if kind == "ymd":
        out.add(_mk(int(g[0]), int(g[1]), int(g[2])))
    elif kind == "either":
        y = _year(g[2])
        out.add(_mk(y, int(g[1]), int(g[0])))   # day/month/year
        out.add(_mk(y, int(g[0]), int(g[1])))   # month/day/year
    elif kind == "dmy":
        out.add(_mk(_year(g[2]), _MONTHS[g[1][:3].lower()], int(g[0])))
    elif kind == "mdy":
        out.add(_mk(_year(g[2]), _MONTHS[g[0][:3].lower()], int(g[1])))
    out.discard(None)
    return out


def to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    from .normalize import parse_date

    d = parse_date(value)
    if d:
        return d
    for rx, kind in _DATES:
        m = rx.search(str(value or ""))
        if m:
            readings = date_readings(m, kind)
            if len(readings) == 1:
                return readings.pop()
    return None


def date_hits(page: PageIndex, value: Any) -> list[Hit]:
    target = to_date(value)
    if target is None:
        return []
    hits = {}
    for line in page.lines:
        for rx, kind in _DATES:
            for m in rx.finditer(line.text):
                readings = date_readings(m, kind)
                if target not in readings:
                    continue
                hit = _span_hit(page, line, m.start(), m.end(), 1.0 if len(readings) == 1 else 0.95)
                if all(_gap_ok(a, b) for a, b in zip(hit.words, hit.words[1:])):
                    hits[(line.index, m.start())] = hit
    return list(hits.values())


# --------------------------------------------------------------------------- ranking


def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in patterns]


_LABELS = {k: _compile(v) for k, v in LABELS.items()}
_NEGATIVE = {k: _compile(v) for k, v in NEGATIVE.items()}


def register_labels(name: str, labels: list[str], negative: list[str] = (), header: bool = True) -> None:
    """Labels printed next to another app's field (and labels that mean "not this value"), registered from
    its AppConfig.ready(). header=True prefers the top of the page, like invoice numbers and dates."""
    LABELS[name] = list(labels)
    _LABELS[name] = _compile(LABELS[name])
    if negative:
        NEGATIVE[name] = list(negative)
        _NEGATIVE[name] = _compile(NEGATIVE[name])
    if header:
        HEADER_FIELDS.add(name)


_LIST_TAIL = re.compile(r"[\w\-/.]+\s*[,;&/]\s*$")


def _label_segment(text: str, list_field: bool) -> str:
    """The label part printed just left of a value: the text after the previous value on the line."""
    if list_field:  # skip earlier items of the same list: 'PO: A-1, A-2, <here>'
        for _ in range(12):
            new = _LIST_TAIL.sub("", text)
            if new == text:
                break
            text = new
    digits = [m.end() for m in re.finditer(r"\d", text)]
    seg = text[digits[-1]:] if digits else text
    return seg[-48:].lower()


def _above_text(hit: Hit) -> str:
    """Text printed just above the value (a column or box header)."""
    page, line = hit.page, hit.line
    boxes = hit.boxes()
    x0, x1 = boxes[0][0], boxes[0][2]
    for li in range(line.index - 1, max(-1, line.index - 3), -1):
        above = page.lines[li]
        if line.top - above.bottom > 3 * max(w.h for w in hit.words):
            break
        near = [w.text for w in above.words if w.x1 >= x0 - 0.03 and w.x0 <= x1 + 0.03]
        if near:
            return " ".join(near).lower()
    return ""


def _label_score(hit: Hit, name: str) -> float:
    pos, neg = _LABELS.get(name, []), _NEGATIVE.get(name, [])
    if not pos and not neg:
        return 0.0
    seg = _label_segment(hit.line.text[:hit.line_char], name in LIST_FIELDS)
    if any(r.search(seg) for r in neg):
        return -0.5
    if any(r.search(seg) for r in pos):
        hit.labelled = True
        return 0.6
    above = _above_text(hit)
    if above:
        if any(r.search(above) for r in neg):
            return -0.3
        if any(r.search(above) for r in pos):
            hit.labelled = True
            return 0.35
    return 0.0


def _position_score(index: DocIndex, hit: Hit, name: str) -> float:
    y = hit.top
    if name in TOTAL_FIELDS:
        return 0.3 * y + (0.15 if hit.page.number == index.last_page else 0.0) + (0.1 if hit.bold else 0.0)
    if name in TOP_FIELDS:
        score = 0.5 * (1 - y) if hit.page.number == 1 else -0.3
        if hit.bold:
            score += 0.1
        if hit.page.median_size and hit.size:
            score += min(0.15, max(0.0, hit.size / hit.page.median_size - 1) * 0.3)
        return score
    if name in HEADER_FIELDS:
        return 0.15 * (1 - y) + (0.05 if hit.page.number == 1 else 0.0)
    return 0.05 * (1 - y)


def _rank(index: DocIndex, hits: list[Hit], name: str, claimed: dict) -> list[Hit]:
    for h in hits:
        h.score = h.quality + _label_score(h, name) + _position_score(index, h, name)
        if any(claimed.get(i, name) != name for i in h.ids):
            h.score -= 0.6
    return sorted(hits, key=lambda h: (-h.score, h.key))


def _kind(name: str, value: Any) -> str:
    if name.endswith("_date") or name == "date":
        return "date"
    if name in {"total_amount", "amount", "unit_price", "subtotal"} or name.endswith(("_amount", "_total")):
        return "number"
    from apps.documents.schemas import field_kind  # other typed fields (entered values, last free days ...)

    return field_kind(name)


def candidates(index: DocIndex, name: str, value: Any) -> list[Hit]:
    kind = _kind(name, value)
    hits: list[Hit] = []
    for page in index.pages:
        if kind == "number":
            hits += number_hits(page, value)
        elif kind == "date":
            hits += date_hits(page, value)
        else:
            hits += text_hits(page, value)
    if not hits and kind == "text":
        for page in index.pages:
            hits += block_hits(page, value)
        if not hits and name in FUZZY_FIELDS | {"description"}:
            for page in index.pages:
                hits += fuzzy_hits(page, value)
    return hits


def best_hit(index: DocIndex, name: str, value: Any, claimed: dict) -> Hit | None:
    ranked = _rank(index, candidates(index, name, value), name, claimed)
    return ranked[0] if ranked else None


# --------------------------------------------------------------------------- records


def _record(hit: Hit) -> dict:
    quality = min(1.0, hit.quality + (0.05 if hit.labelled else 0.0))
    return {"page": hit.page.number, "boxes": hit.boxes(), "score": round(max(quality, 0.0), 2)}


def _missing(index: DocIndex) -> str:
    return SCANNED if index.has_image_pages else NOT_FOUND


def _claim(claimed: dict, hit: Hit, name: str) -> None:
    for i in hit.ids:
        claimed.setdefault(i, name)


def _list_key(name: str, value: Any) -> str:
    return norm(value)


def _locate_scalar(index: DocIndex, name: str, value: Any, claimed: dict) -> dict:
    hit = best_hit(index, name, value, claimed)
    if hit is None:
        return {"status": _missing(index)}
    _claim(claimed, hit, name)
    return {"status": FOUND, **_record(hit)}


def _locate_list(index: DocIndex, name: str, values: list, claimed: dict) -> dict:
    items, missing = [], 0
    for v in values:
        if v in (None, ""):
            continue
        hit = best_hit(index, name, v, claimed)
        if hit is None:
            missing += 1
            continue
        _claim(claimed, hit, f"{name}:{_list_key(name, v)}")
        items.append({"key": _list_key(name, v), **_record(hit)})
    if not items:
        return {"status": _missing(index)}
    return {"status": PARTIAL if missing else FOUND, "page": items[0]["page"], "boxes": [],
            "score": min(i["score"] for i in items), "items": items}


def _pick_line(d_hits: list[Hit], a_hits: list[Hit], cursor: tuple, amount_x1: list[float]):
    """Best (description, amount) pair for one line item: both on one printed row (or the amount up
    to two rows below a wrapped description), the earliest after the previous line item."""
    best, best_score = None, None
    for d in d_hits:
        for a in a_hits:
            gap = a.words[0].line - d.words[-1].line
            if a.page is d.page and 0 <= gap <= 2 and a.x0 > d.x0:
                distance = (d.page.number - cursor[0]) * 100 + d.words[0].line - cursor[1]
                score = d.quality + a.quality + (0.5 if gap == 0 else 0.25) - 0.002 * distance
                if best_score is None or score > best_score:
                    best, best_score = (d, a), score
    if best is None and d_hits:
        best = (min(d_hits, key=lambda h: (-h.quality, h.key)), None)
    if best is None and a_hits:  # amount alone: prefer the column the other amounts are printed in
        best = (None, min(a_hits, key=lambda h: (not any(abs(h.boxes()[0][2] - x) < 0.015 for x in amount_x1),
                                                   h.key)))
    return best


def _locate_line_items(index: DocIndex, items: list, claimed: dict) -> dict:
    """Each line's description and amount. Lines are matched in printed order, so two lines with the
    same amount get different boxes; a line found out of order is still placed on an unused row."""
    out, missing = [], 0
    cursor: tuple = (0, -1)   # (page, line) of the previous located line item
    used: set = set()         # word ids already used by a line item
    amount_x1: list[float] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            missing += 1
            continue
        desc, amount = item.get("description"), item.get("amount")
        all_d = [h for h in (candidates(index, "description", desc) if desc else []) if not h.ids & used]
        all_a = [h for h in (candidates(index, "amount", amount) if amount not in (None, "") else [])
                 if not h.ids & used and not any(claimed.get(x, "line") != "line" for x in h.ids)]

        later_d = [h for h in all_d if (h.page.number, h.words[0].line) > cursor]
        later_a = [h for h in all_a if (h.page.number, h.words[0].line) > cursor]
        best = (_pick_line(later_d, later_a, cursor, amount_x1)
                or _pick_line(all_d, all_a, (0, -1), amount_x1))
        if best is None:
            missing += 1
            continue
        d, a = best
        boxes, amount_boxes = [], []
        for h in (d, a):
            if h:
                used |= h.ids
                _claim(claimed, h, "line")
        if d:
            boxes += d.boxes()
        if a:
            amount_boxes = a.boxes()
            boxes += amount_boxes
            amount_x1.append(amount_boxes[0][2])
        present = [h for h in (d, a) if h]
        cursor = max(cursor, (present[-1].page.number, max(h.words[-1].line for h in present)))
        quality = min(h.quality for h in present) - (0.2 if len(present) < 2 else 0.0)
        out.append({"key": str(i), "page": present[0].page.number, "boxes": boxes, "amount": amount_boxes,
                    "score": round(max(quality, 0.0), 2)})
    if not out:
        return {"status": _missing(index)}
    return {"status": PARTIAL if missing else FOUND, "page": out[0]["page"], "boxes": [],
            "score": min(i["score"] for i in out), "items": out}


def locate_values(index: DocIndex, values: dict[str, Any]) -> dict[str, dict | None]:
    """{field name: location or None} for every field. None means there is no value to locate."""
    out: dict[str, dict | None] = {}
    claimed: dict = {}
    from apps.documents.schemas import TABLE_FIELDS

    names = [n for n in FIELD_ORDER if n in values] + [n for n in values if n not in FIELD_ORDER and n not in TABLE_FIELDS]
    names += [n for n in TABLE_FIELDS if n in values]   # tables last: their rows avoid boxes other fields claimed
    for name in names:
        value = values[name]
        if value in (None, "", []) or isinstance(value, dict):
            out[name] = None
            continue
        if not index.has_text:
            loc = {"status": SCANNED}
        elif name == LINE_ITEMS:
            loc = _locate_line_items(index, value, claimed)
        elif name in TABLE_FIELDS:  # other tables: each row by what names it and its amount, like line items
            label_key, amount_key = TABLE_FIELDS[name]
            rows = [{"description": r.get(label_key), "amount": r.get(amount_key) if amount_key else None}
                    if isinstance(r, dict) else r for r in value]
            loc = _locate_line_items(index, rows, claimed)
        elif isinstance(value, list):
            loc = _locate_list(index, name, value, claimed)
        else:
            loc = _locate_scalar(index, name, value, claimed)
        out[name] = {"v": VERSION, "source": index.source if index.has_text else "none", **loc}
    return out


# --------------------------------------------------------------------------- documents


def build_index(doc, pdf_bytes: bytes | None = None, ocr_words: list | None = None) -> DocIndex:
    """Index of the document's words: OCR word boxes for documents read by Textract, else the text layer."""
    from apps.documents.models import OcrLayout

    if ocr_words is None and doc.text_source == "textract":
        layout = OcrLayout.objects.filter(document=doc).first()
        ocr_words = layout.words if layout else None
    if ocr_words:
        return index_ocr(ocr_words, doc.page_count or 0)
    if pdf_bytes is None:
        with doc.file.open("rb") as fh:
            pdf_bytes = fh.read()
    return index_pdf(pdf_bytes)


def locate_document(doc, pdf_bytes: bytes | None = None, ocr_words: list | None = None) -> dict:
    """Locate every extracted field of a document and save the result on the fields.

    Returns counts: {"found": n, "not_found": n, "scanned": n, "partial": n}.
    """
    from apps.documents.models import ExtractedField

    fields = list(ExtractedField.objects.filter(document=doc))
    counts = {FOUND: 0, PARTIAL: 0, NOT_FOUND: 0, SCANNED: 0}
    if not fields:
        return counts
    if any(f.value not in (None, "", []) for f in fields):
        index = build_index(doc, pdf_bytes, ocr_words)
        results = locate_values(index, {f.name: f.value for f in fields})
    else:
        results = {}
    changed = []
    for f in fields:
        loc = results.get(f.name)
        page = loc.get("page") if loc and loc.get("status") in (FOUND, PARTIAL) else None
        if loc:
            counts[loc["status"]] += 1
        if f.location != loc or f.page != page:
            f.location, f.page = loc, page
            changed.append(f)
    if changed:
        ExtractedField.objects.bulk_update(changed, ["location", "page"])
    return counts


def save_ocr_layout(doc, words: list, provider: str = "textract") -> None:
    from apps.documents.models import OcrLayout

    OcrLayout.objects.update_or_create(document=doc, defaults={"words": words, "provider": provider})


def safe_locate(doc, pdf_bytes: bytes | None = None, ocr_words: list | None = None) -> dict | None:
    """locate_document() that never raises: evidence is a convenience, processing must go on."""
    from django.db import transaction

    if ocr_words:
        try:
            with transaction.atomic():
                save_ocr_layout(doc, ocr_words)
        except Exception:
            log.exception("Could not save OCR word positions for document %s", doc.pk)
    try:
        with transaction.atomic():
            return locate_document(doc, pdf_bytes, ocr_words)
    except Exception:
        log.exception("Could not locate field values on the page for document %s", doc.pk)
        return None
