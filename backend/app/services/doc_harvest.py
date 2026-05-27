"""Crawl document sources → yield ``(filename, bytes)`` tuples.

Three crawl strategies, each is an async generator so the caller
(``workers/harvest_task.py``) can stream results without buffering
the whole crawl in memory.

  * :func:`walk_folder`  — walks a local filesystem directory.
    Recursive, extension-filtered. Path is resolved + must stay under
    the configured root to defend against symlink escapes.
  * :func:`fetch_urls`   — explicit list of HTTPS URLs.
    Uses httpx with a short per-request timeout; non-2xx + transport
    errors are logged and skipped (continue crawling).
  * :func:`harvest_db_column` — queries a WorkspaceConnection via its
    engine, treats every column value as either a URL or a local path
    + ``url_prefix``, then fetches each.

All three honour an upper bound on file size (``MAX_FILE_BYTES``) and
a total-document cap (``MAX_DOCS_PER_HARVEST``) to keep one bad source
from saturating Triton or the metadata DB.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from app.services.doc_extract import SUPPORTED_EXTENSIONS, ext_of

log = logging.getLogger(__name__)


MAX_FILE_BYTES = 25 * 1024 * 1024   # 25 MB per file
MAX_DOCS_PER_HARVEST = 500          # hard cap on docs indexed per crawl
HTTP_TIMEOUT_S = 20.0


# ── Folder walking ────────────────────────────────────────────────


async def walk_folder(
    root: str,
    *,
    recursive: bool = True,
    extensions: list[str] | None = None,
) -> AsyncIterator[tuple[str, bytes]]:
    """Yield ``(relative_path, bytes)`` for files under ``root``.

    ``extensions`` is a list like ``[".pdf", ".docx"]``; entries are
    lower-cased and matched against the file's extension. When the
    list is empty / None, every file with a SUPPORTED extension
    (recognised by the extractor) is included.

    Files larger than :data:`MAX_FILE_BYTES` are skipped with a log
    line. Symlinks are followed but the resolved target must live
    under ``root`` — otherwise the entry is skipped (anti-traversal).
    """
    root_path = Path(root).resolve()
    if not root_path.exists():
        raise FileNotFoundError(f"folder source path does not exist: {root}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"folder source path is not a directory: {root}")

    ext_filter = (
        {e.lower() for e in extensions}
        if extensions
        else SUPPORTED_EXTENSIONS
    )

    yielded = 0
    walker = os.walk(root_path) if recursive else _flat_walk(root_path)
    for dirpath, _dirs, files in walker:
        for name in files:
            ext = ext_of(name)
            if ext not in ext_filter:
                continue
            absolute = Path(dirpath) / name
            # Anti-escape: resolve symlinks and check it's still under root.
            try:
                resolved = absolute.resolve()
            except OSError as e:
                log.warning("walk_folder: resolve failed (%s): %s", absolute, e)
                continue
            try:
                resolved.relative_to(root_path)
            except ValueError:
                log.warning(
                    "walk_folder: symlink escapes root, skipping (%s)", resolved
                )
                continue
            try:
                size = resolved.stat().st_size
            except OSError as e:
                log.warning("walk_folder: stat failed (%s): %s", resolved, e)
                continue
            if size > MAX_FILE_BYTES:
                log.warning(
                    "walk_folder: %s exceeds %d bytes, skipping",
                    resolved, MAX_FILE_BYTES,
                )
                continue
            try:
                data = await asyncio.to_thread(resolved.read_bytes)
            except OSError as e:
                log.warning("walk_folder: read failed (%s): %s", resolved, e)
                continue
            rel_name = str(resolved.relative_to(root_path)).replace("\\", "/")
            yield rel_name, data
            yielded += 1
            if yielded >= MAX_DOCS_PER_HARVEST:
                log.info(
                    "walk_folder: hit %d-doc cap; stopping crawl of %s",
                    MAX_DOCS_PER_HARVEST, root_path,
                )
                return


def _flat_walk(root_path: Path):
    """Single-level walk (no recursion). Mirrors os.walk's shape."""
    files = [p.name for p in root_path.iterdir() if p.is_file()]
    yield str(root_path), [], files


# ── URL list fetch ────────────────────────────────────────────────


