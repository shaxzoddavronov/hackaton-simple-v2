"""Extract plain text from common document formats.

Used by the Phase 14 harvester to turn an arbitrary file's bytes into
something we can chunk + embed. The extractor dispatches on the file
extension (lower-cased) and returns ``(text, mime_type)``; ``None`` is
returned for unsupported formats so callers can skip without
exceptions.

Supported:
  * ``.pdf``                — pypdf
  * ``.docx``               — python-docx
  * ``.xlsx``, ``.xlsm``    — openpyxl (read each sheet as CSV-ish text)
  * ``.html``, ``.htm``     — BeautifulSoup, strip <script>/<style>
  * ``.txt``, ``.md``, ``.csv``, ``.tsv``, ``.json``, ``.log``
                            — direct UTF-8 decode (with BOM detection)

The hard cap on extracted text per file is :data:`MAX_TEXT_BYTES`
(default 1 MB). Anything beyond is truncated — embedding a 50-page
PDF as one chunk would blow the model's context anyway, and the
chunker downstream slices the text into 1200-char windows.

Embedding model is bge-m3 which natively supports 100+ languages
including Uzbek, Russian and English — no language-specific
preprocessing needed.
"""
from __future__ import annotations

import io
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

MAX_TEXT_BYTES = 1 * 1024 * 1024  # 1 MB extracted text cap per file


# Extensions we treat as plain UTF-8 text on disk. We sniff the BOM
# (UTF-8 / UTF-16 LE / UTF-16 BE) so files saved by Windows tools
# decode cleanly.
_PLAIN_TEXT_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".log", ".json", ".jsonl", ".ndjson",
    ".yaml", ".yml", ".sql", ".ini", ".cfg", ".conf",
}

# Extensions where we will produce an extraction. Used by the API
# layer to allow-list uploads on the source side.
SUPPORTED_EXTENSIONS = (
    _PLAIN_TEXT_EXTS
    | {".pdf", ".docx", ".xlsx", ".xlsm", ".html", ".htm"}
)


def ext_of(filename: str) -> str:
    """Return the lower-cased extension including the dot. ``''`` if
    the file has no extension."""
    _, ext = os.path.splitext(filename or "")
    return ext.lower()


def extract_text(filename: str, data: bytes) -> Optional[tuple[str, str]]:
    """Return ``(text, mime_type)`` or ``None`` for unsupported files.

    Errors during extraction (corrupt PDF, password-protected DOCX,
    etc.) bubble up as exceptions — the caller decides whether to mark
    the file as failed or retry. We don't silently swallow because
    silent failures would let the index drift from the source.
    """
    ext = ext_of(filename)
    if ext == ".pdf":
        return _extract_pdf(data), "application/pdf"
    if ext == ".docx":
        return _extract_docx(data), (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        )
    if ext in (".xlsx", ".xlsm"):
        return _extract_xlsx(data), (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )
    if ext in (".html", ".htm"):
        return _extract_html(data), "text/html"
    if ext in _PLAIN_TEXT_EXTS:
        return _decode_text(data), "text/plain"
    return None


# ── per-format helpers ────────────────────────────────────────────


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    total = 0
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception as e:
            # Per-page failures shouldn't kill the whole doc; pypdf
            # occasionally trips on malformed fonts / encrypted pages.
            log.warning("pdf page %d extraction failed: %s", i, e)
            continue
        if t:
            parts.append(t)
            total += len(t.encode("utf-8"))
            if total > MAX_TEXT_BYTES:
                break
    return _cap(_join_paragraphs(parts))


def _extract_docx(data: bytes) -> str:
    import docx  # python-docx

    doc = docx.Document(io.BytesIO(data))
    parts: list[str] = []
    # Body paragraphs.
    for p in doc.paragraphs:
        if p.text:
            parts.append(p.text)
    # Tables — flatten each row as a tab-separated line so the chunker
    # keeps cells together.
    for tbl in doc.tables:
        for row in tbl.rows:
            cells = [cell.text or "" for cell in row.cells]
            line = "\t".join(cells).strip()
            if line:
                parts.append(line)
    return _cap(_join_paragraphs(parts))


def _extract_xlsx(data: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts: list[str] = []
    for sheet_name in wb.sheetnames:
        parts.append(f"## Sheet: {sheet_name}")
        ws = wb[sheet_name]
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) if c is not None else "" for c in row]
            line = "\t".join(cells).strip()
            if line:
                parts.append(line)
    return _cap(_join_paragraphs(parts))


def _extract_html(data: bytes) -> str:
    from bs4 import BeautifulSoup

    # html.parser is stdlib; lxml would be faster but we avoid the
    # extra wheel.
    soup = BeautifulSoup(_decode_text(data), "html.parser")
    # Drop script/style and other noise that contributes no semantics.
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse runs of blank lines.
    lines = [ln.strip() for ln in text.splitlines()]
    return _cap("\n".join(ln for ln in lines if ln))


def _decode_text(data: bytes) -> str:
    """Decode bytes as text with simple BOM detection."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace")
    # Try UTF-8 first; fall back to latin-1 which can't fail.
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


# ── helpers ────────────────────────────────────────────────────────


def _join_paragraphs(parts: list[str]) -> str:
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return "\n\n".join(out)


def _cap(text: str) -> str:
    if len(text.encode("utf-8")) <= MAX_TEXT_BYTES:
        return text
    # Cut at a UTF-8 boundary to avoid lone surrogates.
    encoded = text.encode("utf-8")[:MAX_TEXT_BYTES]
    return encoded.decode("utf-8", errors="ignore") + "\n[...truncated]"
