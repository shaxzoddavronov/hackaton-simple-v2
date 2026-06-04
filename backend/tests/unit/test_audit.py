"""Phase 16 — audit log writer + middleware.

The middleware integration test runs a tiny FastAPI app with the
real ``AuditMiddleware`` mounted and verifies an audit row lands
after a normal request. Failure-isolation is checked by injecting
a broken sessionmaker — the user-visible response must still
succeed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.services.audit import (
    AuditMiddleware,
    _extract_user_id_from_request,
    _should_audit,
    log_action,
)


# ── _should_audit ───────────────────────────────────────────────


def test_should_audit_skips_metrics() -> None:
    assert _should_audit("/metrics") is False
    assert _should_audit("/metrics/foo") is False


def test_should_audit_skips_health_docs() -> None:
    assert _should_audit("/healthz") is False
    assert _should_audit("/docs") is False
    assert _should_audit("/openapi.json") is False
    assert _should_audit("/redoc") is False
    assert _should_audit("/favicon.ico") is False


def test_should_audit_passes_normal_paths() -> None:
    assert _should_audit("/auth/login") is True
    assert _should_audit("/workspaces") is True
    assert _should_audit("/admin/users") is True


# ── _extract_user_id_from_request ───────────────────────────────


def _fake_request(headers: dict[str, str]) -> object:
    """A minimal stand-in for Starlette's Request — only ``headers``
    is read by the function under test."""

    class _Req:
        def __init__(self, h: dict[str, str]) -> None:
            self.headers = h

    return _Req(headers)


def test_extract_returns_none_without_header() -> None:
    assert _extract_user_id_from_request(_fake_request({})) is None  # type: ignore[arg-type]


def test_extract_returns_none_on_non_bearer() -> None:
    r = _fake_request({"authorization": "Basic xyz"})
    assert _extract_user_id_from_request(r) is None  # type: ignore[arg-type]


def test_extract_returns_none_on_malformed_jwt() -> None:
    r = _fake_request({"authorization": "Bearer not.a.jwt"})
    assert _extract_user_id_from_request(r) is None  # type: ignore[arg-type]


def test_extract_returns_uuid_for_valid_jwt() -> None:
    from jose import jwt as jose_jwt

    from app.config import settings

    uid = uuid4()
    tok = jose_jwt.encode(
        {"sub": str(uid)}, settings.JWT_SECRET, algorithm=settings.JWT_ALG
    )
    r = _fake_request({"authorization": f"Bearer {tok}"})
    assert _extract_user_id_from_request(r) == uid  # type: ignore[arg-type]


def test_extract_returns_none_when_sub_is_not_uuid() -> None:
    from jose import jwt as jose_jwt

    from app.config import settings

    tok = jose_jwt.encode(
        {"sub": "not-a-uuid"},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALG,
    )
    r = _fake_request({"authorization": f"Bearer {tok}"})
    assert _extract_user_id_from_request(r) is None  # type: ignore[arg-type]


# ── log_action (writer) ─────────────────────────────────────────


@pytest.fixture
async def audit_session() -> AsyncSession:
    """In-memory SQLite with just the audit_log table."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE audit_log ("
            "id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))), "
            "user_id TEXT, "
            "action TEXT NOT NULL, "
            "target_kind TEXT, "
            "target_id TEXT, "
            "status TEXT NOT NULL DEFAULT 'ok', "
            "payload TEXT NOT NULL DEFAULT '{}', "
            "client_ip TEXT, "
            "user_agent TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_log_action_writes_a_row(audit_session) -> None:
    uid = uuid4()
    await log_action(
        audit_session,
        action="user.create",
        user_id=uid,
        target_kind="user",
        target_id=str(uuid4()),
        status="ok",
        payload={"username": "x"},
        client_ip="10.0.0.1",
        user_agent="curl/8",
    )
    await audit_session.commit()
    rows = (
        await audit_session.execute(
            text("SELECT action, user_id, target_kind, status FROM audit_log")
        )
    ).all()
    assert len(rows) == 1
    assert rows[0][0] == "user.create"
    assert rows[0][1] == str(uid)
    assert rows[0][2] == "user"
    assert rows[0][3] == "ok"


