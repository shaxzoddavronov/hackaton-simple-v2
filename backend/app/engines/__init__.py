"""Engines package.

This module is intentionally side-effect-free so importing
``app.engines.base`` from anywhere — including the read-only validator —
does not eagerly load concrete adapters.

Concrete adapters register themselves with :data:`DIALECT_REGISTRY` only
when their module is imported. Callers that need *any* dialect resolved
(API routes, Celery tasks, app startup) must explicitly call
:func:`register_all` once before invoking ``get_engine``.
"""
from __future__ import annotations

from app.engines.registry import DIALECT_REGISTRY, get_engine


def register_all() -> None:
    """Import every concrete adapter so they self-register.

    Idempotent — Python caches modules so repeated calls are free.
    """
    from app.engines import postgres as _postgres  # noqa: F401
    from app.engines import sqlite as _sqlite  # noqa: F401
    from app.engines import mysql as _mysql  # noqa: F401
    from app.engines import elasticsearch as _es  # noqa: F401
    from app.engines import clickhouse as _ch  # noqa: F401
    from app.engines import mongodb as _mongo  # noqa: F401


__all__ = ["DIALECT_REGISTRY", "get_engine", "register_all"]
