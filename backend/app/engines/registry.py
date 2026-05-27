from __future__ import annotations

from typing import Type

from app.engines.base import Dialect, QueryEngine

DIALECT_REGISTRY: dict[Dialect, Type[QueryEngine]] = {}


def register(dialect: Dialect):
    def deco(cls: Type[QueryEngine]) -> Type[QueryEngine]:
        DIALECT_REGISTRY[dialect] = cls
        return cls

    return deco


def get_engine(source) -> QueryEngine:
    """Construct a QueryEngine for ``source``.

    ``source`` is duck-typed: any object with ``dialect`` and
    ``connection_meta`` attributes works, plus an optional ``_credentials``
    dict (decrypted credentials, attached by callers). Both
    :class:`WorkspaceConnection` ORM rows and ad-hoc ``SimpleNamespace``
    objects (used by ``test_connection`` and tests) satisfy the shape.
    """
    dialect: Dialect = source.dialect
    if dialect not in DIALECT_REGISTRY:
        raise ValueError(
            f"No engine registered for dialect {dialect!r}. "
            f"Known: {sorted(DIALECT_REGISTRY)}"
        )
    return DIALECT_REGISTRY[dialect](source)
