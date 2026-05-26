"""Schema-bundle diffing.

A single function: :func:`schema_changed` returns a small report describing
what is added / removed / modified between two bundles. The Celery diff task
uses it to decide whether to enqueue a re-index.

The granularity we care about is structural — added/removed tables, added/
removed/modified columns. Row-count estimates and sample values are ignored,
otherwise we'd re-embed every day on a busy production DB even though the
schema didn't change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.engines.base import ColumnMeta, SchemaBundle, TableMeta


@dataclass(slots=True)
class SchemaDiff:
    added_tables: list[str] = field(default_factory=list)
    removed_tables: list[str] = field(default_factory=list)
    modified_tables: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added_tables or self.removed_tables or self.modified_tables)


def schema_changed(old: SchemaBundle | None, new: SchemaBundle) -> SchemaDiff:
    """Compare two bundles by structural signature."""
    new_index = _index_tables(new)
    if old is None:
        return SchemaDiff(added_tables=sorted(new_index.keys()))

    old_index = _index_tables(old)

    added = sorted(k for k in new_index if k not in old_index)
    removed = sorted(k for k in old_index if k not in new_index)

    modified: list[str] = []
    for qname in sorted(set(old_index) & set(new_index)):
        if _table_signature(old_index[qname]) != _table_signature(new_index[qname]):
            modified.append(qname)

    return SchemaDiff(
        added_tables=added, removed_tables=removed, modified_tables=modified
    )


def _index_tables(b: SchemaBundle) -> dict[str, TableMeta]:
    return {f"{t.schema}.{t.name}": t for t in b.tables}


def _table_signature(t: TableMeta) -> tuple[Any, ...]:
    """Stable structural fingerprint: column-set + key-set + FK-set."""
    cols = tuple(
        (
            c.name,
            (c.data_type or "").lower(),
            bool(c.nullable),
            bool(c.is_pk),
            bool(c.is_unique),
            c.fk_to or "",
        )
        for c in sorted(t.columns, key=lambda c: c.name)
    )
    fks = tuple(
        (
            tuple(fk.from_columns),
            fk.to_table,
            tuple(fk.to_columns),
        )
        for fk in sorted(
            t.foreign_keys,
            key=lambda fk: (tuple(fk.from_columns), fk.to_table, tuple(fk.to_columns)),
        )
    )
    return (cols, fks)


__all__ = ["SchemaDiff", "schema_changed"]
