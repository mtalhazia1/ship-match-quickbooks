"""Recognize what kind of file arrived, from its bytes first and its name second.

A file's content decides its type (a PNG renamed to .pdf is still a PNG). Every rejection says what the
file is and what to do instead, because the person reading it is a bookkeeper, not a developer.
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from django.conf import settings

from apps.documents.services.ingest import RejectedFile

PDF, IMAGE, SPREADSHEET, ARCHIVE = "pdf", "image", "spreadsheet", "archive"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
SHEET_EXTENSIONS = {".xlsx", ".xlsm", ".csv"}
SUPPORTED_EXTENSIONS = {".pdf", ".zip"} | IMAGE_EXTENSIONS | SHEET_EXTENSIONS
# For <input type=file accept=...>: extensions plus media types (some phones only offer the latter).
ACCEPT = ",".join(sorted(SUPPORTED_EXTENSIONS) + [
    "application/pdf", "image/jpeg", "image/png", "image/tiff", "image/webp", "text/csv", "application/zip",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
])
SUPPORTED_TEXT = "PDF, JPG, PNG, TIFF, WebP, XLSX, CSV or ZIP"
SYSTEM_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}


@dataclass(frozen=True)
class Kind:
    kind: str      # pdf | image | spreadsheet | archive
    subtype: str   # pdf | jpeg | png | tiff | webp | xlsx | csv | zip
    label: str     # for people: "JPG image", "Excel workbook"


def extension(name: str) -> str:
    name = (name or "").lower()
    return name[name.rfind("."):] if "." in name else ""


def is_hidden(name: str) -> bool:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return base.startswith(".") or base.lower() in SYSTEM_FILES


def is_supported_name(name: str) -> bool:
    """For folder and mailbox scans that only have a file name: worth trying to import?"""
    return not is_hidden(name) and extension(name) in SUPPORTED_EXTENSIONS


def max_bytes(kind: str) -> int:
    mb = settings.INTAKE_MAX_ARCHIVE_MB if kind == ARCHIVE else settings.INTAKE_MAX_FILE_MB
    return mb * 1024 * 1024


def detect(filename: str, content: bytes) -> Kind:
    """The kind of file, or RejectedFile with a message that says how to fix it."""
    ext = extension(filename)
    if not content:
        raise RejectedFile(f"{filename}: the file is empty. Check the original and send it again.")
    head = content[:16]
    if content.startswith(b"%PDF"):
        return Kind(PDF, "pdf", "PDF")
    if head.startswith(b"\xff\xd8\xff"):
        return Kind(IMAGE, "jpeg", "JPG image")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return Kind(IMAGE, "png", "PNG image")
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return Kind(IMAGE, "tiff", "TIFF image")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return Kind(IMAGE, "webp", "WebP image")
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
        raise RejectedFile(f"{filename}: HEIC photos (the iPhone default) can't be read. Export the photo as JPG, "
                           "or set the camera to Most Compatible, and send it again.")
    if head[:6] in (b"GIF87a", b"GIF89a") or head[:2] == b"BM":
        raise RejectedFile(f"{filename}: this image type isn't supported. Save it as PNG or JPG and send it again.")
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):  # OLE: Office 97-2003 and Outlook .msg
        if ext == ".xls":
            raise RejectedFile(f"{filename}: older Excel files (.xls) can't be read. Open it in Excel, choose "
                               "Save As > Excel Workbook (.xlsx), and send that file.")
        if ext == ".msg":
            raise RejectedFile(f"{filename}: this is a saved Outlook email. Save its attachments and send those.")
        raise RejectedFile(f"{filename}: older Microsoft Office files can't be read. Save it as PDF and send that.")
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return _zip_kind(filename, content)
    if head.startswith(b"Rar!") or head.startswith(b"7z\xbc\xaf\x27\x1c"):
        raise RejectedFile(f"{filename}: RAR and 7-Zip archives can't be opened. Send a ZIP file instead.")
    if ext == ".csv" and _looks_like_text(content):
        return Kind(SPREADSHEET, "csv", "CSV file")
    if ext == ".xls":
        raise RejectedFile(f"{filename}: older Excel files (.xls) can't be read. Open it in Excel, choose "
                           "Save As > Excel Workbook (.xlsx), and send that file.")
    raise RejectedFile(f"{filename}: not a PDF, image, spreadsheet or ZIP file. Send {SUPPORTED_TEXT} files.")


def check_pdf(filename: str, content: bytes) -> None:
    """Refuse a file that only starts like a PDF: damaged or cut short, or protected by a password.

    A file is accepted when either PDF reader can open it, because the two repair damaged files differently and
    the one that reads the pages later is pdfplumber; a password-protected file needs the password, which
    nobody can give a mailbox importer, so it is refused with the way to remove it."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):   # many "protected" PDFs open with an empty password
            raise _PasswordProtected
        if len(reader.pages) > 0:
            return
    except _PasswordProtected:
        raise RejectedFile(f"{filename}: this PDF is protected with a password. Open it, choose Print > Save as PDF "
                           "to make an unprotected copy, and send that.") from None
    except Exception:   # pypdf raises many kinds of errors for broken files; fall back to the other reader
        pass
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            if len(pdf.pages) > 0:
                return
    except Exception as e:
        if type(e).__name__ in ("PDFPasswordIncorrect", "PDFEncryptionError"):
            raise RejectedFile(f"{filename}: this PDF is protected with a password. Open it, choose Print > Save as "
                               "PDF to make an unprotected copy, and send that.") from None
    raise RejectedFile(f"{filename}: this file isn't a readable PDF. It may be damaged or cut short. Open the "
                       "original, save it as PDF again, and send that.")


