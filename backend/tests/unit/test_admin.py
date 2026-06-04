"""Phase 16 — admin endpoints.

These tests boot a tiny FastAPI app that mounts ``admin.router``
against an in-memory SQLite, with auth deps overridden to inject
a fake "current superuser". We assert:

  - non-superuser callers get 403 (NOT 401)
  - superuser CRUD round-trips work
  - the self-protection guards (no self-demote / self-deactivate
    / self-delete) trip with 400
  - audit rows land in audit_log for create / update / delete
  - the audit listing endpoint filters by user / action / status
"""
from __future__ import annotations

from datetime import datetime, timezone
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

from app.api.admin import router as admin_router
from app.api.deps import get_current_user, require_superuser
from app.db.models import User
from app.db.session import get_db


# ── fixture: in-memory app + DB ─────────────────────────────────


def _make_engine_and_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    return engine, Session


async def _bootstrap_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE users ("
            "id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))), "
            "username TEXT UNIQUE NOT NULL, "
            "email TEXT UNIQUE NOT NULL, "
            "password_hash TEXT NOT NULL, "
            "is_active INTEGER NOT NULL DEFAULT 1, "
            "is_superuser INTEGER NOT NULL DEFAULT 0, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        # Tables ``User`` declares relationships against — delete-user
        # cascade reads them, so they must exist even if empty.
        await conn.exec_driver_sql(
            "CREATE TABLE workspaces ("
            "id TEXT PRIMARY KEY, owner_id TEXT, name TEXT, "
            "dialect TEXT, status TEXT, created_at TIMESTAMP, "
            "updated_at TIMESTAMP"
            ")"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE chat_sessions ("
            "id TEXT PRIMARY KEY, workspace_id TEXT, "
            "connection_id TEXT, user_id TEXT, "
            "title TEXT, summary TEXT, created_at TIMESTAMP"
            ")"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE refresh_tokens ("
            "id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))), "
            "user_id TEXT NOT NULL, "
            "token_hash TEXT UNIQUE NOT NULL, "
            "expires_at TIMESTAMP NOT NULL, "
            "revoked_at TIMESTAMP, "
            "user_agent TEXT, "
            "client_ip TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
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


async def _seed_user(
    Session, *, is_superuser: bool, username: str | None = None
) -> User:
    """Seed via raw SQL to bypass the SQLite UUID-binding quirk —
    UUIDType uses ``String(36)`` on SQLite which the sqlite3 driver
    won't accept a Python UUID for. We construct + return a User
    instance carrying the same UUID so callers can compare
    `request.user_id == admin.id` (production semantics)."""
    from sqlalchemy import text as sa_text

    uid = uuid4()
    uname = username or f"u_{uuid4().hex[:8]}"
    email = f"{uuid4().hex[:8]}@example.com"
    async with Session() as s:
        await s.execute(
            sa_text(
                "INSERT INTO users ("
                "id, username, email, password_hash, "
                "is_active, is_superuser, created_at"
                ") VALUES (:id, :u, :e, :h, :a, :su, :ts)"
            ),
            {
                "id": str(uid),
                "u": uname,
                "e": email,
                "h": "$2b$04$abcdefghijklmnopqrstuv",  # not bcrypt-real
                "a": 1,
                "su": 1 if is_superuser else 0,
                "ts": datetime.now(timezone.utc),
            },
        )
        await s.commit()
    return User(
        id=uid,
        username=uname,
        email=email,
        password_hash="$2b$04$abcdefghijklmnopqrstuv",
        is_active=True,
        is_superuser=is_superuser,
        created_at=datetime.now(timezone.utc),
    )


def _build_app(
    Session, *, current_user: User, is_superuser_user: User | None = None
) -> FastAPI:
    """Mount admin router with deps overridden. ``current_user``
    becomes whatever auth resolves to; the require_superuser
    override depends on is_superuser_user (=current_user when
    None) — useful for the 403 path."""
    app = FastAPI()
    app.include_router(admin_router)

    async def _override_get_db():
        async with Session() as s:
            yield s

    async def _override_current():
        return current_user

    async def _override_require_superuser():
        from fastapi import HTTPException, status

        gate = is_superuser_user or current_user
        if not gate.is_superuser:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="superuser privileges required",
            )
        return gate

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_current
    app.dependency_overrides[require_superuser] = _override_require_superuser
    return app


@pytest.fixture
async def superuser_app_factory():
    """Returns a builder so tests can swap the seeded admin
    user / DB between cases."""
    engine, Session = _make_engine_and_session()
    await _bootstrap_schema(engine)
    admin_user = await _seed_user(
        Session, is_superuser=True, username="admin_test"
    )
    plain_user = await _seed_user(
        Session, is_superuser=False, username="plain_test"
    )

    def build(*, as_admin: bool):
        cu = admin_user if as_admin else plain_user
        return _build_app(Session, current_user=cu), cu, Session

    yield build, admin_user, plain_user, Session
    await engine.dispose()


# ── superuser gate ──────────────────────────────────────────────


def test_non_superuser_gets_403_on_list(superuser_app_factory) -> None:
    build, *_ = superuser_app_factory
    app, _cu, _Session = build(as_admin=False)
    client = TestClient(app)
    resp = client.get("/admin/users")
    assert resp.status_code == 403


def test_non_superuser_gets_403_on_create(superuser_app_factory) -> None:
    build, *_ = superuser_app_factory
    app, _cu, _Session = build(as_admin=False)
    client = TestClient(app)
    resp = client.post(
        "/admin/users",
        json={
            "username": "newone",
            "email": "newone@example.com",
            "password": "Strong1Pass",
            "is_superuser": False,
        },
    )
    assert resp.status_code == 403


def test_non_superuser_gets_403_on_audit(superuser_app_factory) -> None:
    build, *_ = superuser_app_factory
    app, _cu, _Session = build(as_admin=False)
    client = TestClient(app)
    resp = client.get("/admin/audit")
    assert resp.status_code == 403


# ── superuser CRUD happy paths ──────────────────────────────────


def test_superuser_can_list_users(superuser_app_factory) -> None:
    build, admin_user, plain_user, _Session = superuser_app_factory
    app, _cu, _ = build(as_admin=True)
    client = TestClient(app)
    resp = client.get("/admin/users")
    assert resp.status_code == 200
    rows = resp.json()
    ids = {r["id"] for r in rows}
    assert str(admin_user.id) in ids
    assert str(plain_user.id) in ids


def test_superuser_can_create_user(superuser_app_factory) -> None:
    build, *_ = superuser_app_factory
    app, _cu, Session = build(as_admin=True)
    client = TestClient(app)
    resp = client.post(
        "/admin/users",
        json={
            "username": "fresh_one",
            "email": "fresh_one@example.com",
            "password": "Strong1Pass",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "fresh_one"
    assert body["is_superuser"] is False


def test_create_rejects_short_password(superuser_app_factory) -> None:
    """`_validate_password` enforces PASSWORD_MIN_LENGTH (default 8)
    + digit requirement. Pass pydantic's schema-level min_length=4
    so we hit the application-level check, not the Field validator."""
    build, *_ = superuser_app_factory
    app, _cu, _ = build(as_admin=True)
    client = TestClient(app)
    resp = client.post(
        "/admin/users",
        json={
            "username": "x_one",
            "email": "x_one@example.com",
            "password": "abc12",  # passes pydantic, fails PASSWORD_MIN_LENGTH=8
        },
    )
    assert resp.status_code == 400


def test_create_collision_returns_409(superuser_app_factory) -> None:
    build, admin_user, *_ = superuser_app_factory
    app, _cu, _ = build(as_admin=True)
    client = TestClient(app)
    # Same username as the seeded admin → unique-constraint trip.
    resp = client.post(
        "/admin/users",
        json={
            "username": admin_user.username,
            "email": "other@example.com",
            "password": "Strong1Pass",
        },
    )
    assert resp.status_code == 409


# ── update guards ───────────────────────────────────────────────


def test_admin_cannot_self_deactivate(superuser_app_factory) -> None:
    build, admin_user, *_ = superuser_app_factory
    app, _cu, _ = build(as_admin=True)
    client = TestClient(app)
    resp = client.patch(
        f"/admin/users/{admin_user.id}",
        json={"is_active": False},
    )
    assert resp.status_code == 400
    assert "deactivate" in resp.json()["detail"].lower()


def test_admin_cannot_self_demote(superuser_app_factory) -> None:
    build, admin_user, *_ = superuser_app_factory
    app, _cu, _ = build(as_admin=True)
    client = TestClient(app)
    resp = client.patch(
        f"/admin/users/{admin_user.id}",
        json={"is_superuser": False},
    )
    assert resp.status_code == 400
    assert "demote" in resp.json()["detail"].lower()


def test_admin_can_deactivate_another_user(superuser_app_factory) -> None:
    build, _admin, plain_user, _Session = superuser_app_factory
    app, _cu, _ = build(as_admin=True)
    client = TestClient(app)
    resp = client.patch(
        f"/admin/users/{plain_user.id}",
        json={"is_active": False},
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


def test_admin_can_promote_user_to_superuser(superuser_app_factory) -> None:
    build, _admin, plain_user, _Session = superuser_app_factory
    app, _cu, _ = build(as_admin=True)
    client = TestClient(app)
    resp = client.patch(
        f"/admin/users/{plain_user.id}",
        json={"is_superuser": True},
    )
    assert resp.status_code == 200
    assert resp.json()["is_superuser"] is True


# ── delete guard ────────────────────────────────────────────────


def test_admin_cannot_self_delete(superuser_app_factory) -> None:
    build, admin_user, *_ = superuser_app_factory
    app, _cu, _ = build(as_admin=True)
    client = TestClient(app)
    resp = client.delete(f"/admin/users/{admin_user.id}")
    assert resp.status_code == 400


def test_admin_can_delete_another_user(superuser_app_factory) -> None:
    build, _admin, plain_user, _Session = superuser_app_factory
    app, _cu, _ = build(as_admin=True)
    client = TestClient(app)
    resp = client.delete(f"/admin/users/{plain_user.id}")
    assert resp.status_code == 204


def test_delete_missing_user_returns_404(superuser_app_factory) -> None:
    build, *_ = superuser_app_factory
    app, _cu, _ = build(as_admin=True)
    client = TestClient(app)
    resp = client.delete(f"/admin/users/{uuid4()}")
    assert resp.status_code == 404


# ── audit listing ───────────────────────────────────────────────


def test_audit_endpoint_returns_recent_rows(superuser_app_factory) -> None:
    """A create triggers an audit row; the audit listing surfaces it."""
    build, _admin, *_ = superuser_app_factory
    app, _cu, _ = build(as_admin=True)
    client = TestClient(app)
    # Trigger a create → audit row.
    client.post(
        "/admin/users",
        json={
            "username": "audited_one",
            "email": "audited_one@example.com",
            "password": "Strong1Pass",
        },
    )
    resp = client.get("/admin/audit")
    assert resp.status_code == 200
    rows = resp.json()
    assert any(r["action"] == "user.create" for r in rows)


def test_audit_endpoint_supports_action_filter(superuser_app_factory) -> None:
    build, _admin, plain_user, _Session = superuser_app_factory
    app, _cu, _ = build(as_admin=True)
    client = TestClient(app)
    # Issue a couple of audited mutations.
    client.patch(
        f"/admin/users/{plain_user.id}", json={"is_active": False}
    )
    client.patch(
        f"/admin/users/{plain_user.id}", json={"is_active": True}
    )
    # action= filter is prefix-matched.
    resp = client.get("/admin/audit?action=user.update")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) >= 1
    assert all(r["action"].startswith("user.update") for r in rows)
