"""Celery tasks that drive the RAG indexer.

All three entrypoints are sync Celery wrappers around the async services.
Each opens its own SQLAlchemy async session so the task is self-contained.

Failures **don't** poison the workspace; we log and let Celery's normal
retry semantics apply. The agent's retriever path tolerates an empty
index (falls back to BM25), so a transient Triton outage doesn't take
the product down.
"""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.rag.indexer import (
    reindex_api_catalog,
    reindex_connection,
    reindex_document,
)
from app.services.rag.triton_client import TritonUnavailable
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.index_task.run_index_connection",
    bind=True,
    autoretry_for=(TritonUnavailable,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_kwargs={"max_retries": 5},
)
def run_index_connection(self, connection_id: str) -> dict[str, int]:
    """Full reindex for one connection's schema chunks. Called from
    ``profile_task`` on success and from the daily diff job when drift
    is detected."""
    return asyncio.run(_index_connection_async(UUID(connection_id)))


@celery_app.task(
    name="app.workers.index_task.run_index_api_catalog",
    bind=True,
    autoretry_for=(TritonUnavailable,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_kwargs={"max_retries": 5},
)
def run_index_api_catalog(self) -> dict[str, int]:
    """Reindex of QueryMind's own REST routes. Triggered on app deploy /
    daily as a safety net."""
    return asyncio.run(_index_api_catalog_async())


@celery_app.task(
    name="app.workers.index_task.run_index_document",
    bind=True,
    autoretry_for=(TritonUnavailable,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_kwargs={"max_retries": 5},
)
def run_index_document(self, document_id: str) -> dict[str, int]:
    """Reindex a single uploaded document. Enqueued by ``POST /documents``."""
    return asyncio.run(_index_document_async(UUID(document_id)))


async def _index_connection_async(connection_id: UUID) -> dict[str, int]:
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as session:
            report = await reindex_connection(session, connection_id)
        log.info(
            "rag.reindex_connection conn=%s upserted=%d skipped=%d removed=%d",
            connection_id,
            report["upserted"],
            report["skipped"],
            report["removed"],
        )
        return report
    finally:
        await engine.dispose()


async def _index_api_catalog_async() -> dict[str, int]:
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as session:
            report = await reindex_api_catalog(session)
        log.info(
            "rag.reindex_api_catalog upserted=%d skipped=%d removed=%d",
            report["upserted"],
            report["skipped"],
            report["removed"],
        )
        return report
    finally:
        await engine.dispose()


async def _index_document_async(document_id: UUID) -> dict[str, int]:
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as session:
            report = await reindex_document(session, document_id)
        log.info(
            "rag.reindex_document doc=%s upserted=%d",
            document_id,
            report["upserted"],
        )
        return report
    finally:
        await engine.dispose()


__all__ = [
    "run_index_connection",
    "run_index_api_catalog",
    "run_index_document",
]
