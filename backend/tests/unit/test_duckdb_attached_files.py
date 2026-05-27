"""Tests for the DuckDB engine's attached_files mode (Phase 13).

These are integration-style tests against a real DuckDB driver — the
whole point is to verify that read_csv_auto / read_parquet / read_json_auto
produce a queryable view through the QueryEngine surface. No mocks.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.engines.duckdb import DuckdbEngine, _loader_for


def _source(connection_meta: dict):
    return SimpleNamespace(
        dialect="duckdb",
        connection_meta=connection_meta,
        _credentials={},
    )


# ── loader lookup ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("sales.csv", "read_csv_auto"),
        ("sales.TSV", "read_csv_auto"),
        ("part-00000.parquet", "read_parquet"),
        ("snapshot.pq", "read_parquet"),
        ("events.json", "read_json_auto"),
        ("events.ndjson", "read_json_auto"),
        ("events.JSONL", "read_json_auto"),
        ("not-a-data-file.txt", None),
        ("/abs/path/data.csv", "read_csv_auto"),
    ],
)
def test_loader_for(filename: str, expected: str | None) -> None:
    assert _loader_for(filename) == expected


# ── construction validation ──────────────────────────────────────


def test_attached_files_must_be_list() -> None:
    with pytest.raises(ValueError, match="attached_files must be a list"):
        DuckdbEngine(
            _source(
                {
                    "path": ":memory:",
                    "attached_files": "not-a-list",
                }
            )
        )


def test_attached_entry_requires_path_and_view() -> None:
    with pytest.raises(ValueError, match="path' and 'view_name"):
        DuckdbEngine(
            _source(
                {
                    "path": ":memory:",
                    "attached_files": [{"path": "/tmp/x.csv"}],
                }
            )
        )


def test_unsupported_extension_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        DuckdbEngine(
            _source(
                {
                    "path": ":memory:",
                    "attached_files": [
                        {"path": "/tmp/x.xlsx", "view_name": "x"}
                    ],
                }
            )
        )


# ── CSV roundtrip ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_csv_attached_file_introspect_and_query(tmp_path: Path) -> None:
    csv = tmp_path / "sales.csv"
    csv.write_text(
        "id,name,amount\n"
        "1,alice,100.5\n"
        "2,bob,250.0\n"
        "3,carol,75.25\n",
        encoding="utf-8",
    )

    engine = DuckdbEngine(
        _source(
            {
                "path": ":memory:",
                "attached_files": [
                    {"path": str(csv), "view_name": "sales"}
                ],
            }
        )
    )

    bundle = await engine.introspect_schema()
    table_names = {t.name for t in bundle.tables}
    assert "sales" in table_names

    sales = next(t for t in bundle.tables if t.name == "sales")
    col_names = {c.name for c in sales.columns}
    assert col_names == {"id", "name", "amount"}

    rs = await engine.execute(
        "SELECT name, amount FROM sales ORDER BY amount DESC LIMIT 2"
    )
    assert rs.columns == ["name", "amount"]
    # bob (250) > alice (100.5) > carol (75.25)
    assert rs.rows[0] == ["bob", 250.0]
    assert rs.rows[1] == ["alice", 100.5]


# ── JSON roundtrip ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_json_attached_file(tmp_path: Path) -> None:
    payload = [
        {"id": 1, "tier": "gold", "spend": 1000},
        {"id": 2, "tier": "silver", "spend": 250},
        {"id": 3, "tier": "gold", "spend": 1500},
    ]
    p = tmp_path / "customers.json"
    p.write_text(json.dumps(payload), encoding="utf-8")

    engine = DuckdbEngine(
        _source(
            {
                "path": ":memory:",
                "attached_files": [
                    {"path": str(p), "view_name": "customers"}
                ],
            }
        )
    )

    bundle = await engine.introspect_schema()
    assert any(t.name == "customers" for t in bundle.tables)

    rs = await engine.execute(
        "SELECT tier, SUM(spend) AS total FROM customers GROUP BY tier ORDER BY tier"
    )
    by_tier = dict(zip([r[0] for r in rs.rows], [r[1] for r in rs.rows]))
    assert by_tier["gold"] == 2500
    assert by_tier["silver"] == 250


# ── NDJSON roundtrip ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ndjson_attached_file(tmp_path: Path) -> None:
    p = tmp_path / "events.ndjson"
    p.write_text(
        '{"id":1,"kind":"signup"}\n'
        '{"id":2,"kind":"login"}\n'
        '{"id":3,"kind":"login"}\n',
        encoding="utf-8",
    )

    engine = DuckdbEngine(
        _source(
            {
                "path": ":memory:",
                "attached_files": [
                    {"path": str(p), "view_name": "events"}
                ],
            }
        )
    )

    rs = await engine.execute(
        "SELECT kind, COUNT(*) AS n FROM events GROUP BY kind ORDER BY kind"
    )
    by_kind = {r[0]: r[1] for r in rs.rows}
    assert by_kind == {"login": 2, "signup": 1}


# ── Read-only enforcement still holds ─────────────────────────────


@pytest.mark.asyncio
async def test_write_against_attached_view_rejected(tmp_path: Path) -> None:
    csv = tmp_path / "x.csv"
    csv.write_text("id,n\n1,1\n", encoding="utf-8")
    engine = DuckdbEngine(
        _source(
            {
                "path": ":memory:",
                "attached_files": [{"path": str(csv), "view_name": "x"}],
            }
        )
    )
    # The sqlglot validator should reject anything that isn't a SELECT,
    # even though we relaxed read_only=False at the DB layer to allow
    # CREATE VIEW during connect.
    with pytest.raises(ValueError, match="Refusing to execute"):
        await engine.execute("INSERT INTO x VALUES (2, 2)")


# ── Multiple attachments in one connection ───────────────────────


@pytest.mark.asyncio
async def test_multiple_attached_files(tmp_path: Path) -> None:
    a = tmp_path / "a.csv"
    a.write_text("id,v\n1,10\n2,20\n", encoding="utf-8")
    b = tmp_path / "b.csv"
    b.write_text("id,v\n1,100\n2,200\n", encoding="utf-8")

    engine = DuckdbEngine(
        _source(
            {
                "path": ":memory:",
                "attached_files": [
                    {"path": str(a), "view_name": "left_t"},
                    {"path": str(b), "view_name": "right_t"},
                ],
            }
        )
    )

    bundle = await engine.introspect_schema()
    names = {t.name for t in bundle.tables}
    assert {"left_t", "right_t"} <= names

    rs = await engine.execute(
        "SELECT l.id, l.v + r.v AS total "
        "FROM left_t l JOIN right_t r USING (id) ORDER BY l.id"
    )
    assert rs.rows == [[1, 110], [2, 220]]
