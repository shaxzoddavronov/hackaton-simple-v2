"""Phase 16 — refresh token primitives.

These tests run against the in-memory SQLite fixture so the
single-use rotation + replay-detection semantics are exercised
without a Postgres up. The model side (RefreshToken) accepts a
plain UUID through the SQLite `UUIDType` variant so we can wire
fake users without an INSERT into ``users``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.db.models import RefreshToken, User
from app.services.auth_tokens import (
    _hash_token,
    consume_refresh_token,
    generate_refresh_token,
    issue_refresh_token,
    revoke_all_for_user,
)


# ── shared in-memory fixture ────────────────────────────────────


@pytest.fixture
async def session() -> AsyncSession:
    """Per-test SQLite-in-memory session.

    Hand-rolled DDL for only the tables this module exercises —
    the project's existing pattern (see test_rag_retriever.py)
    because ``Base.metadata.create_all`` chokes on Postgres-only
    server defaults like ``'{}'::jsonb``. Keeping the test DB
    minimal also makes failure traces clearer.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE users ("
            "id TEXT PRIMARY KEY, "
            "username TEXT UNIQUE NOT NULL, "
            "email TEXT UNIQUE NOT NULL, "
            "password_hash TEXT NOT NULL, "
            "is_active INTEGER NOT NULL DEFAULT 1, "
            "is_superuser INTEGER NOT NULL DEFAULT 0, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
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
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE"
            ")"
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _seed_user(session: AsyncSession) -> User:
    u = User(
        id=uuid4(),
        username=f"u_{uuid4().hex[:8]}",
        email=f"{uuid4().hex[:8]}@x.test",
        password_hash="not-real-hash",
        is_active=True,
        is_superuser=False,
    )
    session.add(u)
    await session.flush()
    return u


# ── generate_refresh_token ──────────────────────────────────────


def test_generate_returns_urlsafe_string() -> None:
    raw = generate_refresh_token()
    assert isinstance(raw, str)
    assert len(raw) >= 40
    # token_urlsafe alphabet — base64url, no padding chars in output
    assert all(c.isalnum() or c in "-_" for c in raw)


def test_generate_is_unique_across_calls() -> None:
    seen = {generate_refresh_token() for _ in range(20)}
    assert len(seen) == 20  # 256-bit entropy → no collision in 20 picks


def test_hash_token_is_stable_and_one_way() -> None:
    raw = "alpha-beta-gamma"
    assert _hash_token(raw) == _hash_token(raw)
    # SHA-256 hex digest length
    assert len(_hash_token(raw)) == 64


# ── issue_refresh_token ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_returns_raw_and_persists_hash(session) -> None:
    u = await _seed_user(session)
    raw, row = await issue_refresh_token(
        session, user=u, user_agent="curl/8", client_ip="10.0.0.1"
    )
    assert isinstance(raw, str)
    assert row.user_id == u.id
    # Hash must match what we'd compute from the raw secret.
    assert row.token_hash == _hash_token(raw)
    # Metadata recorded.
    assert row.user_agent == "curl/8"
    assert row.client_ip == "10.0.0.1"
    # Not yet revoked / not yet expired.
    assert row.revoked_at is None
    assert row.expires_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_issue_truncates_long_user_agent(session) -> None:
    u = await _seed_user(session)
    long_ua = "X" * 1000
    _, row = await issue_refresh_token(
        session, user=u, user_agent=long_ua, client_ip=None
    )
    assert row.user_agent is not None
    assert len(row.user_agent) <= 255


@pytest.mark.asyncio
async def test_issue_accepts_missing_metadata(session) -> None:
    u = await _seed_user(session)
    raw, row = await issue_refresh_token(
        session, user=u, user_agent=None, client_ip=None
    )
    assert raw  # still works
    assert row.user_agent is None
    assert row.client_ip is None


# ── consume_refresh_token (single-use rotation) ─────────────────


@pytest.mark.asyncio
async def test_consume_marks_revoked_and_returns_row(session) -> None:
    u = await _seed_user(session)
    raw, _ = await issue_refresh_token(session, user=u)
    row = await consume_refresh_token(session, raw)
    assert row.revoked_at is not None
    assert row.user_id == u.id


@pytest.mark.asyncio
async def test_consume_unknown_raises(session) -> None:
    with pytest.raises(ValueError, match="unknown"):
        await consume_refresh_token(session, "not-a-real-token")


@pytest.mark.asyncio
async def test_consume_twice_is_replay_detected(session) -> None:
    """The critical security invariant: a stolen token used a
    second time MUST hard-fail. The original holder's session is
    already gone so they re-login; the attacker is locked out."""
    u = await _seed_user(session)
    raw, _ = await issue_refresh_token(session, user=u)
    await consume_refresh_token(session, raw)
    with pytest.raises(ValueError, match="replay"):
        await consume_refresh_token(session, raw)


@pytest.mark.asyncio
async def test_consume_expired_raises(session) -> None:
    u = await _seed_user(session)
    raw, row = await issue_refresh_token(session, user=u)
    # Backdate the row so the expiry check trips.
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.flush()
    with pytest.raises(ValueError, match="expired"):
        await consume_refresh_token(session, raw)


# ── revoke_all_for_user ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_all_marks_every_active_token(session) -> None:
    u = await _seed_user(session)
    for _ in range(3):
        await issue_refresh_token(session, user=u)
    count = await revoke_all_for_user(session, u.id)
    assert count == 3
    # All previously-issued tokens are now non-consumable.
    from sqlalchemy import select

    rows = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.user_id == u.id)
        )
    ).scalars().all()
    assert all(r.revoked_at is not None for r in rows)


@pytest.mark.asyncio
async def test_revoke_all_skips_already_revoked(session) -> None:
    u = await _seed_user(session)
    raw1, _ = await issue_refresh_token(session, user=u)
    # Consume one — already revoked.
    await consume_refresh_token(session, raw1)
    # Issue another — still active.
    await issue_refresh_token(session, user=u)
    count = await revoke_all_for_user(session, u.id)
    # Only the active token shows up in the bulk count.
    assert count == 1


@pytest.mark.asyncio
async def test_revoke_all_for_user_without_tokens_returns_zero(session) -> None:
    u = await _seed_user(session)
    count = await revoke_all_for_user(session, u.id)
    assert count == 0


# ── isolation: two users don't share token namespaces ───────────


@pytest.mark.asyncio
async def test_consume_only_affects_its_owner(session) -> None:
    a = await _seed_user(session)
    b = await _seed_user(session)
    raw_a, _ = await issue_refresh_token(session, user=a)
    raw_b, _ = await issue_refresh_token(session, user=b)
    # Consuming a's token leaves b's token untouched.
    await consume_refresh_token(session, raw_a)
    row_b = await consume_refresh_token(session, raw_b)
    assert row_b.user_id == b.id
