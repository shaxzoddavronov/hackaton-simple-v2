from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _engine(db_path: Path):
    from app.engines.duckdb import DuckdbEngine

    workspace = SimpleNamespace(
        dialect="duckdb",
        connection_meta={"path": str(db_path)},
    )
    return DuckdbEngine(workspace)


@pytest.fixture()
def sales_db(tmp_path: Path) -> Path:
    """File-backed DuckDB seeded via a writable connection.

    The engine itself opens read-only — seeding must happen out-of-band.
    """
    import duckdb

    db = tmp_path / "sales.duckdb"
    rw = duckdb.connect(str(db))
    rw.execute(
        """
        CREATE TABLE customers(
            customer_id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL
        )
        """
    )
    rw.execute(
        """
        CREATE TABLE sales(
            order_id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(customer_id),
            ts TIMESTAMP NOT NULL,
            amount DOUBLE NOT NULL,
            region VARCHAR NOT NULL
        )
        """
    )
    rw.execute(
        "INSERT INTO customers VALUES (1,'Alice'),(2,'Bob'),(3,'Carol'),(4,'Dan')"
    )
    rw.execute(
        """
        INSERT INTO sales VALUES
          (1,1,'2024-01-01 00:00:00',50.0,'NA'),
          (2,2,'2024-01-02 00:00:00',100.0,'NA'),
          (3,3,'2024-01-03 00:00:00',25.0,'EU'),
          (4,4,'2024-01-04 00:00:00',200.0,'EU'),
          (5,1,'2024-01-05 00:00:00',75.0,'APAC')
        """
    )
    rw.close()
    return db


@pytest.mark.asyncio
async def test_introspect_returns_tables_columns_fks(sales_db: Path) -> None:
    engine = _engine(sales_db)
    bundle = await engine.introspect_schema()
    names = {t.name for t in bundle.tables}
    assert names == {"customers", "sales"}

    sales = next(t for t in bundle.tables if t.name == "sales")
    col_names = [c.name for c in sales.columns]
    assert col_names == ["order_id", "customer_id", "ts", "amount", "region"]
    assert sales.row_count_estimate == 5

    fk = next(c for c in sales.columns if c.name == "customer_id")
    assert fk.fk_to == "main.customers.customer_id"


@pytest.mark.asyncio
async def test_execute_returns_columns_and_rows(sales_db: Path) -> None:
    engine = _engine(sales_db)
    rs = await engine.execute(
        "SELECT region, SUM(amount) AS total FROM sales "
        "GROUP BY region ORDER BY region"
    )
    assert rs.columns == ["region", "total"]
    assert rs.row_count == 3
    regions = {row[0] for row in rs.rows}
    assert regions == {"APAC", "EU", "NA"}
    # dtypes mapped from DuckDB type codes.
    assert rs.dtypes[0] == "string"
    assert rs.dtypes[1] == "double"


@pytest.mark.asyncio
async def test_execute_respects_row_cap(sales_db: Path) -> None:
    engine = _engine(sales_db)
    rs = await engine.execute("SELECT * FROM sales", row_cap=2)
    assert rs.row_count == 2
    assert rs.truncated is True
    assert rs.columns == ["order_id", "customer_id", "ts", "amount", "region"]


@pytest.mark.asyncio
async def test_execute_refuses_write(tmp_path: Path) -> None:
    import duckdb

    db = tmp_path / "ro.duckdb"
    rw = duckdb.connect(str(db))
    rw.execute("CREATE TABLE t(x INTEGER)")
    rw.close()

    engine = _engine(db)
    # Parse-time refusal via the read-only validator.
    with pytest.raises(ValueError, match="Refusing to execute"):
        await engine.execute("DROP TABLE t")
    with pytest.raises(ValueError, match="Refusing to execute"):
        await engine.execute("INSERT INTO t VALUES (1)")


@pytest.mark.asyncio
async def test_runtime_blocks_writes_even_if_validator_bypassed(
    tmp_path: Path,
) -> None:
    """Belt-and-braces: opening with read_only=True must also block at the DB layer."""
    import duckdb

    db = tmp_path / "rt.duckdb"
    rw = duckdb.connect(str(db))
    rw.execute("CREATE TABLE t(x INTEGER)")
    rw.execute("INSERT INTO t VALUES (1)")
    rw.close()

    engine = _engine(db)
    # Open exactly like the engine would and try to write.
    conn = engine._connect_sync()  # noqa: SLF001 (test-only)
    try:
        with pytest.raises(Exception):
            conn.execute("DROP TABLE t")
    finally:
        conn.close()


def test_validate_readonly_delegates_to_validator(tmp_path: Path) -> None:
    db = tmp_path / "v.duckdb"
    import duckdb

    duckdb.connect(str(db)).close()
    engine = _engine(db)
    assert engine.validate_readonly("SELECT 1").ok is True
    assert engine.validate_readonly("DROP TABLE t").ok is False


def test_missing_path_raises() -> None:
    from app.engines.duckdb import DuckdbEngine

    workspace = SimpleNamespace(dialect="duckdb", connection_meta={})
    with pytest.raises(ValueError, match="path"):
        DuckdbEngine(workspace)


def test_duckdb_dtype_helper() -> None:
    from app.engines.duckdb import _duckdb_dtype

    assert _duckdb_dtype("BIGINT") == "bigint"
    assert _duckdb_dtype("INTEGER") == "bigint"
    assert _duckdb_dtype("DOUBLE") == "double"
    assert _duckdb_dtype("FLOAT") == "double"
    assert _duckdb_dtype("REAL") == "double"
    assert _duckdb_dtype("DECIMAL(10,2)") == "numeric"
    assert _duckdb_dtype("NUMERIC") == "numeric"
    assert _duckdb_dtype("TIMESTAMP") == "timestamp"
    assert _duckdb_dtype("DATE") == "timestamp"
    assert _duckdb_dtype("BOOLEAN") == "bool"
    assert _duckdb_dtype("VARCHAR") == "string"
    assert _duckdb_dtype("UNKNOWN") == "string"
