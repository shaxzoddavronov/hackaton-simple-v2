"""Phase 39 — slash-command parser + handlers."""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.slash_commands import (
    SUPPORTED_LANGS,
    SlashCommand,
    handle_command,
    parse_command,
)


# ── parse_command ────────────────────────────────────────────────


def test_parse_returns_none_for_non_slash() -> None:
    assert parse_command("hello") is None
    assert parse_command("") is None
    assert parse_command(None) is None  # type: ignore[arg-type]


def test_parse_strips_whitespace() -> None:
    cmd = parse_command("   /help   ")
    assert cmd is not None and cmd.name == "help"


def test_parse_lowercases_command_name() -> None:
    cmd = parse_command("/HELP")
    assert cmd is not None and cmd.name == "help"


def test_parse_splits_arg() -> None:
    cmd = parse_command("/lang uz")
    assert cmd is not None
    assert cmd.name == "lang"
    assert cmd.arg == "uz"


def test_parse_returns_none_for_unknown_command() -> None:
    """``/unknown`` must NOT short-circuit — fall through to the
    agent so user can ask /-prefixed questions."""
    assert parse_command("/totally-bogus") is None


def test_parse_recognises_all_commands() -> None:
    for name in (
        "/help", "/sql", "/lang", "/clear-cache",
        "/refresh-schema", "/explain",
    ):
        assert parse_command(name) is not None, name


# ── handle_command — pure handlers (no side effects) ─────────────


@pytest.mark.asyncio
async def test_help_lists_commands() -> None:
    cmd = SlashCommand(name="help")
    result = await handle_command(
        cmd, db=None, user_id=uuid4(), workspace_id=None,
        connection_id=None, last_sql=None,
    )
    assert result.ui_spec["type"] == "text_only"
    body = result.ui_spec["body_md"]
    assert "/sql" in body
    assert "/lang" in body
    assert "/clear-cache" in body


@pytest.mark.asyncio
async def test_sql_with_no_history_returns_message() -> None:
    result = await handle_command(
        SlashCommand(name="sql"), db=None, user_id=uuid4(),
        workspace_id=None, connection_id=None, last_sql=None,
    )
    assert "No SQL yet" in result.ui_spec["body_md"]


@pytest.mark.asyncio
async def test_sql_with_last_sql_echoes_it() -> None:
    result = await handle_command(
        SlashCommand(name="sql"), db=None, user_id=uuid4(),
        workspace_id=None, connection_id=None,
        last_sql="SELECT 1",
    )
    body = result.ui_spec["body_md"]
    assert "SELECT 1" in body
    assert "```sql" in body


@pytest.mark.asyncio
async def test_lang_without_arg_shows_usage() -> None:
    result = await handle_command(
        SlashCommand(name="lang"), db=None, user_id=uuid4(),
        workspace_id=None, connection_id=None, last_sql=None,
    )
    body = result.ui_spec["body_md"]
    assert "uz" in body and "ru" in body and "en" in body


@pytest.mark.asyncio
async def test_lang_with_unsupported_arg_shows_usage() -> None:
    result = await handle_command(
        SlashCommand(name="lang", arg="fr"), db=None,
        user_id=uuid4(), workspace_id=None, connection_id=None,
        last_sql=None,
    )
    body = result.ui_spec["body_md"]
    assert "Usage" in body


@pytest.mark.asyncio
async def test_lang_with_supported_arg_confirms() -> None:
    for lang in SUPPORTED_LANGS:
        result = await handle_command(
            SlashCommand(name="lang", arg=lang), db=None,
            user_id=uuid4(), workspace_id=None,
            connection_id=None, last_sql=None,
        )
        assert lang in result.ui_spec["body_md"]


@pytest.mark.asyncio
async def test_explain_returns_overview() -> None:
    result = await handle_command(
        SlashCommand(name="explain"), db=None, user_id=uuid4(),
        workspace_id=None, connection_id=None, last_sql=None,
    )
    body = result.ui_spec["body_md"]
    # Mentions the key nodes.
    for kw in ("Coordinator", "Planner", "Validator", "Executor"):
        assert kw in body, kw


# ── handle_command — side effects ────────────────────────────────


@pytest.mark.asyncio
async def test_clear_cache_without_connection_is_friendly() -> None:
    result = await handle_command(
        SlashCommand(name="clear-cache"), db=None,
        user_id=uuid4(), workspace_id=uuid4(),
        connection_id=None, last_sql=None,
    )
    assert "Pick a connection" in result.ui_spec["body_md"]
    assert result.clear_cache_connection_id is None


@pytest.mark.asyncio
async def test_clear_cache_with_connection_signals_invalidation() -> None:
    cid = uuid4()
    result = await handle_command(
        SlashCommand(name="clear-cache"), db=None,
        user_id=uuid4(), workspace_id=uuid4(),
        connection_id=cid, last_sql=None,
    )
    assert result.clear_cache_connection_id == cid
    assert "cleared" in result.ui_spec["body_md"].lower()


@pytest.mark.asyncio
async def test_refresh_schema_without_connection_is_friendly() -> None:
    result = await handle_command(
        SlashCommand(name="refresh-schema"), db=None,
        user_id=uuid4(), workspace_id=uuid4(),
        connection_id=None, last_sql=None,
    )
    assert "Pick a connection" in result.ui_spec["body_md"]
    assert result.refresh_connection_id is None


@pytest.mark.asyncio
async def test_refresh_schema_with_connection_signals_reprofile() -> None:
    cid = uuid4()
    result = await handle_command(
        SlashCommand(name="refresh-schema"), db=None,
        user_id=uuid4(), workspace_id=uuid4(),
        connection_id=cid, last_sql=None,
    )
    assert result.refresh_connection_id == cid
    assert "queued" in result.ui_spec["body_md"].lower()
