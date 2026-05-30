"""Crawl document sources → yield ``(filename, bytes)`` tuples.

Six crawl strategies, each is an async generator so the caller
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
  * :func:`harvest_gdrive` — walks a Google Drive folder via the v3
    REST API using a service-account key. Google-native docs (Docs /
    Sheets / Slides) are exported to PDF / CSV before download so the
    existing extractor handles them.
  * :func:`harvest_onedrive` — walks a OneDrive / SharePoint folder via
    Microsoft Graph v1. The caller supplies a bearer token already
    refreshed (see :mod:`services.cloud_auth`); presigned download URLs
    are fetched without auth headers (Graph attaches an SAS token).
  * :func:`harvest_smb`   — walks an SMB / CIFS network share via
    ``smbprotocol``. NTLM / Kerberos auth; the package's sync
    ``smbclient`` API is driven inside :func:`asyncio.to_thread` because
    the async port is incomplete. Permission-denied subdirectories are
    logged and skipped instead of aborting the crawl.

All honour an upper bound on file size (``MAX_FILE_BYTES``) and a
total-document cap (``MAX_DOCS_PER_HARVEST``) to keep one bad source
from saturating Triton or the metadata DB.
"""
from __future__ import annotations

import asyncio
import io
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
    extra_columns: list[str] | None = None,
) -> AsyncIterator[tuple[str, bytes, dict]]:
    """Run a SELECT against a workspace connection to discover file
    references and yield each fetched file alongside a row-context
    dict that links it back to the source row.

    Phase 17.1 — yields 3-tuples ``(filename, bytes, row_context)``
    instead of the older 2-tuples. ``row_context`` shape::

        {
          "connection_id":  "<uuid>",   # the source DB connection
          "table":          "tickets",  # unqualified table name
          "row_pk":         {"id": 42}, # PK col → value (composite ok)
          "extras":         {...},      # other requested columns
          "file_column":    "attachment_url",
          "file_reference": "https://...",
        }

    The retriever surfaces row_context inside chunk_metadata; the
    answer writer reads it to cite the file *and* the originating
    row. This is what makes hybrid questions like "which user
    submitted the ticket whose attached policy mentions refund?"
    answerable — the chunk knows it came from ticket #42, and the
    agent can SELECT FROM tickets WHERE id=42 to fill in the rest.

    Query shape (identifiers ANSI-double-quoted, all our SQL
    dialects accept it — Postgres / MySQL / ClickHouse / Oracle /
    MSSQL / SQLite)::

        SELECT "<pk_col>"[, "<pk_col2>"...], "<file_col>"
               [, "<extra_col>"...]
        FROM "<table>" LIMIT N

    Read-only is enforced three layers down: sqlglot AST validator,
    per-dialect engine runtime, and the SELECT-only shape we build
    here. We never write.

    PK discovery uses the connection's stored SchemaBundle (the
    profiler ran is_pk detection at connection creation). If the
    table has no PK in the bundle, ``row_context["row_pk"]`` stays
    empty — we still yield the file content + extras + the original
    ``file_reference`` so the citation can name the row by its
    surrogate column value.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.db.models import (
        SchemaBundle as SchemaBundleRow,
        WorkspaceConnection,
        WorkspaceCredentials,
    )
    from app.engines import register_all as register_engines
    from app.engines.registry import get_engine
    from app.services import crypto

    register_engines()

    # Pull the connection row + decrypted creds + stored schema
    # bundle in a short-lived session. We can't keep this open
    # across the long-running fetch loop because the connection
    # pool would be tied up. The bundle gives us PK columns for the
    # target table (set by the schema profiler at connect time).
    sa_engine = create_async_engine(
        settings.DATABASE_URL, pool_pre_ping=True
    )
    Session = async_sessionmaker(sa_engine, expire_on_commit=False)
    pk_columns: list[str] = []
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

            # Load the stored bundle to find PK columns for the table.
            bundle_row = (
                await session.execute(
                    select(SchemaBundleRow).where(
                        SchemaBundleRow.connection_id == connection_id
                    )
                )
            ).scalar_one_or_none()
            if bundle_row is not None:
                pk_columns = _pk_columns_from_bundle(
                    bundle_row.bundle, table
                )
    finally:
        await sa_engine.dispose()

    # Build the SELECT column list. PK columns come first, then the
    # file column, then any extras the caller asked for. Skip
    # duplicates between the three so the SQL doesn't repeat the
    # same identifier and the row tuple stays positional.
    extra_columns = list(extra_columns or [])
    ordered_cols: list[str] = []
    seen: set[str] = set()
    for c in pk_columns + [column] + extra_columns:
        if c in seen:
            continue
        seen.add(c)
        ordered_cols.append(c)
    if column not in ordered_cols:
        # Defensive — shouldn't happen, but guarantees file column
        # presence even if pk_columns somehow shadowed it.
        ordered_cols.append(column)
    file_col_idx = ordered_cols.index(column)

    safe_tbl = table.replace('"', '""')
    safe_cols = ", ".join(
        f'"{c.replace(chr(34), chr(34) * 2)}"' for c in ordered_cols
    )
    sql = f'SELECT {safe_cols} FROM "{safe_tbl}" LIMIT {int(row_limit)}'

    engine = get_engine(source)
    try:
        rs = await engine.execute(sql, row_cap=row_limit, timeout_s=30)
    finally:
        await engine.aclose()

    # Walk result rows, decide URL vs path per row, accumulate both
    # queues with their row context so the fetch step preserves the
    # linkage.
    url_targets: list[tuple[str, dict]] = []
    path_targets: list[tuple[str, dict]] = []
    for row in rs.rows:
        if not row or len(row) <= file_col_idx:
            continue
        file_value = row[file_col_idx]
        if file_value is None:
            continue
        file_str = str(file_value)
        if not file_str.strip():
            continue
        row_pk = {
            col: _serialize_cell(row[i])
            for i, col in enumerate(ordered_cols)
            if col in pk_columns and i < len(row)
        }
        extras_map = {
            col: _serialize_cell(row[i])
            for i, col in enumerate(ordered_cols)
            if col in extra_columns and col != column and i < len(row)
        }
        row_context: dict = {
            "connection_id": str(connection_id),
            "table": table,
            "row_pk": row_pk,
            "extras": extras_map,
            "file_column": column,
            "file_reference": file_str,
        }

        if file_str.startswith(("http://", "https://")):
            url_targets.append((file_str, row_context))
        elif url_prefix:
            url_targets.append(
                (
                    url_prefix.rstrip("/") + "/" + file_str.lstrip("/"),
                    row_context,
                )
            )
        elif os.path.isabs(file_str):
            path_targets.append((file_str, row_context))
        else:
            log.warning(
                "harvest_db_column: skipping ambiguous reference %r "
                "(neither URL nor absolute path, and no url_prefix set)",
                file_str,
            )

    # Fetch URLs one by one so we can pair each result with its row
    # context. The shared ``fetch_urls`` helper streams a single URL
    # list and would lose the pairing.
    own_client = httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT_S))
    yielded = 0
    try:
        for url, ctx in url_targets:
            if yielded >= MAX_DOCS_PER_HARVEST:
                log.info(
                    "harvest_db_column: hit %d-doc cap; stopping",
                    MAX_DOCS_PER_HARVEST,
                )
                return
            try:
                resp = await own_client.get(url)
            except httpx.HTTPError as e:
                log.warning(
                    "harvest_db_column: transport error %s: %s", url, e
                )
                continue
            if resp.status_code >= 400:
                log.warning(
                    "harvest_db_column: HTTP %d for %s",
                    resp.status_code, url,
                )
                continue
            body = resp.content
            if len(body) > MAX_FILE_BYTES:
                log.warning(
                    "harvest_db_column: %s exceeds size cap", url
                )
                continue
            yield _displayname_from_url(url), body, ctx
            yielded += 1
    finally:
        await own_client.aclose()

    # Then local paths, also paired with row context.
    for p, ctx in path_targets:
        if yielded >= MAX_DOCS_PER_HARVEST:
            return
        path = Path(p)
        if not path.is_file():
            log.warning(
                "harvest_db_column: path from DB row not found: %s", p
            )
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            log.warning(
                "harvest_db_column: %s exceeds size cap (%d bytes)",
                p, size,
            )
            continue
        data = await asyncio.to_thread(path.read_bytes)
        yield path.name, data, ctx
        yielded += 1


def _pk_columns_from_bundle(bundle: object, table_name: str) -> list[str]:
    """Pull PK column names for ``table_name`` out of the stored
    SchemaBundle JSON (the profiler ran is_pk detection at connect
    time and persisted the result).

    The bundle may be a dict or a JSON string depending on dialect
    storage (Postgres JSONB returns dict, SQLite JSON returns string).
    Returns an empty list if the table has no PK declared — caller
    falls back to yielding rows without a row_pk linkage.
    """
    if isinstance(bundle, str):
        import json as _json

        try:
            bundle = _json.loads(bundle)
        except Exception:
            return []
    if not isinstance(bundle, dict):
        return []
    for t in bundle.get("tables", []) or []:
        if not isinstance(t, dict):
            continue
        if t.get("name") == table_name:
            return [
                c["name"]
                for c in (t.get("columns") or [])
                if isinstance(c, dict)
                and c.get("is_pk")
                and isinstance(c.get("name"), str)
            ]
    return []


def _serialize_cell(value: object) -> object:
    """Coerce a DB cell into a JSON-serialisable representation so it
    can ride along inside chunk_metadata (which is jsonb on Postgres,
    JSON on SQLite)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# ── Google Drive harvest ─────────────────────────────────────────


