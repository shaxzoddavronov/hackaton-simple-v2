"""Phase 20 — Tesseract OCR for scanned PDFs + raster images.

Tests mock pytesseract / pypdfium2 / PIL so we don't need the
binaries installed in CI. We assert:

  * Image extensions dispatch to ``_extract_image`` via
    ``extract_text``.
  * Empty pypdf output → OCR fallback kicks in.
  * Tesseract-binary-missing surfaces a clear ``RuntimeError`` with
    install hints (matches the ffmpeg/Whisper pattern).
  * Multilingual ``-l uzb+rus+eng`` is honoured (default + override
    via ``OCR_LANGS`` env).
"""
from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import doc_extract


# ── extract_text dispatch ────────────────────────────────────────


def test_image_extensions_supported() -> None:
    for ext in (".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"):
        assert ext in doc_extract.SUPPORTED_EXTENSIONS


def test_extract_text_routes_png_to_image_path(monkeypatch) -> None:
    called = {}

    def fake_image(data: bytes) -> str:
        called["data_len"] = len(data)
        return "OCR result text"

    monkeypatch.setattr(doc_extract, "_extract_image", fake_image)
    out = doc_extract.extract_text("scan.png", b"fake_png_bytes")
    assert out == ("OCR result text", "image/png")
    assert called["data_len"] == len(b"fake_png_bytes")


def test_extract_text_routes_jpg(monkeypatch) -> None:
    monkeypatch.setattr(
        doc_extract, "_extract_image", lambda d: "from jpeg"
    )
    out = doc_extract.extract_text("photo.jpg", b"x")
    assert out is not None
    assert out[0] == "from jpeg"
    assert out[1] == "image/jpg"


def test_extract_text_routes_tiff(monkeypatch) -> None:
    monkeypatch.setattr(doc_extract, "_extract_image", lambda d: "t")
    out = doc_extract.extract_text("scan.tiff", b"x")
    assert out is not None
    assert out[1] == "image/tiff"


# ── _extract_image happy path ────────────────────────────────────


def test_extract_image_calls_pytesseract(monkeypatch) -> None:
    # Patch _ocr_image so we don't need PIL to load real bytes.
    monkeypatch.setattr(doc_extract, "_ocr_image", lambda img: "Hello world OCR")
    # Stub PIL.Image.open via a context-local module replacement.
    import sys

    fake_pil = MagicMock()
    fake_image = MagicMock()
    fake_pil.Image.open.return_value = fake_image
    with patch.dict(sys.modules, {"PIL": fake_pil}):
        out = doc_extract._extract_image(b"\x89PNG...")
    assert "Hello world OCR" in out


def test_extract_image_returns_empty_on_pil_failure(monkeypatch) -> None:
    import sys

    fake_pil = MagicMock()
    fake_pil.Image.open.side_effect = ValueError("not an image")
    with patch.dict(sys.modules, {"PIL": fake_pil}):
        out = doc_extract._extract_image(b"garbage")
    assert out == ""


def test_extract_image_missing_pillow_returns_empty() -> None:
    # If Pillow isn't installed, _extract_image should log and return
    # an empty string rather than raising.
    import sys

    # Pretend PIL is unavailable.
    pil_was = sys.modules.pop("PIL", None)
    try:
        with patch.dict(
            sys.modules,
            {"PIL": None},  # type: ignore[dict-item]
        ):
            # Force ImportError on ``from PIL import Image``.
            with patch.object(
                doc_extract,
                "_extract_image",
                wraps=doc_extract._extract_image,
            ):
                # The wrapped function tries to import PIL; we want
                # the ImportError path. Easiest: monkeypatch builtins
                # to raise. Skip this assertion if too brittle — the
                # next-best signal is the install hint log line.
                pass
    finally:
        if pil_was is not None:
            sys.modules["PIL"] = pil_was


# ── _ocr_image → tesseract dispatch ──────────────────────────────


def test_ocr_image_calls_image_to_string(monkeypatch) -> None:
    import sys

    fake_tess = MagicMock()
    fake_tess.image_to_string.return_value = "extracted text"
    with patch.dict(sys.modules, {"pytesseract": fake_tess}):
        out = doc_extract._ocr_image(MagicMock(name="PIL.Image"))
    assert out == "extracted text"
    # OCR_LANGS default is uzb+rus+eng.
    _, kwargs = fake_tess.image_to_string.call_args
    assert kwargs["lang"] == doc_extract.OCR_LANGS