class _PasswordProtected(Exception):
    pass


def _zip_kind(filename: str, content: bytes) -> Kind:
    """ZIP containers: an Excel workbook, another Office file, or a plain archive of documents."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = set(zf.namelist()[:5000])
            mimetype = b""
            if "mimetype" in names:  # read a few bytes only: the entry itself could be a zip bomb
                with zf.open("mimetype") as fh:
                    mimetype = fh.read(100)
    except (zipfile.BadZipFile, OSError, EOFError, ValueError, NotImplementedError) as e:
        raise RejectedFile(f"{filename}: the ZIP file is damaged and can't be opened ({e}). "
                           "Create the ZIP again and send it.") from e
    if "xl/workbook.xml" in names:
        return Kind(SPREADSHEET, "xlsx", "Excel workbook")
    if "word/document.xml" in names:
        raise RejectedFile(f"{filename}: Word documents can't be read. Save it as PDF and send that.")
    if any(n.startswith("ppt/") for n in names):
        raise RejectedFile(f"{filename}: PowerPoint files can't be read. Save it as PDF and send that.")
    if mimetype.startswith(b"application/vnd.oasis.opendocument.spreadsheet"):
        raise RejectedFile(f"{filename}: OpenDocument spreadsheets can't be read. Save it as Excel Workbook (.xlsx).")
    if mimetype.startswith(b"application/vnd.oasis.opendocument"):
        raise RejectedFile(f"{filename}: OpenDocument files can't be read. Save it as PDF and send that.")
    return Kind(ARCHIVE, "zip", "ZIP archive")


def _looks_like_text(content: bytes) -> bool:
    sample = content[:4096]
    return b"\x00" not in sample


def check_size(filename: str, content: bytes, kind: Kind) -> None:
    limit = max_bytes(kind.kind)
    if len(content) > limit:
        raise RejectedFile(f"{filename}: larger than {limit // (1024 * 1024)} MB. Send it in smaller parts"
                           + (", or as separate files." if kind.kind == ARCHIVE else "."))


def human_size(size) -> str:
    try:
        size = int(size)
    except (TypeError, ValueError):
        return ""
    if size < 1024:
        return f"{size} bytes"
    return f"{size / 1024:,.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:,.1f} MB"


def media_type(kind: str, subtype: str) -> str:
    return {
        "pdf": "application/pdf", "jpeg": "image/jpeg", "png": "image/png", "tiff": "image/tiff",
        "webp": "image/webp", "csv": "text/csv", "zip": "application/zip",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(subtype, "application/octet-stream")


def convert(filename: str, content: bytes, kind: Kind) -> tuple[bytes, dict]:
    """The PDF copy that reviewers see and QuickBooks receives, plus notes on how it was made."""
    if kind.kind == PDF:
        return content, {}
    if kind.kind == IMAGE:
        from .images import image_to_pdf

        pdf, info = image_to_pdf(filename, content)
        return pdf, {"format": kind.label, "subtype": kind.subtype, "image": info}
    if kind.kind == SPREADSHEET:
        from .sheets import spreadsheet_to_pdf

        pdf, info = spreadsheet_to_pdf(filename, content, kind.subtype)
        return pdf, {"format": kind.label, "subtype": kind.subtype, "sheet": info}
    raise RejectedFile(f"{filename}: {kind.label} can't be converted")