async def fetch_urls(
    urls: list[str],
    *,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[tuple[str, bytes]]:
    """Fetch each URL and yield ``(displayname, bytes)``.

    Non-2xx responses are logged and skipped. Sourcing display names
    from the URL path (``urlparse``-based) keeps source_keys stable
    across re-crawls.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT_S))

    try:
        yielded = 0
        for url in urls:
            if yielded >= MAX_DOCS_PER_HARVEST:
                log.info("fetch_urls: hit doc cap; stopping")
                return
            try:
                resp = await client.get(url)
            except httpx.HTTPError as e:
                log.warning("fetch_urls: transport error %s: %s", url, e)
                continue
            if resp.status_code >= 400:
                log.warning(
                    "fetch_urls: HTTP %d for %s", resp.status_code, url
                )
                continue
            if int(resp.headers.get("content-length", "0") or 0) > MAX_FILE_BYTES:
                log.warning("fetch_urls: %s exceeds size cap", url)
                continue
            body = resp.content
            if len(body) > MAX_FILE_BYTES:
                log.warning("fetch_urls: %s body exceeds size cap", url)
                continue
            display = _displayname_from_url(url)
            yield display, body
            yielded += 1
    finally:
        if own_client:
            await client.aclose()


def _displayname_from_url(url: str) -> str:
    from urllib.parse import urlparse, unquote

    parsed = urlparse(url)
    path = unquote(parsed.path or "")
    name = path.rsplit("/", 1)[-1] or parsed.netloc or url
    # Strip query string from the name.
    return name.split("?", 1)[0]


# ── DB-column harvest ────────────────────────────────────────────


async def harvest_db_column(
    connection_id: UUID,
    *,
    table: str,
    column: str,
    url_prefix: str | None = None,
    row_limit: int = 1000,
) -> AsyncIterator[tuple[str, bytes]]:
    """Run a SELECT against a workspace connection to discover file
    references, then fetch each one.

    The query is built as ``SELECT "<column>" FROM "<table>" LIMIT N``
    using the engine's existing read-only enforcement path — the
    sqlglot validator rejects anything that isn't a SELECT, and the
    engine's per-dialect runtime layer adds its own read-only
    transaction. Identifiers are quoted with double-quotes (ANSI),
    which all SQL dialects we ship accept.

    Each value becomes a fetch target:
      * Absolute http(s) URL  → fetched via httpx.
      * Filesystem path        → read from disk (must exist on the
        server running the harvester).
      * Anything else, when ``url_prefix`` is set → ``url_prefix`` +
        value is fetched.

    The connection's credentials are decrypted by the caller and
    attached as ``_credentials`` on a SimpleNamespace before
    ``get_engine`` constructs the adapter.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.db.models import WorkspaceConnection, WorkspaceCredentials
    from app.engines import register_all as register_engines
    from app.engines.registry import get_engine
    from app.services import crypto

    register_engines()

    # Pull the connection row + decrypted creds in a short-lived
    # session. We can't keep this open across the long-running fetch
    # loop because the connection pool would be tied up.
    sa_engine = create_async_engine(
        settings.DATABASE_URL, pool_pre_ping=True
    )
    Session = async_sessionmaker(sa_engine, expire_on_commit=False)
    try:
        async with Session() as session:
            conn = await session.get(WorkspaceConnection, connection_id)
            if conn is None:
                raise ValueError(
                    f"db_column source references unknown connection {connection_id}"
                )
            cred_row = (
                await session.execute(
                    select(WorkspaceCredentials).where(
                        WorkspaceCredentials.connection_id == connection_id
                    )
                )
            ).scalar_one_or_none()
            credentials: dict[str, str] = {}
            if cred_row is not None:
                aads: list[bytes | None] = [str(connection_id).encode()]
                aads.append(str(conn.workspace_id).encode())
                raw = crypto.decrypt_with_aads(
                    cred_row.ciphertext,
                    cred_row.nonce,
                    key_version=cred_row.key_version,
                    aads=aads,
                )
                import json as _json

                try:
                    parsed = _json.loads(raw.decode("utf-8"))
                    if isinstance(parsed, dict):
                        credentials = {str(k): str(v) for k, v in parsed.items()}
                except Exception:
                    credentials = {
                        "password": raw.decode("utf-8", errors="replace")
                    }
            from types import SimpleNamespace

            source = SimpleNamespace(
                dialect=conn.dialect,
                connection_meta=dict(conn.connection_meta or {}),
                _credentials=credentials,
                auth_kind=getattr(
                    cred_row, "auth_kind", None
                ) if cred_row else None,
            )
    finally:
        await sa_engine.dispose()

    engine = get_engine(source)
    safe_col = column.replace('"', '""')
    safe_tbl = table.replace('"', '""')
    sql = f'SELECT "{safe_col}" FROM "{safe_tbl}" LIMIT {int(row_limit)}'
    try:
        rs = await engine.execute(sql, row_cap=row_limit, timeout_s=30)
    finally:
        await engine.aclose()

    values: list[str] = []
    for r in rs.rows:
        if not r:
            continue
        v = r[0]
        if v is None:
            continue
        values.append(str(v))

    fetch_urls_acc: list[str] = []
    local_paths: list[str] = []
    for v in values:
        if v.startswith(("http://", "https://")):
            fetch_urls_acc.append(v)
        elif url_prefix:
            fetch_urls_acc.append(
                url_prefix.rstrip("/") + "/" + v.lstrip("/")
            )
        elif os.path.isabs(v):
            local_paths.append(v)
        else:
            # Relative path without url_prefix — ambiguous, skip.
            log.warning(
                "harvest_db_column: skipping ambiguous reference %r "
                "(neither URL nor absolute path, and no url_prefix set)",
                v,
            )

    # Stream URL fetches.
    async for name, body in fetch_urls(fetch_urls_acc):
        yield name, body

    # Then local files.
    yielded = 0
    for p in local_paths:
        path = Path(p)
        if not path.is_file():
            log.warning(
                "harvest_db_column: path from DB row not found: %s", p
            )
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            log.warning(
                "harvest_db_column: %s exceeds size cap (%d bytes)", p, size
            )
            continue
        data = await asyncio.to_thread(path.read_bytes)
        yield path.name, data
        yielded += 1
        if yielded >= MAX_DOCS_PER_HARVEST:
            return
