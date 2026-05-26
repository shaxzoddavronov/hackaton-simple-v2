"""Introspect QueryMind's own FastAPI app to feed the RAG index.

This lives in ``services/rag`` rather than ``api/`` because nothing in
``api/`` should import from agents/services — we keep the dependency arrow
pointing in one direction.

We import the FastAPI app lazily (inside the function) to avoid a circular
import: ``app.main`` already imports routers that import this module's
parent package via the indexer.
"""
from __future__ import annotations

from typing import Any, Iterable


def iter_routes() -> Iterable[dict[str, Any]]:
    """Yield ``{method, path, summary, description}`` for each documented route.

    Excludes the auto-generated ``/openapi.json``, ``/docs``, ``/redoc``,
    and HEAD/OPTIONS variants that FastAPI registers automatically.
    """
    from app.main import app  # local import — see module docstring

    seen: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        endpoint = getattr(route, "endpoint", None)
        if not path or not methods:
            continue
        if path in {"/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}:
            continue
        for method in sorted(methods):
            if method in {"HEAD", "OPTIONS"}:
                continue
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            summary = getattr(route, "summary", None) or ""
            description = getattr(route, "description", None) or ""
            if not description and endpoint is not None:
                description = (endpoint.__doc__ or "").strip()
            if not summary and endpoint is not None:
                # Default to the function name humanized.
                summary = endpoint.__name__.replace("_", " ").strip()
            yield {
                "method": method,
                "path": path,
                "summary": summary,
                "description": description,
            }


__all__ = ["iter_routes"]
