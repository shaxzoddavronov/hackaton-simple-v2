"""Tests for the folder + URL-list crawl strategies.

DB-column harvest is exercised separately because it requires a live
engine — covered by the e2e Postgres test once that fixture is up.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.services.doc_harvest import (
    _displayname_from_url,
    fetch_urls,
    walk_folder,
)


# ── walk_folder ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_walk_folder_yields_matching_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "b.csv").write_text("id\n1\n", encoding="utf-8")
    (tmp_path / "ignore.bin").write_bytes(b"\x00\x01")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.md").write_text("# title", encoding="utf-8")

    seen: list[tuple[str, bytes]] = []
    async for name, data in walk_folder(str(tmp_path), recursive=True):
        seen.append((name, data))

    names = {n for n, _ in seen}
    # ignore.bin is filtered (unsupported extension).
    assert "a.txt" in names
    assert "b.csv" in names
    assert "sub/c.md" in names
    assert all(not n.endswith(".bin") for n in names)


@pytest.mark.asyncio
async def test_walk_folder_non_recursive(tmp_path: Path) -> None:
    (tmp_path / "top.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("y", encoding="utf-8")

    seen: list[str] = []
    async for name, _ in walk_folder(str(tmp_path), recursive=False):
        seen.append(name)

    assert "top.txt" in seen
    assert "nested.txt" not in seen
    assert "sub/nested.txt" not in seen


@pytest.mark.asyncio
async def test_walk_folder_extension_filter(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("x", encoding="utf-8")

    async for name, _ in walk_folder(
        str(tmp_path), recursive=True, extensions=[".md"]
    ):
        # Only .md should come through.
        assert name.endswith(".md")


@pytest.mark.asyncio
async def test_walk_folder_missing_path_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError):
        async for _ in walk_folder(str(bogus)):
            pass


@pytest.mark.asyncio
async def test_walk_folder_not_a_directory(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        async for _ in walk_folder(str(f)):
            pass


# ── fetch_urls ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_urls_happy_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "doc1" in str(request.url):
            return httpx.Response(200, content=b"doc1 contents")
        if "doc2" in str(request.url):
            return httpx.Response(200, content=b"doc2 contents")
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        seen: list[tuple[str, bytes]] = []
        async for name, body in fetch_urls(
            [
                "https://example.com/files/doc1.txt",
                "https://example.com/files/doc2.txt",
            ],
            client=client,
        ):
            seen.append((name, body))
    finally:
        await client.aclose()

    assert len(seen) == 2
    assert {"doc1.txt", "doc2.txt"} == {n for n, _ in seen}


@pytest.mark.asyncio
async def test_fetch_urls_skips_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "exists" in str(request.url):
            return httpx.Response(200, content=b"ok")
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        seen: list[str] = []
        async for name, _ in fetch_urls(
            [
                "https://x.com/missing.pdf",
                "https://x.com/exists.txt",
            ],
            client=client,
        ):
            seen.append(name)
    finally:
        await client.aclose()
    assert seen == ["exists.txt"]


@pytest.mark.asyncio
async def test_fetch_urls_handles_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        async for _ in fetch_urls(["https://x/y"], client=client):
            pytest.fail("should yield nothing on transport error")
    finally:
        await client.aclose()


def test_displayname_from_url() -> None:
    assert (
        _displayname_from_url("https://x.com/path/sales.csv")
        == "sales.csv"
    )
    assert (
        _displayname_from_url("https://x.com/path/sales.csv?token=abc")
        == "sales.csv"
    )
    # URL-encoded spaces decoded.
    assert (
        _displayname_from_url("https://x.com/Q1%20Report.pdf")
        == "Q1 Report.pdf"
    )
