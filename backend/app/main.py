"""FastAPI application factory for QueryMind AI.

Owns the lifespan hook (engine setup + vLLM reachability probe) and the
top-level router wiring. Importing this module does *not* start the
event loop — call ``create_app()`` (or run ``uvicorn app.main:app``).
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Make our INFO logs visible in the uvicorn console. uvicorn ships with
# its own handler on the root logger; we just need to ensure our level
# isn't suppressed. Format includes the logger name so per-stream trace
# ids land in a single column.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Keep the noisier libraries at WARNING so our traces stay readable.
for noisy in ("httpx", "httpcore", "asyncio", "sqlalchemy.engine"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from app.api import (
    auth,
    chat,
    documents,
    schema,
    settings as settings_router,
    workspaces,
)
from app.config import settings
from app.db.session import engine
from app.engines import register_all as register_engines

# Eagerly register concrete engine adapters so `get_engine(workspace)` works
# from the first request — kept out of `app.engines.__init__` to avoid a
# circular import with `services.readonly_validator`.
register_engines()

logger = logging.getLogger("querymind.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hooks.

    Startup:
      * Touch the metadata-DB engine pool so misconfiguration fails fast
        (instead of on the first request).
      * Ping the local vLLM server. We *warn* on failure rather than
        crash — vLLM may be slow to come up in dev, and an external
        health check should report the degraded state.

    Shutdown:
      * Dispose of the async engine so connection-pool sockets close
        cleanly.
    """
    # `engine` is already created at import time; this is a no-op check
    # that the URL parsed cleanly and the dialect driver imported.
    _ = engine.url

    vllm_health_url = f"{settings.VLLM_ENDPOINT.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.get(vllm_health_url)
            response.raise_for_status()
        logger.info("vLLM reachable at %s", vllm_health_url)
    except Exception as exc:  # noqa: BLE001 — degraded mode is allowed
        logger.warning(
            "vLLM unreachable at %s (%s). Agent nodes will fail until it comes up.",
            vllm_health_url,
            exc,
        )

    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="QueryMind AI",
        version="0.1.0",
        description=(
            "Self-hosted NL-to-SQL over user-connected databases. "
            "All inference runs locally via vLLM."
        ),
        lifespan=lifespan,
    )

    # Allowed origins come from Settings.CORS_ORIGINS so dev (:3001) and
    # any prod host can both work without code edits. NOTE: when
    # ``allow_credentials=True`` you MUST list explicit origins —
    # browsers reject ``Access-Control-Allow-Origin: *`` in combination
    # with credentials, which silently breaks every fetch from the SPA.
    origins = [
        o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers are stubs in Wave 1 — they'll grow real routes in later waves.
    app.include_router(auth.router)
    app.include_router(workspaces.router)
    app.include_router(chat.router)
    app.include_router(schema.router)
    app.include_router(settings_router.router)
    app.include_router(documents.router)

    @app.get("/healthz", tags=["health"], include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness probe — does not touch the DB or vLLM."""
        return {"status": "ok"}

    return app


# Importable as `uvicorn app.main:app`.
app = create_app()