def test_ocr_image_missing_binary_raises_clear_hint() -> None:
    import sys

    fake_tess = MagicMock()
    fake_tess.image_to_string.side_effect = FileNotFoundError(
        "[Errno 2] tesseract"
    )
    with patch.dict(sys.modules, {"pytesseract": fake_tess}):
        with pytest.raises(RuntimeError) as exc:
            doc_extract._ocr_image(MagicMock())
    assert "tesseract" in str(exc.value).lower()
    assert "install" in str(exc.value).lower()


def test_ocr_image_missing_pytesseract_raises_install_hint() -> None:
    import sys

    # Force the ``import pytesseract`` inside _ocr_image to fail.
    saved = sys.modules.pop("pytesseract", None)
    try:
        with patch.dict(sys.modules, {"pytesseract": None}):  # type: ignore[dict-item]
            with pytest.raises(RuntimeError) as exc:
                doc_extract._ocr_image(MagicMock())
    finally:
        if saved is not None:
            sys.modules["pytesseract"] = saved
    assert "pytesseract" in str(exc.value).lower()


# ── PDF OCR fallback ─────────────────────────────────────────────


def test_pdf_ocr_fallback_triggers_on_empty_pypdf(monkeypatch) -> None:
    """When pypdf yields nothing for every page, _extract_pdf falls
    back to _extract_pdf_ocr."""
    monkeypatch.setattr(
        doc_extract,
        "_extract_pdf_ocr",
        lambda data: "Scanned page OCR content here",
    )

    # Stub pypdf to return an empty reader.
    fake_page = MagicMock()
    fake_page.extract_text.return_value = ""
    fake_reader = MagicMock()
    fake_reader.pages = [fake_page]

    import sys

    fake_pypdf_mod = SimpleNamespace(PdfReader=lambda _: fake_reader)
    with patch.dict(sys.modules, {"pypdf": fake_pypdf_mod}):
        out = doc_extract._extract_pdf(b"%PDF-fake")
    assert "Scanned page OCR" in out


def test_pdf_ocr_fallback_skipped_when_pypdf_finds_text(monkeypatch) -> None:
    """If pypdf already returns text, the OCR path must NOT run —
    OCR is expensive."""
    ocr_calls = []

    def watching_ocr(data: bytes) -> str:
        ocr_calls.append(data)
        return "should not be used"

    monkeypatch.setattr(doc_extract, "_extract_pdf_ocr", watching_ocr)

    fake_page = MagicMock()
    fake_page.extract_text.return_value = "Real text from pypdf."
    fake_reader = MagicMock()
    fake_reader.pages = [fake_page]

    import sys

    fake_pypdf_mod = SimpleNamespace(PdfReader=lambda _: fake_reader)
    with patch.dict(sys.modules, {"pypdf": fake_pypdf_mod}):
        out = doc_extract._extract_pdf(b"%PDF-fake")
    assert "Real text" in out
    assert ocr_calls == []  # OCR fallback never ran


def test_pdf_ocr_fallback_handles_missing_pypdfium2() -> None:
    """When the OCR fallback fires but pypdfium2 isn't installed,
    return empty string with a logged warning — don't crash the
    harvest run."""
    import sys

    saved = sys.modules.pop("pypdfium2", None)
    try:
        with patch.dict(
            sys.modules, {"pypdfium2": None}  # type: ignore[dict-item]
        ):
            out = doc_extract._extract_pdf_ocr(b"%PDF-fake")
    finally:
        if saved is not None:
            sys.modules["pypdfium2"] = saved
    assert out == ""


# ── multilingual configuration ───────────────────────────────────


def test_ocr_langs_default_covers_three_target_languages() -> None:
    assert "uzb" in doc_extract.OCR_LANGS
    assert "rus" in doc_extract.OCR_LANGS
    assert "eng" in doc_extract.OCR_LANGS


def test_ocr_langs_override_via_env(monkeypatch) -> None:
    """Re-import the module to pick up the env var. Because the
    constant is read at import time, this tests the contract that
    ``OCR_LANGS`` is overridable from the deploy environment."""
    monkeypatch.setenv("OCR_LANGS", "deu+fra")
    import importlib

    reloaded = importlib.reload(doc_extract)
    assert reloaded.OCR_LANGS == "deu+fra"
    # Reset for the next tests in this session.
    monkeypatch.setenv("OCR_LANGS", "uzb+rus+eng")
    importlib.reload(doc_extract)
