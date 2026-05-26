"""REST API for the RAG document store.

Two endpoints:

  - ``POST /documents``  — upload a markdown/plain-text doc. Persists the
    raw body and enqueues a Celery task to chunk + embed it.
  - ``GET  /documents``  — list the calling user's uploaded docs.
  - ``DELETE /documents/{id}`` — remove a doc and its chunks.

Workspace scope: a doc may be linked to a workspace (``workspace_id``)
so retrieval can prefer it for that workspace's questions. Global docs
(``workspace_id=null``) match every workspace.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import UploadedDocument, User, Workspace
from app.db.session import get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])


class DocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(default="text/markdown", max_length=64)
    body: str = Field(min_length=1, max_length=400_000)
    workspace_id: UUID | None = None


class DocumentOut(BaseModel):
    id: UUID
    title: str
    mime_type: str
    workspace_id: UUID | None
    created_at: Any


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=DocumentOut)
async def create_document(
    payload: DocumentCreate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentOut:
    # If a workspace is named, verify ownership before storing the doc.
    if payload.workspace_id is not None:
        ws = await session.get(Workspace, payload.workspace_id)
        if ws is None or ws.owner_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
            )

    doc = UploadedDocument(
        owner_id=current_user.id,
        workspace_id=payload.workspace_id,
        title=payload.title,
        mime_type=payload.mime_type,
        body=payload.body,
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    # Enqueue chunk + embed. Local import keeps the API module independent
    # from Celery at import time (so unit tests don't need Redis).
    try:
        from app.workers.index_task import run_index_document

        run_index_document.delay(str(doc.id))
    except Exception:
        log.exception("failed to enqueue document index task (doc=%s)", doc.id)

    return DocumentOut(
        id=doc.id,
        title=doc.title,
        mime_type=doc.mime_type,
        workspace_id=doc.workspace_id,
        created_at=doc.created_at,
    )


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DocumentOut]:
    rows = await session.execute(
        select(UploadedDocument)
        .where(UploadedDocument.owner_id == current_user.id)
        .order_by(UploadedDocument.created_at.desc())
    )
    return [
        DocumentOut(
            id=d.id,
            title=d.title,
            mime_type=d.mime_type,
            workspace_id=d.workspace_id,
            created_at=d.created_at,
        )
        for d in rows.scalars().all()
    ]


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    doc = await session.get(UploadedDocument, document_id)
    if doc is None or doc.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="document not found"
        )
    await session.delete(doc)
    await session.commit()
    # rag_chunks cascade via document_id FK.
