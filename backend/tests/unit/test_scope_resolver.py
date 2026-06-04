"""Phase 42 — chat-scope → connection-id resolver."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.services.scope_resolver import resolve_scope


# ── fixture ─────────────────────────────────────────────────────


@pytest.fixture
async def session() -> AsyncSession:
    """Hand-rolled SQLite schema for connections + clusters — the
    project pattern (see test_auth_tokens.py) for fixtures that
    avoid Postgres-only server defaults."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE workspace_connections ("
            "id TEXT PRIMARY KEY, "
            "workspace_id TEXT NOT NULL, "
            "cluster_id TEXT, "
            "name TEXT, dialect TEXT, "
            "status TEXT NOT NULL DEFAULT 'pending', "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _seed_conn(
    session: AsyncSession,
    *,
    workspace_id: str,
    cluster_id: str | None = None,
    status: str = "ready",
) -> str:
    cid = str(uuid4())
    await session.execute(
        sa_text(
            "INSERT INTO workspace_connections "
            "(id, workspace_id, cluster_id, status) "
            "VALUES (:id, :w, :c, :s)"
        ),
        {"id": cid, "w": workspace_id, "c": cluster_id, "s": status},
    )
    await session.commit()
    return cid


# ── table / database (the narrow scopes) ────────────────────────


@pytest.mark.asyncio
async def test_database_scope_returns_active_connection(session) -> None:
    ws = uuid4()
    cid = await _seed_conn(session, workspace_id=str(ws))
    from uuid import UUID

    r = await resolve_scope(
        session,
        workspace_id=ws,
        scope="database",
        active_connection_id=UUID(cid),
        scope_cluster_id=None,
    )
    assert r.connection_ids == [UUID(cid)]
    assert r.federation is False
    assert r.error is None


@pytest.mark.asyncio
async def test_table_scope_same_as_database_at_connection_level(
    session,
) -> None:
    """table narrowing happens in schema_loader, not here; the
    resolver returns the same single id."""
    ws = uuid4()
    cid = await _seed_conn(session, workspace_id=str(ws))
    from uuid import UUID

    r = await resolve_scope(
        session,
        workspace_id=ws,
        scope="table",
        active_connection_id=UUID(cid),
        scope_cluster_id=None,
    )
    assert r.connection_ids == [UUID(cid)]
    assert r.federation is False


@pytest.mark.asyncio
async def test_database_scope_without_active_connection_errors(
    session,
) -> None:
    r = await resolve_scope(
        session,
        workspace_id=uuid4(),
        scope="database",
        active_connection_id=None,
        scope_cluster_id=None,
    )
    assert r.connection_ids == []
    assert r.federation is False
    assert r.error is not None
    assert "pick" in r.error.lower() or "needs" in r.error.lower()


# ── cluster ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cluster_scope_returns_cluster_members(session) -> None:
    ws = uuid4()
    cluster = str(uuid4())
    a = await _seed_conn(session, workspace_id=str(ws), cluster_id=cluster)
    b = await _seed_conn(session, workspace_id=str(ws), cluster_id=cluster)
    # Distractor: same workspace, different cluster
    other_cluster = str(uuid4())
    await _seed_conn(
        session, workspace_id=str(ws), cluster_id=other_cluster
    )
    # Distractor: clusterless connection
    await _seed_conn(session, workspace_id=str(ws), cluster_id=None)
    from uuid import UUID

    r = await resolve_scope(
        session,
        workspace_id=ws,
        scope="cluster",
        active_connection_id=None,
        scope_cluster_id=UUID(cluster),
    )
    assert set(r.connection_ids) == {UUID(a), UUID(b)}
    assert r.federation is True


@pytest.mark.asyncio
async def test_cluster_scope_without_cluster_id_errors(session) -> None:
    r = await resolve_scope(
        session,
        workspace_id=uuid4(),
        scope="cluster",
        active_connection_id=None,
        scope_cluster_id=None,
    )
    assert r.connection_ids == []
    assert r.federation is True
    assert r.error is not None


@pytest.mark.asyncio
async def test_cluster_with_no_ready_members_errors(session) -> None:
    ws = uuid4()
    cluster = str(uuid4())
    # Member exists but is in `pending` state — scope only returns
    # ready connections.
    await _seed_conn(
        session, workspace_id=str(ws), cluster_id=cluster,
        status="pending",
    )
    from uuid import UUID

    r = await resolve_scope(
        session,
        workspace_id=ws,
        scope="cluster",
        active_connection_id=None,
        scope_cluster_id=UUID(cluster),
    )
    assert r.connection_ids == []
    assert r.error is not None


# ── all_databases / all_connections / all_clusters ──────────────


@pytest.mark.asyncio
async def test_all_databases_returns_every_ready_connection(session) -> None:
    ws = uuid4()
    a = await _seed_conn(session, workspace_id=str(ws))
    b = await _seed_conn(session, workspace_id=str(ws))
    # pending → excluded
    await _seed_conn(session, workspace_id=str(ws), status="pending")
    # different workspace → excluded
    await _seed_conn(session, workspace_id=str(uuid4()))
    from uuid import UUID

    r = await resolve_scope(
        session,
        workspace_id=ws,
        scope="all_databases",
        active_connection_id=None,
        scope_cluster_id=None,
    )
    assert set(r.connection_ids) == {UUID(a), UUID(b)}
    assert r.federation is True


@pytest.mark.asyncio
async def test_all_connections_is_alias_of_all_databases(session) -> None:
    ws = uuid4()
    a = await _seed_conn(session, workspace_id=str(ws))
    from uuid import UUID

    r = await resolve_scope(
        session,
        workspace_id=ws,
        scope="all_connections",
        active_connection_id=None,
        scope_cluster_id=None,
    )
    assert r.connection_ids == [UUID(a)]


@pytest.mark.asyncio
async def test_all_clusters_returns_only_clustered_connections(
    session,
) -> None:
    ws = uuid4()
    cluster = str(uuid4())
    clustered = await _seed_conn(
        session, workspace_id=str(ws), cluster_id=cluster
    )
    # Standalone (no cluster) — must be excluded for all_clusters.
    await _seed_conn(session, workspace_id=str(ws), cluster_id=None)
    from uuid import UUID

    r = await resolve_scope(
        session,
        workspace_id=ws,
        scope="all_clusters",
        active_connection_id=None,
        scope_cluster_id=None,
    )
    assert r.connection_ids == [UUID(clustered)]
    assert r.federation is True


@pytest.mark.asyncio
async def test_all_databases_empty_workspace_errors(session) -> None:
    r = await resolve_scope(
        session,
        workspace_id=uuid4(),
        scope="all_databases",
        active_connection_id=None,
        scope_cluster_id=None,
    )
    assert r.connection_ids == []
    assert r.federation is True
    assert r.error is not None


@pytest.mark.asyncio
async def test_all_clusters_empty_returns_clear_error(session) -> None:
    ws = uuid4()
    # Standalone-only — no clustered connection in this workspace.
    await _seed_conn(session, workspace_id=str(ws), cluster_id=None)
    r = await resolve_scope(
        session,
        workspace_id=ws,
        scope="all_clusters",
        active_connection_id=None,
        scope_cluster_id=None,
    )
    assert r.connection_ids == []
    assert r.error is not None
    assert "cluster" in r.error.lower()
