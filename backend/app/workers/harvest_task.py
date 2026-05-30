"""Celery task that runs a single DocSource crawl end-to-end.

Orchestration:
  1. Load the ``DocSource`` row from the metadata DB.
  2. Mark ``status='harvesting'``.
  3. Pick the crawl strategy based on ``source_kind`` and produce a
     stream of ``(filename, bytes)`` tuples.
  4. For each, extract text via :mod:`services.doc_extract`. Skip
     files the extractor doesn't recognise.
  5. Embed + upsert via :func:`reindex_harvested_source`.
  6. Stamp ``last_harvested_at``, ``doc_count``, ``status='ready'``
     (or ``'error'`` with ``last_error`` on failure).

Errors don't poison the workspace — the source row carries the error
message, the agent's retriever keeps working with whatever chunks are
already indexed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import DocSource
from app.services.doc_extract import extract_text
from app.services.doc_harvest import (
    OneDriveAuthError,
    fetch_urls,
    harvest_db_column,
    harvest_gdrive,
    harvest_imap,
    harvest_onedrive,
    harvest_slack_export,
    harvest_smb,
    harvest_telegram_export,
    walk_folder,
)
from app.services.rag.indexer import reindex_harvested_source
from app.services.rag.triton_client import TritonUnavailable
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.harvest_task.run_harvest_doc_source",
    bind=True,
    autoretry_for=(TritonUnavailable,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_kwargs={"max_retries": 3},
)
def run_harvest_doc_source(self, source_id: str) -> dict[str, int]:
    """Crawl one DocSource. Sync Celery wrapper around the async work."""
    return asyncio.run(_harvest_async(UUID(source_id)))


async def _harvest_async(source_id: UUID) -> dict[str, int]:
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as session:
            source = await session.get(DocSource, source_id)
            if source is None:
                log.warning("harvest: DocSource %s not found", source_id)
                return {"docs": 0}
            workspace_id = source.workspace_id
            kind = source.source_kind
            config = dict(source.config or {})

            source.status = "harvesting"
            source.last_error = None
            await session.commit()

        # Crawl happens OUTSIDE the session because some sources fetch
        # for minutes and we don't want the session/connection held.
        #
        # Yields uniform 3-tuples ``(filename, bytes, row_context)``
        # where ``row_context`` is None for every source kind except
        # ``db_column``. Phase 17.1 added row-linkage to db_column so
        # the agent can cite the originating DB row alongside the
        # file content — this dispatch normalises the shape so
        # _extracted() doesn't have to branch.
        async def _crawl():
            if kind == "folder":
                path = str(config.get("path") or "")
                exts = config.get("extensions") or None
                recursive = bool(config.get("recursive", True))
                async for fname, data in walk_folder(
                    path, recursive=recursive, extensions=exts,
                ):
                    yield fname, data, None
            elif kind == "url_list":
                urls = list(config.get("urls") or [])
                if not isinstance(urls, list):
                    raise ValueError("url_list.urls must be a list")
                async for fname, data in fetch_urls(urls):
                    yield fname, data, None
            elif kind == "db_column":
                conn_id = UUID(str(config["connection_id"]))
                table = str(config["table"])
                column = str(config["column"])
                url_prefix = config.get("url_prefix") or None
                row_limit = int(config.get("row_limit", 1000))
                extra_columns = config.get("extra_columns") or None
                async for fname, data, ctx in harvest_db_column(
                    conn_id,
                    table=table,
                    column=column,
                    url_prefix=url_prefix,
                    row_limit=row_limit,
                    extra_columns=extra_columns,
                ):
                    yield fname, data, ctx
            elif kind == "gdrive":
                folder_id = str(config["folder_id"])
                sa_json = str(config["service_account_json"])
                exts = config.get("extensions") or None
                recursive = bool(config.get("recursive", True))
                async for fname, data in harvest_gdrive(
                    service_account_json=sa_json,
                    folder_id=folder_id,
                    recursive=recursive,
                    extensions=exts,
                ):
                    yield fname, data, None
            elif kind == "onedrive":
                # Token refresh is persisted back into config inside
                # _onedrive_with_refresh so the next harvest finds a
                # fresh token.
                async for fname, data in _onedrive_with_refresh(
                    source_id, config, Session,
                ):
                    yield fname, data, None
            elif kind == "smb":
                server = str(config["server"])
                share = str(config["share"])
                path = str(config.get("path") or "")
                username = str(config.get("username") or "")
                password = str(config.get("password") or "")
                domain = str(config.get("domain") or "")
                recursive = bool(config.get("recursive", True))
                extensions = config.get("extensions") or None
                port = int(config.get("port") or 445)
                async for fname, data in harvest_smb(
                    server=server,
                    share=share,
                    path=path,
                    username=username,
                    password=password,
                    domain=domain,
                    recursive=recursive,
                    extensions=extensions,
                    port=port,
                ):
                    yield fname, data, None
            elif kind == "slack":
                # Slack export ZIP. Source supplies either ``zip_b64``
                # (user upload) or ``zip_path`` (server-local). Both
                # yield natively as 3-tuples with thread-scoped
                # row_context.
                only_channels = config.get("only_channels") or None
                async for fname, data, ctx in harvest_slack_export(
                    zip_path=config.get("zip_path") or None,
                    zip_b64=config.get("zip_b64") or None,
                    only_channels=only_channels,
                ):
                    yield fname, data, ctx
            elif kind == "telegram":
                # Telegram Desktop chat export (result.json). One
                # yield per chat-day with row_context tying chunks
                # back to chat_id + date.
                async for fname, data, ctx in harvest_telegram_export(
                    json_path=config.get("json_path") or None,
                    json_b64=config.get("json_b64") or None,
                    group_by_day=bool(config.get("group_by_day", True)),
                ):
                    yield fname, data, ctx
            elif kind == "imap":
                # IMAP harvester yields 3-tuples natively — each email
                # body + attachment carries a synthetic row_context
                # (table="email", row_pk={message_id:...}) so citations
                # link the chunk back to the originating message,
                # mirroring the Phase 17.1 db_column linkage.
                imap_server = str(config["server"])
                imap_port = int(config.get("port") or 993)
                imap_ssl = bool(config.get("ssl", True))
                imap_user = str(config["username"])
                imap_pass = str(config["password"])
                imap_folder = str(config.get("folder") or "INBOX")
                imap_since_days = int(config.get("since_days", 90))
                imap_max = int(config.get("max_messages", 500))
                imap_atts = bool(config.get("include_attachments", True))
                async for fname, data, ctx in harvest_imap(
                    server=imap_server,
                    port=imap_port,
                    ssl=imap_ssl,
                    username=imap_user,
                    password=imap_pass,
                    folder=imap_folder,
                    since_days=imap_since_days,
                    max_messages=imap_max,
                    include_attachments=imap_atts,
                ):
                    yield fname, data, ctx
            else:
                raise ValueError(f"unknown source_kind {kind!r}")

        async def _extracted():
            async for fname, data, ctx in _crawl():
                try:
                    extracted = extract_text(fname, data)
                except Exception as e:
                    log.warning(
                        "harvest: extraction failed for %s: %s", fname, e
                    )
                    continue
                if extracted is None:
                    continue
                text_value, _mime = extracted
                if not text_value.strip():
                    continue
                # Indexer accepts both 2-tuples (fname, text) and
                # 3-tuples (fname, text, extra_metadata). We yield the
                # 3-form when row_context is present (db_column) so the
                # chunk preserves the DB-row linkage; otherwise the
                # 2-form keeps the existing source kinds unchanged.
                if ctx is None:
                    yield fname, text_value
                else:
                    yield fname, text_value, ctx

        try:
            async with Session() as session:
                report = await reindex_harvested_source(
                    session,
                    source_id=source_id,
                    workspace_id=workspace_id,
                    files_iter=_extracted(),
                )

            async with Session() as session:
                source = await session.get(DocSource, source_id)
                if source is not None:
                    source.status = "ready"
                    source.doc_count = int(report.get("docs", 0))
                    source.last_harvested_at = datetime.now(timezone.utc)
                    source.last_error = None
                    await session.commit()
            log.info(
                "harvest: source=%s docs=%d upserted=%d",
                source_id,
                report.get("docs", 0),
                report.get("upserted", 0),
            )
            return report

        except TritonUnavailable:
            # Surface the retry to Celery — embeddings are required.
            async with Session() as session:
                source = await session.get(DocSource, source_id)
                if source is not None:
                    source.status = "error"
                    source.last_error = "Triton embedding service unavailable"
                    await session.commit()
            raise

        except Exception as e:
            log.exception("harvest: crawl failed for source=%s", source_id)
            async with Session() as session:
                source = await session.get(DocSource, source_id)
                if source is not None:
                    source.status = "error"
                    source.last_error = str(e)[:1000]
                    await session.commit()
            return {"docs": 0, "error": str(e)[:300]}

    finally:
        await engine.dispose()


async def _onedrive_with_refresh(
    source_id: UUID,
    config: dict[str, Any],
    Session: Any,
):
    """Harvest from OneDrive, refreshing the access token when needed.

    The token lives in ``DocSource.config`` as ``access_token`` +
    ``refresh_token`` + ``expires_at`` (UTC ISO 8601 string). Before we
    crawl, if ``expires_at`` is within the next 60 seconds we
    pre-emptively refresh; if Graph still returns 401 mid-crawl we
    catch :class:`OneDriveAuthError`, refresh once, persist, and
    retry. Refreshed tokens are written back through ``Session`` so
    subsequent harvests find them.
    """
    from app.db.models import DocSource
    from app.services.cloud_auth import refresh_onedrive_token

    client_id = str(config.get("client_id") or "")
    access_token = str(config.get("access_token") or "")
    refresh_token = str(config.get("refresh_token") or "")
    tenant = str(config.get("tenant") or "common")
    expires_at_raw = config.get("expires_at")

    # Pre-emptive refresh if we know the token's about to die.
    if expires_at_raw and refresh_token and client_id:
        try:
            expires_at = datetime.fromisoformat(str(expires_at_raw))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc) + timedelta(seconds=60):
                new = await refresh_onedrive_token(
                    client_id, refresh_token, tenant=tenant,
                )
                access_token = new.get("access_token") or access_token
                refresh_token = new.get("refresh_token") or refresh_token
                await _persist_onedrive_tokens(
                    Session, source_id, access_token, refresh_token,
                    int(new.get("expires_in") or 3600),
                )
        except Exception as e:
            log.warning(
                "harvest_onedrive: pre-emptive refresh skipped: %s", e,
            )

    folder_path = str(config.get("folder_path") or "/")
    drive_id = config.get("drive_id") or None
    recursive = bool(config.get("recursive", True))
    exts = config.get("extensions") or None

    async def _stream(token: str):
        async for fname, data in harvest_onedrive(
            access_token=token,
            folder_path=folder_path,
            drive_id=drive_id,
            recursive=recursive,
            extensions=exts,
        ):
            yield fname, data

    try:
        async for fname, data in _stream(access_token):
            yield fname, data
    except OneDriveAuthError:
        # Reactive refresh — token expired mid-crawl. Do it once.
        if not (refresh_token and client_id):
            raise
        log.info("harvest_onedrive: 401 mid-crawl, refreshing token")
        new = await refresh_onedrive_token(
            client_id, refresh_token, tenant=tenant,
        )
        access_token = new.get("access_token") or access_token
        refresh_token = new.get("refresh_token") or refresh_token
        await _persist_onedrive_tokens(
            Session, source_id, access_token, refresh_token,
            int(new.get("expires_in") or 3600),
        )
        async for fname, data in _stream(access_token):
            yield fname, data


async def _persist_onedrive_tokens(
    Session: Any,
    source_id: UUID,
    access_token: str,
    refresh_token: str,
    expires_in: int,
) -> None:
    """Write refreshed OneDrive tokens back into DocSource.config.

    Microsoft rotates the refresh_token on each refresh, so the old
    one stops working — persistence is mandatory, not optional.
    """
    from app.db.models import DocSource

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    async with Session() as session:
        src = await session.get(DocSource, source_id)
        if src is None:
            return
        cfg = dict(src.config or {})
        cfg["access_token"] = access_token
        cfg["refresh_token"] = refresh_token
        cfg["expires_at"] = expires_at.isoformat()
        src.config = cfg
        await session.commit()


@celery_app.task(
    name="app.workers.harvest_task.run_daily_doc_recrawl",
    bind=True,
)
def run_daily_doc_recrawl(self) -> dict[str, int]:
    """Enumerate every registered DocSource and enqueue a fresh crawl.

    Phase 15: keeps mount / OneDrive / DB-column reference indexes
    fresh without users clicking 'Crawl now'. Skips sources currently
    in ``status='harvesting'`` so we don't pile up duplicate jobs.

    Returns ``{"enqueued": N, "skipped_active": M}`` for observability.
    """
    return asyncio.run(_daily_recrawl_async())


async def _daily_recrawl_async() -> dict[str, int]:
    from sqlalchemy import select

    from app.db.models import DocSource

    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    enqueued = 0
    skipped = 0
    try:
        async with Session() as session:
            rows = await session.execute(select(DocSource))
            for src in rows.scalars().all():
                if src.status == "harvesting":
                    skipped += 1
                    continue
                # Re-enqueue via the same .delay() entrypoint the API
                # uses. Decoupling the enumeration from execution means
                # Celery worker concurrency caps the actual work.
                run_harvest_doc_source.delay(str(src.id))
                enqueued += 1
    finally:
        await engine.dispose()
    log.info(
        "doc-sources-daily-recrawl: enqueued=%d skipped_active=%d",
        enqueued,
        skipped,
    )
    return {"enqueued": enqueued, "skipped_active": skipped}


__all__ = ["run_harvest_doc_source", "run_daily_doc_recrawl"]
