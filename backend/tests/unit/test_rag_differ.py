from __future__ import annotations

from app.engines.base import (
    ColumnMeta,
    ForeignKeyMeta,
    SchemaBundle,
    TableMeta,
)
from app.services.rag.differ import schema_changed


def _make(columns: dict[str, list[tuple[str, str, bool, bool]]]) -> SchemaBundle:
    """Compact constructor: {table: [(col_name, dtype, nullable, is_pk)]}"""
    tables = []
    for tname, cols in columns.items():
        tables.append(
            TableMeta(
                schema="public",
                name=tname,
                columns=[
                    ColumnMeta(name=c, data_type=dt, nullable=n, is_pk=pk)
                    for (c, dt, n, pk) in cols
                ],
            )
        )
    return SchemaBundle(dialect="postgres", tables=tables)


def test_no_old_bundle_reports_all_added() -> None:
    new = _make({"orders": [("id", "bigint", False, True)]})
    diff = schema_changed(None, new)
    assert diff.changed
    assert diff.added_tables == ["public.orders"]
    assert not diff.removed_tables
    assert not diff.modified_tables


def test_identical_bundles_no_change() -> None:
    a = _make({"orders": [("id", "bigint", False, True)]})
    b = _make({"orders": [("id", "bigint", False, True)]})
    diff = schema_changed(a, b)
    assert not diff.changed


def test_added_table_detected() -> None:
    a = _make({"orders": [("id", "bigint", False, True)]})
    b = _make(
        {
            "orders": [("id", "bigint", False, True)],
            "customers": [("id", "bigint", False, True)],
        }
    )
    diff = schema_changed(a, b)
    assert diff.added_tables == ["public.customers"]
    assert not diff.removed_tables
    assert not diff.modified_tables


def test_removed_table_detected() -> None:
    a = _make(
        {
            "orders": [("id", "bigint", False, True)],
            "old_stuff": [("id", "bigint", False, True)],
        }
    )
    b = _make({"orders": [("id", "bigint", False, True)]})
    diff = schema_changed(a, b)
    assert diff.removed_tables == ["public.old_stuff"]


def test_modified_column_type_detected() -> None:
    a = _make({"orders": [("amount", "int", True, False)]})
    b = _make({"orders": [("amount", "numeric", True, False)]})
    diff = schema_changed(a, b)
    assert diff.modified_tables == ["public.orders"]


def test_new_column_detected_as_modified() -> None:
    a = _make({"orders": [("id", "bigint", False, True)]})
    b = _make(
        {
            "orders": [
                ("id", "bigint", False, True),
                ("amount", "numeric", True, False),
            ]
        }
    )
    diff = schema_changed(a, b)
    assert diff.modified_tables == ["public.orders"]


def test_row_count_changes_dont_matter() -> None:
    a = SchemaBundle(
        dialect="postgres",
        tables=[
            TableMeta(
                schema="public",
                name="orders",
                columns=[ColumnMeta(name="id", data_type="bigint", nullable=False, is_pk=True)],
                row_count_estimate=10,
            )
        ],
    )
    b = SchemaBundle(
        dialect="postgres",
        tables=[
            TableMeta(
                schema="public",
                name="orders",
                columns=[ColumnMeta(name="id", data_type="bigint", nullable=False, is_pk=True)],
                row_count_estimate=1_000_000,
            )
        ],
    )
    assert not schema_changed(a, b).changed


def test_fk_addition_detected() -> None:
    a = _make(
        {
            "orders": [("customer_id", "bigint", False, False)],
            "customers": [("id", "bigint", False, True)],
        }
    )
    b = _make(
        {
            "orders": [("customer_id", "bigint", False, False)],
            "customers": [("id", "bigint", False, True)],
        }
    )
    # mutate b to add an FK on orders
    b.tables[0].foreign_keys = [
        ForeignKeyMeta(
            from_columns=["customer_id"], to_table="customers", to_columns=["id"]
        )
    ]
    diff = schema_changed(a, b)
    assert diff.modified_tables == ["public.orders"]