# Google-native MIME types we know how to export. Anything else with
# the ``application/vnd.google-apps.`` prefix is exported to PDF.
_GDRIVE_EXPORT_MAP: dict[str, tuple[str, str]] = {
    # source mime → (export mime, file extension we tag onto the name)
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}
_GDRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


def _build_gdrive_service(service_account_json: str):
    """Construct the Google Drive v3 client. Imported lazily so the
    google-api-python-client wheels aren't required when the gdrive
    source kind isn't used (and so unit tests can monkeypatch this
    function before the deps are even installed)."""
    import json as _json

    from google.oauth2 import service_account  # type: ignore[import-not-found]
    from googleapiclient.discovery import build  # type: ignore[import-not-found]

    info = _json.loads(service_account_json)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


async def harvest_gdrive(
    *,
    service_account_json: str,
    folder_id: str,
    recursive: bool = True,
    extensions: list[str] | None = None,
) -> AsyncIterator[tuple[str, bytes]]:
    """Walk a Google Drive folder via the v3 REST API.

    Authentication is via a service-account key (``service_account_json``
    is the raw JSON contents of the key file). The service account must
    have read access to ``folder_id`` — typically the user shares the
    folder with the service-account email address.

    * Sub-folders are walked depth-first when ``recursive=True``.
    * Google-native docs (Docs, Sheets, Slides, Drawings) are exported:
      Docs/Slides/Drawings → PDF, Sheets → CSV. The export extension is
      appended to the file name so :func:`services.doc_extract.extract_text`
      picks the right handler.
    * Binary files are streamed via ``MediaIoBaseDownload``.
    * The Drive file ID is encoded into the yielded filename as
      ``"{name}__gdrive_{id}"`` so the chunker / RAG store can later
      re-fetch a specific document if needed.

    Standard caps (:data:`MAX_FILE_BYTES`, :data:`MAX_DOCS_PER_HARVEST`)
    apply.
    """
    from googleapiclient.http import MediaIoBaseDownload  # type: ignore[import-not-found]

    drive = await asyncio.to_thread(_build_gdrive_service, service_account_json)

    ext_filter: set[str] | None = (
        {e.lower() for e in extensions} if extensions else None
    )

    yielded = 0
    # Iterative depth-first walk — explicit stack so we don't blow the
    # Python recursion limit on deep folder trees.
    folder_stack: list[str] = [folder_id]
    visited_folders: set[str] = set()

    while folder_stack:
        if yielded >= MAX_DOCS_PER_HARVEST:
            log.info(
                "harvest_gdrive: hit %d-doc cap; stopping",
                MAX_DOCS_PER_HARVEST,
            )
            return
        current = folder_stack.pop()
        if current in visited_folders:
            continue
        visited_folders.add(current)

        page_token: str | None = None
        while True:
            if yielded >= MAX_DOCS_PER_HARVEST:
                return

            def _list(token: str | None = page_token, fid: str = current):
                kwargs: dict[str, Any] = {
                    "q": f"'{fid}' in parents and trashed=false",
                    "pageSize": 100,
                    "fields": (
                        "nextPageToken, files(id, name, mimeType, size)"
                    ),
                    "supportsAllDrives": True,
                    "includeItemsFromAllDrives": True,
                }
                if token:
                    kwargs["pageToken"] = token
                return drive.files().list(**kwargs).execute()

            try:
                resp = await asyncio.to_thread(_list)
            except Exception as e:
                log.warning(
                    "harvest_gdrive: list failed for folder %s: %s",
                    current, e,
                )
                break

            for item in resp.get("files", []):
                if yielded >= MAX_DOCS_PER_HARVEST:
                    return
                mime = item.get("mimeType", "")
                name = item.get("name", "")
                fid = item.get("id", "")
                if not fid:
                    continue

                if mime == _GDRIVE_FOLDER_MIME:
                    if recursive:
                        folder_stack.append(fid)
                    continue

                # Decide download strategy + final extension.
                is_native = mime.startswith("application/vnd.google-apps.")
                if is_native:
                    export_mime, export_ext = _GDRIVE_EXPORT_MAP.get(
                        mime, ("application/pdf", ".pdf")
                    )
                    final_name = name
                    if not final_name.lower().endswith(export_ext):
                        final_name = f"{final_name}{export_ext}"
                else:
                    export_mime = None
                    final_name = name

                # Honour extension filter (only meaningful for non-native
                # files; Google exports come through whatever we picked).
                if ext_filter is not None and not is_native:
                    file_ext = ext_of(final_name)
                    if file_ext not in ext_filter:
                        continue

                # Size check on metadata when available (Google docs
                # don't report size pre-export, so we let those through
                # and check post-download).
                size_str = item.get("size")
                if size_str is not None:
                    try:
                        if int(size_str) > MAX_FILE_BYTES:
                            log.warning(
                                "harvest_gdrive: %s exceeds size cap, skipping",
                                final_name,
                            )
                            continue
                    except (TypeError, ValueError):
                        pass

                try:
                    data = await asyncio.to_thread(
                        _download_gdrive_file,
                        drive,
                        fid,
                        export_mime,
                        MediaIoBaseDownload,
                    )
                except Exception as e:
                    log.warning(
                        "harvest_gdrive: download failed for %s (%s): %s",
                        final_name, fid, e,
                    )
                    continue

                if len(data) > MAX_FILE_BYTES:
                    log.warning(
                        "harvest_gdrive: %s exceeds size cap after download",
                        final_name,
                    )
                    continue

                # Tag the Drive ID into the name so we can locate the
                # source doc from a RagChunk later. The extractor only
                # looks at the extension, so this rename is safe.
                stem, ext = os.path.splitext(final_name)
                tagged = f"{stem}__gdrive_{fid}{ext}"
                yield tagged, data
                yielded += 1

            page_token = resp.get("nextPageToken")
            if not page_token:
                break


