"""Dashboards + saved-questions REST API (Phase 26).

Endpoints (all workspace-scoped, owner-only):

  Saved questions:
    POST   /workspaces/{ws}/saved-questions        — star a question
    GET    /workspaces/{ws}/saved-questions        — list, filterable by ?dashboard_id=
    DELETE /workspaces/{ws}/saved-questions/{id}   — un-star
    PATCH  /workspaces/{ws}/saved-questions/{id}   — rename / re-file under dashboard

  Dashboards:
    POST   /workspaces/{ws}/dashboards             — create
    GET    /workspaces/{ws}/dashboards             — list
    GET    /workspaces/{ws}/dashboards/{id}        — detail + its saved questions
    DELETE /workspaces/{ws}/dashboards/{id}        — delete (saved_questions detach)

The dashboard *renders* live by POSTing each saved_question.prompt
through the existing /chat SSE pipeline — no separate execution
path. That keeps the planner / validator / executor / retriever /
citation stack as the single source of truth for "what does this
question produce", and benefits transparently from the Phase 23
query result cache when the dashboard reloads.
"""
from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.workspaces import _get_owned_workspace
from app.db.models import Dashboard, SavedQuestion, User
from app.db.session import get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["dashboards"])


# ── schemas ──────────────────────────────────────────────────────


class SavedQuestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    prompt: str = Field(min_length=1, max_length=4000)
    dashboard_id: UUID | None = None
    connection_id: UUID | None = None


class SavedQuestionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=255)
    dashboard_id: UUID | None = None
    position: int | None = None


class SavedQuestionOut(BaseModel):
    id: str
    workspace_id: str
    dashboard_id: str | None
    connection_id: str | None
    title: str
    prompt: str
    position: int | None
    created_at: datetime


class DashboardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class DashboardOut(BaseModel):
    id: str
    workspace_id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    question_count: int


class DashboardDetail(DashboardOut):
    questions: list[SavedQuestionOut]


# ── helpers ──────────────────────────────────────────────────────


def _sq_out(sq: SavedQuestion) -> SavedQuestionOut:
    return SavedQuestionOut(
        id=str(sq.id),
        workspace_id=str(sq.workspace_id),
        dashboard_id=str(sq.dashboard_id) if sq.dashboard_id else None,
        connection_id=str(sq.connection_id) if sq.connection_id else None,
        title=sq.title,
        prompt=sq.prompt,
        position=sq.position,
        created_at=sq.created_at,
    )


def _dash_out(
    d: Dashboard, question_count: int = 0
) -> DashboardOut:
    return DashboardOut(
        id=str(d.id),
        workspace_id=str(d.workspace_id),
        name=d.name,
        description=d.description,
        created_at=d.created_at,
        updated_at=d.updated_at,
        question_count=question_count,
    )


# ── Saved-question endpoints ─────────────────────────────────────


