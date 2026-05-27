"""Unit tests for the data-file upload route's helpers.

The wire-level integration (multipart POST → file on disk → connection
in DB) is covered by the e2e Postgres test once Postgres is reachable;
here we lock in the small pure helpers that gate the upload.
"""
from __future__ import annotations

import pytest

from app.api.data_files import _ALLOWED_EXTENSIONS, _slug, _split_ext


def test_split_ext_basic() -> None:
    assert _split_ext("sales.csv") == ("sales", ".csv")
    assert _split_ext("sales.CSV") == ("sales", ".csv")
    assert _split_ext("/abs/path/sales.csv") == ("sales", ".csv")


def test_split_ext_multi_suffix_keeps_last() -> None:
    # We don't auto-decompress, so json.gz keeps only `.gz` — which is
    # not in _ALLOWED_EXTENSIONS so the route will reject it. Correct.
    assert _split_ext("payload.json.gz") == ("payload.json", ".gz")


def test_split_ext_no_extension() -> None:
    assert _split_ext("README") == ("README", "")


def test_split_ext_empty() -> None:
    assert _split_ext("") == ("", "")
    assert _split_ext(None) == ("", "")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sales", "sales"),
        ("Sales 2024", "sales_2024"),
        ("Sales/2024.q1", "sales_2024_q1"),
        ("   leading and trailing   ", "leading_and_trailing"),
        ("$$$$", "data"),
        ("", "data"),
        ("multi___underscores", "multi_underscores"),
    ],
)
def test_slug(raw: str, expected: str) -> None:
    assert _slug(raw) == expected


def test_slug_truncates_to_63_chars() -> None:
    out = _slug("a" * 200)
    assert len(out) == 63
    assert out == "a" * 63


def test_allowed_extensions_cover_engine_loaders() -> None:
    """Every extension listed in the route's allow-list must be
    understood by the DuckDB engine's loader lookup. Drift between
    these two lists would let users upload files we can't actually
    introspect."""
    from app.engines.duckdb import _loader_for

    for ext in _ALLOWED_EXTENSIONS:
        # Fabricate a path with that extension and check the loader is
        # not None — proves the engine and the route agree.
        assert _loader_for(f"x{ext}") is not None