@pytest.mark.asyncio
async def test_log_action_clamps_unknown_status(audit_session) -> None:
    """Defence: a caller passing ``status="bogus"`` must NOT trip the
    CHECK constraint — log_action coerces to 'ok'."""
    await log_action(
        audit_session,
        action="x",
        status="bogus",
    )
    await audit_session.commit()
    rows = (
        await audit_session.execute(text("SELECT status FROM audit_log"))
    ).all()
    assert rows[0][0] == "ok"


@pytest.mark.asyncio
async def test_log_action_truncates_long_fields(audit_session) -> None:
    """Field length guards prevent a misbehaving caller from blowing
    the column width."""
    await log_action(
        audit_session,
        action="x" * 200,
        target_kind="y" * 100,
        target_id="z" * 200,
        client_ip="i" * 200,
        user_agent="u" * 1000,
    )
    await audit_session.commit()
    rows = (
        await audit_session.execute(
            text(
                "SELECT action, target_kind, target_id, "
                "client_ip, user_agent FROM audit_log"
            )
        )
    ).all()
    action, target_kind, target_id, ip, ua = rows[0]
    assert len(action) <= 64
    assert len(target_kind) <= 32
    assert len(target_id) <= 64
    assert len(ip) <= 64
    assert len(ua) <= 255


# ── AuditMiddleware (FastAPI integration) ───────────────────────


def _middleware_app(sessionmaker_factory) -> FastAPI:
    app = FastAPI()

    @app.get("/hello")
    def hello() -> dict:
        return {"ok": True}

    @app.get("/err")
    def err() -> dict:
        raise RuntimeError("boom")

    app.add_middleware(
        AuditMiddleware, sessionmaker_factory=sessionmaker_factory
    )
    return app


def test_middleware_writes_row_on_successful_request(audit_session) -> None:
    """A 200 response leaves an ok-status audit row."""

    # Pass our pre-built sessionmaker so the middleware reuses the
    # in-memory SQLite (the default factory would spin up a real
    # async engine against DATABASE_URL).
    engine = audit_session.bind
    Session = async_sessionmaker(engine, expire_on_commit=False)

    app = _middleware_app(sessionmaker_factory=Session)
    client = TestClient(app)
    resp = client.get("/hello")
    assert resp.status_code == 200

    import asyncio

    async def _count() -> int:
        async with Session() as s:
            rows = (
                await s.execute(text("SELECT status FROM audit_log"))
            ).all()
            return len(rows)

    # TestClient runs the middleware inside an event loop; the audit
    # write happens BEFORE the response is yielded back, so by this
    # point the row exists.
    assert asyncio.run(_count()) == 1


def test_middleware_writes_denied_for_401(audit_session) -> None:
    """A 401 surfaces as status='denied' so audit consumers can
    filter for permission failures."""
    from fastapi import HTTPException

    engine = audit_session.bind
    Session = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()

    @app.get("/locked")
    def locked() -> None:
        raise HTTPException(status_code=401, detail="nope")

    app.add_middleware(AuditMiddleware, sessionmaker_factory=Session)
    client = TestClient(app)
    resp = client.get("/locked")
    assert resp.status_code == 401

    import asyncio

    async def _status() -> str:
        async with Session() as s:
            rows = (
                await s.execute(text("SELECT status FROM audit_log"))
            ).all()
            return rows[0][0] if rows else ""

    assert asyncio.run(_status()) == "denied"


def test_middleware_swallows_audit_write_failure() -> None:
    """If the audit session can't commit, the response MUST still
    reach the client. This is the "audit MUST NOT break user
    request" invariant from services/audit.py."""

    # A sessionmaker that produces a session whose .execute raises.
    class _BrokenSession:
        async def execute(self, *a, **k):
            raise RuntimeError("audit DB exploded")

        async def commit(self):
            pass

        async def rollback(self):
            pass

        def add(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class _BrokenMaker:
        def __call__(self):
            return _BrokenSession()

    app = _middleware_app(sessionmaker_factory=_BrokenMaker())
    client = TestClient(app)
    resp = client.get("/hello")
    # The user got their response despite the audit failure.
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
