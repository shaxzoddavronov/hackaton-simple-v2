"""REST endpoints for managing document sources (Phase 14).

A DocSource is a registered crawl target — one of three kinds:
``folder``, ``url_list``, ``db_column`` — that the background harvester
pulls documents from, extracts text, and indexes into the RAG store.

Endpoints (all scoped to a workspace the caller owns):
  * ``POST   /workspaces/{ws}/doc-sources``           create
  * ``GET    /workspaces/{ws}/doc-sources``           list
  * ``DELETE /workspaces/{ws}/doc-sources/{id}``      delete (+ chunks)
  * ``POST   /workspaces/{ws}/doc-sources/{id}/crawl`` trigger harvest

Documents discovered through these sources land in ``rag_chunks`` with
``kind='harvested_doc'``, so the agent's existing RAG retriever picks
them up alongside schema chunks and uploaded user docs. bge-m3 handles
Uzbek, Russian, and English in a shared embedding space, so a Russian
question retrieves Uzbek-language documents and vice versa.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.workspaces import _get_owned_workspace
from app.db.models import DocSource, RagChunk, User
from app.db.session import get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["doc-sources"])


SourceKind = Literal[
    "folder", "url_list", "db_column", "smb", "gdrive", "onedrive",
    "imap", "slack",
]
SourceStatus = Literal["idle", "harvesting", "ready", "error"]


class DocSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    source_kind: SourceKind
    # Per-kind config — validation happens at harvest time so the API
    # accepts forward-compatible shapes. We sanity-check the obvious
    # required keys here so a bad request is rejected synchronously.
    config: dict[str, Any] = Field(default_factory=dict)


class DocSourceOut(BaseModel):
    id: str
    workspace_id: str
    name: str
    source_kind: str
    status: str
    config: dict[str, Any]
    doc_count: int
    last_harvested_at: datetime | None
    last_error: str | None


def _validate_config(kind: str, config: dict[str, Any]) -> None:
    """Reject obviously-malformed configs at API time so the user gets
    immediate feedback instead of a vague Celery error 30 seconds later.

    More thorough validation happens inside the harvester (path existence,
    URL reachability, connection ownership), but the cheap checks live
    here.
    """
    if kind == "folder":
        path = config.get("path")
        if not isinstance(path, str) or not path.strip():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="folder source requires non-empty 'path' in config",
            )
        if "extensions" in config and not isinstance(
            config["extensions"], list
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="folder source 'extensions' must be a list of strings",
            )
    elif kind == "url_list":
        urls = config.get("urls")
        if not isinstance(urls, list) or not urls:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="url_list source requires non-empty 'urls' list",
            )
        for u in urls:
            if not isinstance(u, str) or not u.startswith(
                ("http://", "https://")
            ):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "url_list entries must be http(s) URLs; got "
                        + repr(u)
                    ),
                )
    elif kind == "db_column":
        for key in ("connection_id", "table", "column"):
            if not isinstance(config.get(key), str) or not config[key]:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"db_column source requires non-empty '{key}'",
                )
        try:
            UUID(str(config["connection_id"]))
        except ValueError as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="db_column 'connection_id' must be a valid UUID",
            ) from e
    elif kind == "gdrive":
        if not isinstance(config.get("folder_id"), str) or not config["folder_id"]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="gdrive source requires 'folder_id'",
            )
        if (
            not isinstance(config.get("service_account_json"), str)
            or not config["service_account_json"]
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=(
                    "gdrive source requires 'service_account_json' "
                    "(raw JSON string of a Google service-account key)"
                ),
            )
    elif kind == "onedrive":
        if (
            not isinstance(config.get("access_token"), str)
            or not config["access_token"]
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="onedrive source requires 'access_token'",
            )
        if (
            not isinstance(config.get("client_id"), str)
            or not config["client_id"]
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=(
                    "onedrive source requires 'client_id' for token refresh"
                ),
            )
    elif kind == "imap":
        for key in ("server", "username", "password"):
            if (
                not isinstance(config.get(key), str)
                or not config[key]
            ):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"imap source requires non-empty '{key}'",
                )
        # Port + since_days + max_messages have safe defaults inside
        # the harvester, but if the caller supplies them we sanity-
        # check the types so a bad number doesn't reach the IMAP lib.
        for key in ("port", "since_days", "max_messages"):
            if key in config and not isinstance(config[key], int):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"imap source '{key}' must be an integer",
                )
    elif kind == "slack":
        # Exactly one of zip_b64 / zip_path must be set. zip_b64 is
        # the standard UI path (user uploads the ZIP, frontend
        # base64-encodes it). zip_path is the server-local fast path
        # for an admin who's already dropped the export on disk.
        has_b64 = isinstance(config.get("zip_b64"), str) and config["zip_b64"]
        has_path = isinstance(config.get("zip_path"), str) and config["zip_path"]
        if has_b64 == has_path:  # both true or both false
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=(
                    "slack source requires exactly one of 'zip_b64' "
                    "(upload) or 'zip_path' (server-local)"
                ),
            )
        if "only_channels" in config and not isinstance(
            config["only_channels"], list
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="slack source 'only_channels' must be a list of strings",
            )


def _enqueue_harvest(source_id: str) -> None:
    """Isolated for monkeypatching in unit tests."""
    from app.workers.harvest_task import run_harvest_doc_source

    run_harvest_doc_source.delay(source_id)


@router.post(
    "/{workspace_id}/doc-sources",
    response_model=DocSourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_doc_source(
    workspace_id: UUID,
    payload: DocSourceCreate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocSourceOut:
    await _get_owned_workspace(session, workspace_id, current_user)
    _validate_config(payload.source_kind, payload.config)

    src = DocSource(
        workspace_id=workspace_id,
        name=payload.name,
        source_kind=payload.source_kind,
        config=payload.config,
        status="idle",
    )
    session.add(src)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="a doc source with this name already exists in this workspace",
        ) from exc
    await session.refresh(src)
    return _to_out(src)


@router.get(
    "/{workspace_id}/doc-sources",
    response_model=list[DocSourceOut],
)
async def list_doc_sources(
    workspace_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DocSourceOut]:
    await _get_owned_workspace(session, workspace_id, current_user)
    rows = await session.execute(
        select(DocSource)
        .where(DocSource.workspace_id == workspace_id)
        .order_by(DocSource.created_at.desc())
    )
    return [_to_out(s) for s in rows.scalars().all()]


@router.delete(
    "/{workspace_id}/doc-sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_doc_source(
    workspace_id: UUID,
    source_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    await _get_owned_workspace(session, workspace_id, current_user)
    src = await session.get(DocSource, source_id)
    if src is None or src.workspace_id != workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "doc source not found")
    # Drop the source's RAG chunks first (no FK to cascade).
    from sqlalchemy import delete as sa_delete

    await session.execute(
        sa_delete(RagChunk).where(
            RagChunk.workspace_id == workspace_id,
            RagChunk.kind == "harvested_doc",
            RagChunk.source_key.like(f"docsource:{source_id}:%"),
        )
    )
    await session.delete(src)
    await session.commit()


@router.post(
    "/{workspace_id}/doc-sources/{source_id}/crawl",
    response_model=DocSourceOut,
)
async def crawl_doc_source(
    workspace_id: UUID,
    source_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocSourceOut:
    await _get_owned_workspace(session, workspace_id, current_user)
    src = await session.get(DocSource, source_id)
    if src is None or src.workspace_id != workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "doc source not found")
    src.status = "harvesting"
    src.last_error = None
    await session.commit()
    await session.refresh(src)
    _enqueue_harvest(str(source_id))
    return _to_out(src)


def _to_out(s: DocSource) -> DocSourceOut:
    return DocSourceOut(
        id=str(s.id),
        workspace_id=str(s.workspace_id),
        name=s.name,
        source_kind=s.source_kind,
        status=s.status,
        config=dict(s.config or {}),
        doc_count=int(s.doc_count or 0),
        last_harvested_at=s.last_harvested_at,
        last_error=s.last_error,
    )
