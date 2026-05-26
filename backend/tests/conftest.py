"""Shared pytest fixtures / environment.

Registers a ``sqlite3`` adapter for :class:`uuid.UUID` so tests that hit
SQLite (most of them) can bind a UUID parameter without manually
``str()``-ing it. Production runs Postgres + asyncpg, which handles UUIDs
natively, so this is test-only plumbing.
"""
from __future__ import annotations

import sqlite3
import uuid


sqlite3.register_adapter(uuid.UUID, lambda u: str(u))