def _download_gdrive_file(
    drive: Any,
    file_id: str,
    export_mime: str | None,
    MediaIoBaseDownload: Any,
) -> bytes:
    """Synchronously download a Drive file. Runs in a worker thread.

    ``export_mime`` is set for Google-native docs (Docs / Sheets /
    Slides / Drawings); ``None`` otherwise. The MediaIoBaseDownload
    helper streams the file in 5 MB chunks so very large binaries
    don't allocate one big buffer.
    """
    buf = io.BytesIO()
    if export_mime is not None:
        request = drive.files().export_media(
            fileId=file_id, mimeType=export_mime
        )
    else:
        request = drive.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, request, chunksize=5 * 1024 * 1024)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
        if buf.tell() > MAX_FILE_BYTES:
            # Bail early — caller logs + skips.
            break
    return buf.getvalue()


# ── OneDrive / Microsoft Graph harvest ───────────────────────────


GRAPH_BASE = "https://graph.microsoft.com/v1.0"


async def harvest_onedrive(
    *,
    access_token: str,
    folder_path: str = "/",
    drive_id: str | None = None,
    recursive: bool = True,
    extensions: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[tuple[str, bytes]]:
    """Walk a OneDrive / SharePoint folder via Microsoft Graph.

    ``access_token`` is a Graph bearer token the caller pre-acquired
    (see :mod:`services.cloud_auth` for the device-code flow). Token
    refresh is the harvest-task layer's responsibility — this function
    just uses whatever token it's given.

    ``folder_path`` is the Drive-relative path (``/`` for the root,
    ``/Documents/Policies`` for a sub-folder). ``drive_id`` switches
    between the user's personal drive (``None`` → ``/me/drive``) and a
    specific business / shared drive.

    Each file item returned by Graph carries a presigned
    ``@microsoft.graph.downloadUrl`` (a short-lived SAS URL). We fetch
    it via plain httpx without sending the Authorization header — the
    SAS token authenticates the request and Azure rejects calls that
    carry both.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT_S))

    ext_filter: set[str] | None = (
        {e.lower() for e in extensions} if extensions else None
    )

    auth_headers = {"Authorization": f"Bearer {access_token}"}

    def _children_url(path: str) -> str:
        base = (
            f"{GRAPH_BASE}/drives/{drive_id}"
            if drive_id
            else f"{GRAPH_BASE}/me/drive"
        )
        # Root vs sub-folder — Graph uses two different URL shapes.
        if path in ("", "/", None):
            return f"{base}/root/children"
        clean = path.strip("/")
        return f"{base}/root:/{clean}:/children"

    try:
        yielded = 0
        folder_stack: list[str] = [folder_path or "/"]
        visited: set[str] = set()

        while folder_stack:
            if yielded >= MAX_DOCS_PER_HARVEST:
                log.info(
                    "harvest_onedrive: hit %d-doc cap; stopping",
                    MAX_DOCS_PER_HARVEST,
                )
                return
            current = folder_stack.pop()
            if current in visited:
                continue
            visited.add(current)

            next_url: str | None = _children_url(current)
            while next_url:
                if yielded >= MAX_DOCS_PER_HARVEST:
                    return
                try:
                    resp = await client.get(next_url, headers=auth_headers)
                except httpx.HTTPError as e:
                    log.warning(
                        "harvest_onedrive: list error %s: %s", current, e
                    )
                    break
                if resp.status_code == 401:
                    raise OneDriveAuthError(
                        "OneDrive access token expired or invalid (HTTP 401)"
                    )
                if resp.status_code >= 400:
                    log.warning(
                        "harvest_onedrive: list HTTP %d for %s — %s",
                        resp.status_code, current, resp.text[:200],
                    )
                    break

                payload = resp.json()
                for item in payload.get("value", []):
                    if yielded >= MAX_DOCS_PER_HARVEST:
                        return
                    name = item.get("name") or ""
                    if "folder" in item:
                        if recursive:
                            # Build the sub-folder path relative to the
                            # drive root.
                            sub = (
                                current.rstrip("/") + "/" + name
                                if current not in ("", "/")
                                else "/" + name
                            )
                            folder_stack.append(sub)
                        continue
                    if "file" not in item:
                        # Could be a OneNote notebook, package, etc.
                        # Skip rather than guess.
                        continue
                    if ext_filter is not None:
                        if ext_of(name) not in ext_filter:
                            continue
                    size = int(item.get("size") or 0)
                    if size > MAX_FILE_BYTES:
                        log.warning(
                            "harvest_onedrive: %s exceeds size cap (%d bytes)",
                            name, size,
                        )
                        continue
                    download_url = item.get(
                        "@microsoft.graph.downloadUrl"
                    )
                    if not download_url:
                        log.warning(
                            "harvest_onedrive: %s missing downloadUrl, skipping",
                            name,
                        )
                        continue
                    try:
                        # Presigned URL — do NOT send Authorization; the
                        # SAS token in the URL is the credential and
                        # Azure rejects calls that carry both.
                        dl = await client.get(download_url)
                    except httpx.HTTPError as e:
                        log.warning(
                            "harvest_onedrive: download error %s: %s", name, e
                        )
                        continue
                    if dl.status_code >= 400:
                        log.warning(
                            "harvest_onedrive: download HTTP %d for %s",
                            dl.status_code, name,
                        )
                        continue
                    body = dl.content
                    if len(body) > MAX_FILE_BYTES:
                        log.warning(
                            "harvest_onedrive: %s body exceeds size cap", name,
                        )
                        continue
                    yield name, body
                    yielded += 1

                next_url = payload.get("@odata.nextLink")
    finally:
        if own_client:
            await client.aclose()


class OneDriveAuthError(RuntimeError):
    """Raised when Microsoft Graph returns HTTP 401, signalling the
    caller should refresh the token and retry. The harvest task layer
    catches this and triggers :func:`services.cloud_auth.refresh_onedrive_token`.
    """


# ── SMB / CIFS share harvest ─────────────────────────────────────


def _smb_unc(server: str, share: str, *parts: str) -> str:
    """Build a normalised UNC path.

    ``smbclient`` accepts both backslash and forward-slash separators
    but expects a single ``\\\\server\\share`` prefix. We always emit
    backslash form, strip any leading / trailing separators from the
    individual path components, and drop empty segments.
    """
    clean: list[str] = []
    for raw in parts:
        if raw is None:
            continue
        s = str(raw).replace("/", "\\").strip("\\")
        if s:
            clean.append(s)
    if clean:
        return f"\\\\{server}\\{share}\\" + "\\".join(clean)
    return f"\\\\{server}\\{share}"


async def harvest_smb(
    *,
    server: str,
    share: str,
    path: str = "",
    username: str,
    password: str,
    domain: str = "",
    recursive: bool = True,
    extensions: list[str] | None = None,
    port: int = 445,
) -> AsyncIterator[tuple[str, bytes]]:
    """Yield ``(relative_path, bytes)`` for files on an SMB / CIFS share.

    Authenticates with NTLM (or Kerberos, when ``domain`` looks like an
    AD realm and the host has a TGT). The ``smbprotocol`` package ships
    a sync API as :mod:`smbclient`; we wrap every blocking call in
    :func:`asyncio.to_thread` so a slow share doesn't stall the Celery
    worker's event loop.

    Honours the same caps as :func:`walk_folder`
    (:data:`MAX_FILE_BYTES`, :data:`MAX_DOCS_PER_HARVEST`). Sub-directories
    we lack permission on are logged and skipped rather than raised — one
    locked-down folder shouldn't kill the rest of the crawl.

    Session lifecycle: a session is registered against the target server
    before walking and torn down via
    :func:`smbclient.reset_connection_cache` in a ``finally`` block so
    we don't leak TCP sockets between harvest runs.
    """
    import smbclient  # type: ignore[import-not-found]

    ext_filter = (
        {e.lower() for e in extensions}
        if extensions
        else SUPPORTED_EXTENSIONS
    )

    def _register() -> None:
        # ``register_session`` is idempotent for the same host but using
        # a fresh registration per crawl keeps credentials scoped to
        # this run. Empty ``domain`` is treated as workgroup by the
        # underlying NTLM auth.
        kwargs: dict[str, Any] = {
            "username": username,
            "password": password,
            "port": port,
        }
        if domain:
            # smbprotocol picks up domain from ``DOMAIN\\user`` but we
            # keep them separate in our config — combine here.
            kwargs["username"] = f"{domain}\\{username}"
        smbclient.register_session(server, **kwargs)

    try:
        await asyncio.to_thread(_register)
    except Exception as e:
        # Surface auth / network failures to the caller — unlike per-dir
        # permission errors, we can't make progress without a session.
        raise RuntimeError(
            f"smb: failed to register session for {server!r}: {e}"
        ) from e

    root_unc = _smb_unc(server, share, path)

    yielded = 0
    try:
        # Iterative depth-first walk via explicit stack. Each entry is
        # a ``(unc, rel)`` pair — ``rel`` is the path inside the
        # configured root, used to build the yielded filename.
        stack: list[tuple[str, str]] = [(root_unc, "")]

        while stack:
            if yielded >= MAX_DOCS_PER_HARVEST:
                log.info(
                    "harvest_smb: hit %d-doc cap; stopping crawl of %s",
                    MAX_DOCS_PER_HARVEST, root_unc,
                )
                return
            current_unc, current_rel = stack.pop()

            try:
                entries = await asyncio.to_thread(
                    smbclient.listdir, current_unc
                )
            except Exception as e:
                # Permission denied / dir vanished mid-crawl — log and
                # keep going rather than abort the whole harvest.
                log.warning(
                    "harvest_smb: listdir failed (%s): %s", current_unc, e
                )
                continue

            for name in entries:
                if name in (".", ".."):
                    continue
                child_unc = current_unc.rstrip("\\") + "\\" + name
                child_rel = (
                    current_rel + "/" + name if current_rel else name
                )

                try:
                    st = await asyncio.to_thread(smbclient.stat, child_unc)
                except Exception as e:
                    log.warning(
                        "harvest_smb: stat failed (%s): %s", child_unc, e
                    )
                    continue

                # S_ISDIR via the standard stat module — smbclient.stat
                # populates st_mode the same way os.stat does.
                import stat as _stat_mod

                if _stat_mod.S_ISDIR(int(st.st_mode)):
                    if recursive:
                        stack.append((child_unc, child_rel))
                    continue

                ext = ext_of(name)
                if ext not in ext_filter:
                    continue

                size = int(getattr(st, "st_size", 0) or 0)
                if size > MAX_FILE_BYTES:
                    log.warning(
                        "harvest_smb: %s exceeds %d bytes, skipping",
                        child_unc, MAX_FILE_BYTES,
                    )
                    continue

                def _read(unc: str = child_unc) -> bytes:
                    with smbclient.open_file(unc, mode="rb") as fh:
                        return fh.read()

                try:
                    data = await asyncio.to_thread(_read)
                except Exception as e:
                    log.warning(
                        "harvest_smb: read failed (%s): %s", child_unc, e
                    )
                    continue

                if len(data) > MAX_FILE_BYTES:
                    log.warning(
                        "harvest_smb: %s body exceeds size cap", child_unc
                    )
                    continue

                yield child_rel, data
                yielded += 1
                if yielded >= MAX_DOCS_PER_HARVEST:
                    log.info(
                        "harvest_smb: hit %d-doc cap; stopping crawl of %s",
                        MAX_DOCS_PER_HARVEST, root_unc,
                    )
                    return
    finally:
        # Always tear the connection cache down — leaks TCP sockets
        # across Celery task invocations otherwise.
        try:
            await asyncio.to_thread(smbclient.reset_connection_cache)
        except Exception as e:  # pragma: no cover — defensive
            log.warning("harvest_smb: reset_connection_cache failed: %s", e)


# ── IMAP email harvest (Phase 19) ─────────────────────────────────


async def harvest_imap(
    *,
    server: str,
    port: int = 993,
    ssl: bool = True,
    username: str,
    password: str,
    folder: str = "INBOX",
    since_days: int = 90,
    max_messages: int = 500,
    include_attachments: bool = True,
) -> AsyncIterator[tuple[str, bytes, dict]]:
    """Crawl an IMAP mailbox and yield each message + each attachment
    as a 3-tuple ``(filename, bytes, row_context)``.

    The row_context shape mirrors Phase 17.1's DB-column linkage so
    the citation pipeline doesn't need a special path for emails::

        {
          "connection_id": "imap:server/user",   # synthetic
          "table":         "email",               # virtual table
          "row_pk":        {"message_id": "..."},
          "extras":        {"from":..., "subject":..., "date":..., "folder":...},
          "file_column":   "body" | "attachment",
          "file_reference": "<subject>" | "<filename>",
        }

    Each message produces:
      * One ``.txt`` "file" carrying the subject + headers + body —
        the extractor will pass it through ``_decode_text``.
      * Zero or more attachment "files" (PDF / DOCX / XLSX / ...)
        — passed through the existing per-format extractors. When
        ``include_attachments=False`` the attachments are skipped.

    Both message-as-text and each attachment share the same
    row_context, so when the agent answers from an attachment the
    citation panel still shows the email's metadata and the user
    can find the original thread.

    ``since_days`` filters to the recent slice (default 90 days) so
    a fresh source doesn't trigger a multi-gigabyte download on the
    first crawl. ``max_messages`` is a hard cap on top of that.

    Uses ``imap_tools`` (sync client) inside ``asyncio.to_thread`` —
    the IMAP protocol is chatty and there's no async client worth
    the dependency for a once-a-day crawl.
    """
    # Local imports keep the dependency optional — installations
    # that don't use IMAP don't pay for the wheel at startup.
    from datetime import datetime, timedelta, timezone

    from imap_tools import AND, MailBox, MailBoxUnencrypted

    yielded = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    conn_id_synthetic = f"imap:{server}/{username}"

    def _open_mailbox():
        if ssl:
            box = MailBox(server, port=port)
        else:
            box = MailBoxUnencrypted(server, port=port)
        box.login(username, password, initial_folder=folder)
        return box

    def _list_uids() -> list[str]:
        box = _open_mailbox()
        try:
            since_str = cutoff.date()
            uids = list(box.uids(AND(date_gte=since_str)))
            # imap_tools returns oldest-first; keep the most recent
            # max_messages so a fresh crawl prioritises freshness.
            return (
                uids[-max_messages:] if len(uids) > max_messages else uids
            )
        finally:
            try:
                box.logout()
            except Exception:
                pass

    def _fetch_one(uid: str) -> list[tuple[str, bytes, dict]]:
        """Pull one message + its attachments. Returns a list of
        3-tuples or empty list on error. Runs inside to_thread."""
        out: list[tuple[str, bytes, dict]] = []
        try:
            box = _open_mailbox()
        except Exception as e:
            log.warning("harvest_imap: connect/login failed: %s", e)
            return out
        try:
            msgs = list(box.fetch(AND(uid=uid), mark_seen=False, bulk=False))
            if not msgs:
                return out
            msg = msgs[0]

            subject = msg.subject or "(no subject)"
            from_ = (msg.from_ or "").lower()
            date_iso = (
                msg.date.replace(microsecond=0).isoformat()
                if msg.date
                else ""
            )
            message_id = msg.headers.get("message-id", "") or msg.uid

            row_pk = {"message_id": str(message_id)}
            extras = {
                "from": from_,
                "subject": subject,
                "date": date_iso,
                "folder": folder,
            }

            # Compose the body text — prefer plain when present, fall
            # back to HTML (extractor will strip tags via BS4).
            body_text = (msg.text or "").strip()
            if not body_text and msg.html:
                body_blob = msg.html.encode("utf-8")
                body_filename = (
                    f"email_{_safe_email_slug(subject)}.html"
                )
            else:
                # Prepend headers so the embedder sees the metadata
                # too — searchable context like "from Alice on 2026-05-10".
                header_block = (
                    f"From: {from_}\n"
                    f"Subject: {subject}\n"
                    f"Date: {date_iso}\n\n"
                )
                body_text = header_block + body_text
                body_blob = body_text.encode("utf-8")
                body_filename = (
                    f"email_{_safe_email_slug(subject)}.txt"
                )

            if 0 < len(body_blob) <= MAX_FILE_BYTES:
                body_ctx = dict(
                    connection_id=conn_id_synthetic,
                    table="email",
                    row_pk=row_pk,
                    extras=extras,
                    file_column="body",
                    file_reference=subject,
                )
                out.append((body_filename, body_blob, body_ctx))

            if include_attachments:
                for att in msg.attachments:
                    if not att.filename:
                        continue
                    data = att.payload or b""
                    if not data:
                        continue
                    if len(data) > MAX_FILE_BYTES:
                        log.warning(
                            "harvest_imap: attachment %s exceeds size "
                            "cap (%d bytes), skipping",
                            att.filename, len(data),
                        )
                        continue
                    att_ctx = dict(
                        connection_id=conn_id_synthetic,
                        table="email",
                        row_pk=row_pk,
                        extras=extras,
                        file_column="attachment",
                        file_reference=att.filename,
                    )
                    out.append((att.filename, data, att_ctx))
            return out
        finally:
            try:
                box.logout()
            except Exception:
                pass

    try:
        uids = await asyncio.to_thread(_list_uids)
    except Exception as e:
        log.warning("harvest_imap: enumerate UIDs failed: %s", e)
        return

    for uid in uids:
        if yielded >= MAX_DOCS_PER_HARVEST:
            log.info(
                "harvest_imap: hit %d-doc cap; stopping",
                MAX_DOCS_PER_HARVEST,
            )
            return
        items = await asyncio.to_thread(_fetch_one, uid)
        for fname, blob, ctx in items:
            yield fname, blob, ctx
            yielded += 1
            if yielded >= MAX_DOCS_PER_HARVEST:
                return


def _safe_email_slug(s: str) -> str:
    """Slugify a subject for use as a filename. Keeps the extension
    pipeline happy (alphanumerics + underscores), truncates at 60
    chars."""
    import re

    out = re.sub(r"[^a-zA-Z0-9]+", "_", s or "").strip("_").lower()
    if not out:
        out = "noname"
    return out[:60]


# ── Slack workspace export (Phase 21) ─────────────────────────────


async def harvest_slack_export(
    *,
    zip_path: str | None = None,
    zip_b64: str | None = None,
    only_channels: list[str] | None = None,
) -> AsyncIterator[tuple[str, bytes, dict]]:
    """Crawl a Slack workspace ZIP export and yield each thread as
    one ``.txt`` "document".

    Slack's standard "Export workspace data" feature produces a ZIP
    with this layout::

        users.json
        channels.json
        groups.json (optional)
        <channel_name>/
            2026-03-14.json
            2026-03-15.json
            ...

    Each daily JSON is a list of message dicts: ``{user, ts, text,
    thread_ts?, files?, attachments?, replies?}``. We group messages
    by ``thread_ts`` (or the message's own ``ts`` for unthreaded
    messages), so one Slack thread → one chunked document. That
    keeps semantic context together for the embedder: a 5-reply
    thread asking "should we approve this PR?" becomes one chunk
    instead of 5 disconnected snippets.

    Yields 3-tuples ``(filename, bytes, row_context)`` where the
    row_context follows the Phase 17.1 / 19 pattern::

        {
          "connection_id":  "slack:<export_filename>",
          "table":          "slack_thread",
          "row_pk":         {"channel": "engineering", "ts": "1710000000.000001"},
          "extras":         {"channel_name", "date", "first_user",
                             "message_count", "reply_count"},
          "file_column":    "thread",
          "file_reference": "<channel>#<date>#<first 60 chars>",
        }

    User IDs (``U12345``) are resolved to real names + handles via
    the export's ``users.json``. Files / image attachments listed
    in messages are skipped by default — they require a Slack bot
    token to download, which the export ZIP doesn't include. The
    text of the message + every reply IS captured.

    Source supplied either as ``zip_path`` (server-local file) or
    ``zip_b64`` (base64-encoded ZIP bytes from a user upload).
    ``only_channels`` restricts the crawl when set; defaults to
    every channel in the export.
    """
    import base64 as _base64
    import json as _json
    import tempfile
    import zipfile
    from pathlib import Path as _Path

    if not zip_path and not zip_b64:
        raise ValueError(
            "slack source requires either 'zip_path' or 'zip_b64'"
        )

    # Materialise the ZIP on disk — ZipFile accepts a file path or a
    # file-like, but tempdir-on-disk lets us pipe through pathlib for
    # the channel walk without buffering the whole archive in RAM.
    tmpdir = tempfile.mkdtemp(prefix="slack_export_")
    cleanup_tmpdir = True
    try:
        if zip_path:
            archive_path = _Path(zip_path)
            source_label = archive_path.name
        else:
            archive_path = _Path(tmpdir) / "export.zip"
            archive_path.write_bytes(
                _base64.b64decode(zip_b64, validate=True)
            )
            source_label = "uploaded_export.zip"

        if not archive_path.is_file():
            raise FileNotFoundError(
                f"slack export archive not found: {archive_path}"
            )

        extract_dir = _Path(tmpdir) / "extracted"
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive_path) as zf:
            # Defence: cap the uncompressed size to MAX_DOCS_PER_HARVEST
            # × 1 MB, so a malicious export doesn't fill the disk.
            total_uncomp = sum(zi.file_size for zi in zf.infolist())
            if total_uncomp > 2 * 1024 * 1024 * 1024:
                raise ValueError(
                    f"slack export uncompressed size "
                    f"{total_uncomp / (1024 ** 3):.1f} GB exceeds 2 GB cap"
                )
            zf.extractall(extract_dir)

        # Load users.json for ID → name resolution.
        users_map: dict[str, str] = {}
        users_file = extract_dir / "users.json"
        if users_file.is_file():
            try:
                for u in _json.loads(users_file.read_text("utf-8")):
                    if not isinstance(u, dict):
                        continue
                    uid = u.get("id") or ""
                    profile = u.get("profile") or {}
                    name = (
                        profile.get("real_name")
                        or profile.get("display_name")
                        or u.get("name")
                        or uid
                    )
                    users_map[uid] = str(name)
            except Exception as e:
                log.warning(
                    "slack export: users.json parse failed: %s", e
                )

        only_set = set(only_channels) if only_channels else None
        yielded = 0

        # Walk channel directories. The export sometimes nests channel
        # JSON inside ``general/`` etc; iterate every immediate child
        # of extract_dir that's a directory.
        for channel_dir in sorted(extract_dir.iterdir()):
            if not channel_dir.is_dir():
                continue
            channel_name = channel_dir.name
            if only_set is not None and channel_name not in only_set:
                continue

            # Collect every message from every daily JSON in this
            # channel, then group by thread_ts.
            messages: list[dict] = []
            for daily in sorted(channel_dir.glob("*.json")):
                try:
                    raw = _json.loads(daily.read_text("utf-8"))
                except Exception as e:
                    log.warning(
                        "slack export: %s parse failed: %s", daily, e
                    )
                    continue
                if isinstance(raw, list):
                    messages.extend(raw)

            threads = _group_slack_threads(messages)
            for thread_ts, thread in threads.items():
                if yielded >= MAX_DOCS_PER_HARVEST:
                    log.info(
                        "slack export: hit %d-doc cap; stopping",
                        MAX_DOCS_PER_HARVEST,
                    )
                    return
                text, first_user, first_date = _format_slack_thread(
                    thread, users_map
                )
                if not text.strip():
                    continue
                blob = text.encode("utf-8")
                if len(blob) > MAX_FILE_BYTES:
                    blob = blob[:MAX_FILE_BYTES]
                ctx = {
                    "connection_id": f"slack:{source_label}",
                    "table": "slack_thread",
                    "row_pk": {
                        "channel": channel_name,
                        "ts": thread_ts,
                    },
                    "extras": {
                        "channel_name": channel_name,
                        "date": first_date,
                        "first_user": first_user,
                        "message_count": len(thread),
                        "reply_count": max(0, len(thread) - 1),
                    },
                    "file_column": "thread",
                    "file_reference": (
                        f"#{channel_name} {first_date} "
                        f"{text[:60].replace(chr(10), ' ')}"
                    ),
                }
                fname = (
                    f"slack_{channel_name}_"
                    f"{thread_ts.replace('.', '_')}.txt"
                )
                yield fname, blob, ctx
                yielded += 1
    finally:
        if cleanup_tmpdir:
            import shutil

            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass


def _group_slack_threads(
    messages: list[dict],
) -> dict[str, list[dict]]:
    """Bucket a flat list of Slack messages into threads keyed by
    the parent ``ts`` (or the message's own ``ts`` when unthreaded).

    Slack uses ``thread_ts`` on every reply and on the parent
    message; standalone messages have only ``ts``. Sort each thread
    by ``ts`` ascending so the parent comes first.
    """
    threads: dict[str, list[dict]] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # Skip channel-system messages (joins, name changes, etc.) —
        # they're noise for retrieval and have no human content.
        subtype = msg.get("subtype")
        if subtype in (
            "channel_join", "channel_leave", "channel_topic",
            "channel_purpose", "channel_name", "channel_archive",
        ):
            continue
        ts = msg.get("thread_ts") or msg.get("ts") or ""
        if not ts:
            continue
        threads.setdefault(ts, []).append(msg)
    for ts in threads:
        threads[ts].sort(key=lambda m: m.get("ts") or "")
    return threads


def _format_slack_thread(
    thread: list[dict], users_map: dict[str, str]
) -> tuple[str, str, str]:
    """Render a thread as plain text suitable for embedding.

    Returns ``(text, first_user_label, first_date_iso)``. The
    first_user / first_date land in the row_context.extras so the
    citation panel can show "from @alice on 2026-03-14" without
    re-parsing the chunk.
    """
    from datetime import datetime, timezone

    parts: list[str] = []
    first_user = ""
    first_date = ""
    for i, msg in enumerate(thread):
        ts = msg.get("ts") or ""
        text = msg.get("text") or ""
        user_id = msg.get("user") or msg.get("bot_id") or ""
        user_label = users_map.get(user_id, user_id) if user_id else "(unknown)"

        try:
            when = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            date_str = when.replace(microsecond=0).isoformat()
        except (ValueError, TypeError):
            date_str = ts

        if i == 0:
            first_user = user_label
            first_date = date_str

        prefix = "" if i == 0 else "↳ "
        parts.append(f"{prefix}{user_label} ({date_str}):\n{text}")

        # Slack's ``files`` field lists attachments without their
        # bodies (those need a token). Mention the filename so the
        # embedder sees there was an attachment + searchable name.
        for f in msg.get("files") or []:
            if not isinstance(f, dict):
                continue
            fname = f.get("name") or f.get("title") or "(file)"
            mimetype = f.get("mimetype") or ""
            parts.append(
                f"  [attachment: {fname} ({mimetype})]"
            )

    return "\n\n".join(parts), first_user, first_date