@router.post(
    "/{workspace_id}/saved-questions",
    response_model=SavedQuestionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_saved_question(
    workspace_id: UUID,
    payload: SavedQuestionCreate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SavedQuestionOut:
    await _get_owned_workspace(session, workspace_id, current_user)
    # If a dashboard_id was supplied, verify it belongs to this
    # workspace before linking — otherwise the user could file a
    # question under a dashboard they don't own.
    if payload.dashboard_id is not None:
        dash = await session.get(Dashboard, payload.dashboard_id)
        if (
            dash is None
            or dash.workspace_id != workspace_id
            or dash.owner_id != current_user.id
        ):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail="dashboard not found in this workspace",
            )
    sq = SavedQuestion(
        owner_id=current_user.id,
        workspace_id=workspace_id,
        dashboard_id=payload.dashboard_id,
        connection_id=payload.connection_id,
        title=payload.title,
        prompt=payload.prompt,
        position=None,
    )
    session.add(sq)
    await session.commit()
    await session.refresh(sq)
    return _sq_out(sq)


@router.get(
    "/{workspace_id}/saved-questions",
    response_model=list[SavedQuestionOut],
)
async def list_saved_questions(
    workspace_id: UUID,
    dashboard_id: UUID | None = None,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[SavedQuestionOut]:
    await _get_owned_workspace(session, workspace_id, current_user)
    stmt = (
        select(SavedQuestion)
        .where(
            SavedQuestion.workspace_id == workspace_id,
            SavedQuestion.owner_id == current_user.id,
        )
        .order_by(
            SavedQuestion.position.nulls_last(),
            SavedQuestion.created_at.desc(),
        )
    )
    if dashboard_id is not None:
        stmt = stmt.where(SavedQuestion.dashboard_id == dashboard_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [_sq_out(sq) for sq in rows]


@router.delete(
    "/{workspace_id}/saved-questions/{question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_saved_question(
    workspace_id: UUID,
    question_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    await _get_owned_workspace(session, workspace_id, current_user)
    sq = await session.get(SavedQuestion, question_id)
    if (
        sq is None
        or sq.workspace_id != workspace_id
        or sq.owner_id != current_user.id
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="saved question not found"
        )
    await session.delete(sq)
    await session.commit()


@router.patch(
    "/{workspace_id}/saved-questions/{question_id}",
    response_model=SavedQuestionOut,
)
async def update_saved_question(
    workspace_id: UUID,
    question_id: UUID,
    payload: SavedQuestionUpdate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SavedQuestionOut:
    await _get_owned_workspace(session, workspace_id, current_user)
    sq = await session.get(SavedQuestion, question_id)
    if (
        sq is None
        or sq.workspace_id != workspace_id
        or sq.owner_id != current_user.id
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="saved question not found"
        )
    if payload.title is not None:
        sq.title = payload.title
    if payload.dashboard_id is not None:
        dash = await session.get(Dashboard, payload.dashboard_id)
        if (
            dash is None
            or dash.workspace_id != workspace_id
            or dash.owner_id != current_user.id
        ):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail="dashboard not found in this workspace",
            )
        sq.dashboard_id = payload.dashboard_id
    if payload.position is not None:
        sq.position = payload.position
    await session.commit()
    await session.refresh(sq)
    return _sq_out(sq)


# ── Dashboard endpoints ──────────────────────────────────────────


@router.post(
    "/{workspace_id}/dashboards",
    response_model=DashboardOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_dashboard(
    workspace_id: UUID,
    payload: DashboardCreate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DashboardOut:
    await _get_owned_workspace(session, workspace_id, current_user)
    d = Dashboard(
        owner_id=current_user.id,
        workspace_id=workspace_id,
        name=payload.name,
        description=payload.description,
    )
    session.add(d)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="a dashboard with that name already exists in this workspace",
        ) from exc
    await session.refresh(d)
    return _dash_out(d, question_count=0)


@router.get(
    "/{workspace_id}/dashboards",
    response_model=list[DashboardOut],
)
async def list_dashboards(
    workspace_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DashboardOut]:
    await _get_owned_workspace(session, workspace_id, current_user)
    rows = (
        await session.execute(
            select(Dashboard)
            .where(
                Dashboard.workspace_id == workspace_id,
                Dashboard.owner_id == current_user.id,
            )
            .order_by(Dashboard.created_at.desc())
        )
    ).scalars().all()
    # Cheap per-dashboard count — could be folded into a single
    # GROUP BY query, but workspaces typically have <50 dashboards
    # so the N+1 is fine.
    out: list[DashboardOut] = []
    for d in rows:
        cnt = (
            await session.execute(
                select(SavedQuestion.id).where(
                    SavedQuestion.dashboard_id == d.id
                )
            )
        ).scalars().all()
        out.append(_dash_out(d, question_count=len(cnt)))
    return out


@router.get(
    "/{workspace_id}/dashboards/{dashboard_id}",
    response_model=DashboardDetail,
)
async def get_dashboard(
    workspace_id: UUID,
    dashboard_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DashboardDetail:
    await _get_owned_workspace(session, workspace_id, current_user)
    d = await session.get(Dashboard, dashboard_id)
    if (
        d is None
        or d.workspace_id != workspace_id
        or d.owner_id != current_user.id
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="dashboard not found"
        )
    questions = (
        await session.execute(
            select(SavedQuestion)
            .where(SavedQuestion.dashboard_id == dashboard_id)
            .order_by(
                SavedQuestion.position.nulls_last(),
                SavedQuestion.created_at.asc(),
            )
        )
    ).scalars().all()
    base = _dash_out(d, question_count=len(questions))
    return DashboardDetail(
        **base.model_dump(),
        questions=[_sq_out(sq) for sq in questions],
    )


@router.delete(
    "/{workspace_id}/dashboards/{dashboard_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_dashboard(
    workspace_id: UUID,
    dashboard_id: UUID,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    await _get_owned_workspace(session, workspace_id, current_user)
    d = await session.get(Dashboard, dashboard_id)
    if (
        d is None
        or d.workspace_id != workspace_id
        or d.owner_id != current_user.id
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="dashboard not found"
        )
    await session.delete(d)
    await session.commit()
