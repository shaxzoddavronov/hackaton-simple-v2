"""Phase 35 — connection health probe + sweep tests.

Probe functions hit real engine constructors but with a mocked
underlying client so we never need a live Postgres / Mongo / ES.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.connection_health import (
    HealthResult,
    PROBE_TIMEOUT_S,
    _sanitize_error,
    probe_one,
)


@pytest.fixture(autouse=True)
def _stub_engine_register(monkeypatch):
    """Skip real engine registration — every probe path imports
    `register_all` at call time, and we don't need the heavy import
    chain for these unit tests."""
    import app.services.connection_health as ch

    monkeypatch.setattr(
        "app.engines.register_all", lambda: None, raising=False
    )
    # connection_health imports inside the function body, so the
    # patch must also cover the resolved name there.
    return ch


def _conn(dialect: str, meta: dict | None = None, auth_kind: str = "password"):
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        workspace_id="00000000-0000-0000-0000-000000000002",
        dialect=dialect,
        connection_meta=meta or {},
        auth_kind=auth_kind,
    )


# ── _sanitize_error ──────────────────────────────────────────────


def test_sanitize_strips_traceback_to_first_line() -> None:
    e = RuntimeError("connection refused\n  File ...\n  ...")
    assert _sanitize_error(e) == "connection refused"


def test_sanitize_caps_long_messages() -> None:
    e = RuntimeError("x" * 1000)
    assert len(_sanitize_error(e)) <= 240


def test_sanitize_falls_back_to_class_name_on_empty() -> None:
    class MyErr(Exception):
        pass

    assert _sanitize_error(MyErr("")) == "MyErr"


# ── probe_one happy / sad paths ─────────────────────────────────


@pytest.mark.asyncio
async def test_probe_one_sql_success_records_latency() -> None:
    """SQL probe path: get_engine().execute('SELECT 1') succeeds →
    HealthResult.ok=True with a sensible latency."""
    mock_engine = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rows=[[1]])),
        aclose=AsyncMock(),
    )
    with patch(
        "app.engines.registry.get_engine", return_value=mock_engine
    ):
        result = await probe_one(_conn("postgres"), {"password": "x"})
    assert result.ok is True
    assert result.latency_ms >= 0
    assert result.error is None
    mock_engine.execute.assert_awaited_once()
    mock_engine.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_one_sql_failure_records_error() -> None:
    mock_engine = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("auth failed: bad password")),
        aclose=AsyncMock(),
    )
    with patch(
        "app.engines.registry.get_engine", return_value=mock_engine
    ):
        result = await probe_one(_conn("postgres"), {"password": "bad"})
    assert result.ok is False
    assert "auth failed" in (result.error or "")
    # aclose still ran even though execute raised.
    mock_engine.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_one_timeout_returns_clear_message() -> None:
    """If execute hangs past PROBE_TIMEOUT_S, the probe must return
    a timeout error — not bubble asyncio.TimeoutError up."""
    import asyncio

    async def hang(*a, **k):
        await asyncio.sleep(60)

    mock_engine = SimpleNamespace(
        execute=AsyncMock(side_effect=hang),
        aclose=AsyncMock(),
    )
    # Patch the constant to 0.1s so the test is fast.
    with patch(
        "app.services.connection_health.PROBE_TIMEOUT_S", 0.1
    ), patch(
        "app.engines.registry.get_engine", return_value=mock_engine
    ):
        result = await probe_one(_conn("postgres"), {"password": "x"})
    assert result.ok is False
    assert result.error is not None
    assert "exceeded" in result.error


@pytest.mark.asyncio
async def test_probe_one_es_calls_cluster_health() -> None:
    """ES probe uses the engine's _client.cluster.health() not
    execute(). Verify the right path fires."""
    health_call = AsyncMock(return_value={"status": "green"})
    cluster = SimpleNamespace(health=health_call)
    mock_client = SimpleNamespace(cluster=cluster)
    mock_engine = SimpleNamespace(
        _client=mock_client,
        aclose=AsyncMock(),
    )
    with patch(
        "app.engines.registry.get_engine", return_value=mock_engine
    ):
        result = await probe_one(_conn("elasticsearch"), {})
    assert result.ok is True
    health_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_one_mongo_calls_ping() -> None:
    ping = AsyncMock(return_value={"ok": 1})
    db = SimpleNamespace(command=ping)
    mock_client = {"admin": db}
    mock_engine = SimpleNamespace(
        _client=mock_client,
        aclose=AsyncMock(),
    )
    with patch(
        "app.engines.registry.get_engine", return_value=mock_engine
    ):
        result = await probe_one(_conn("mongodb"), {})
    assert result.ok is True
    ping.assert_awaited_once_with("ping")


@pytest.mark.asyncio
async def test_probe_one_graphql_runs_typename_query() -> None:
    """GraphQL probe sends the smallest possible operation."""
    seen: list[str] = []

    async def fake_execute(envelope, **kwargs):
        seen.append(envelope)
        return SimpleNamespace(rows=[])

    mock_engine = SimpleNamespace(
        execute=AsyncMock(side_effect=fake_execute),
        aclose=AsyncMock(),
    )
    with patch(
        "app.engines.registry.get_engine", return_value=mock_engine
    ):
        result = await probe_one(_conn("graphql"), {})
    assert result.ok is True
    assert seen, "probe never reached execute"
    assert "__typename" in seen[0]


@pytest.mark.asyncio
async def test_probe_one_rest_api_missing_base_url_fails_cleanly() -> None:
    """REST probe needs base_url; without it we want a clear error,
    not a 500."""
    result = await probe_one(
        _conn("rest_api", meta={}, auth_kind="none"), {}
    )
    assert result.ok is False
    assert "base_url" in (result.error or "")


# ── HealthResult sanity ────────────────────────────────────────


def test_health_result_dataclass_is_compact() -> None:
    r = HealthResult(ok=True, latency_ms=12)
    assert r.ok is True
    assert r.latency_ms == 12
    assert r.error is None


def test_probe_timeout_constant_is_sane() -> None:
    """Guardrail: the per-probe ceiling must be smaller than the
    beat interval (5 minutes = 300s) so consecutive sweeps don't
    pile up if a probe takes forever."""
    assert 0 < PROBE_TIMEOUT_S < 60
