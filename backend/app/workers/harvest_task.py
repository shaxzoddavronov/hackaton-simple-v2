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
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import DocSource
from app.services.doc_extract import extract_text
from app.services.doc_harvest import (
    fetch_urls,
    harvest_db_column,
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
        async def _crawl():
            if kind == "folder":
                path = str(config.get("path") or "")
                exts = config.get("extensions") or None
                recursive = bool(config.get("recursive", True))
                async for fname, data in walk_folder(
                    path, recursive=recursive, extensions=exts,
                ):
                    yield fname, data
            elif kind == "url_list":
                urls = list(config.get("urls") or [])
                if not isinstance(urls, list):
                    raise ValueError("url_list.urls must be a list")
                async for fname, data in fetch_urls(urls):
                    yield fname, data
            elif kind == "db_column":
                conn_id = UUID(str(config["connection_id"]))
                table = str(config["table"])
                column = str(config["column"])
                url_prefix = config.get("url_prefix") or None
                row_limit = int(config.get("row_limit", 1000))
                async for fname, data in harvest_db_column(
                    conn_id,
                    table=table,
                    column=column,
                    url_prefix=url_prefix,
                    row_limit=row_limit,
                ):
                    yield fname, data
            else:
                raise ValueError(f"unknown source_kind {kind!r}")

        async def _extracted():
            async for fname, data in _crawl():
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
                yield fname, text_value

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


__all__ = ["run_harvest_doc_source"]
