"""Audit log writer + FastAPI middleware (Phase 16).

Two public entry points:

  * :func:`log_action` — explicit, used inside handlers when the
    action's semantics matter (``user.create``, ``permission.grant``,
    ``chat.turn``, ``auth.login``). Writes a single
    :class:`AuditLog` row.

  * :class:`AuditMiddleware` — wraps every non-trivial HTTP request
    and writes a coarse ``http.{METHOD}.{path_pattern}`` row with
    status, user_id (if authenticated), client IP, user agent.

Design notes:

  * Writes happen in the request's own session (handler-side) or in
    a short-lived ad-hoc session (middleware), never blocking the
    response. We swallow write failures with a warning log so an
    audit-write outage never breaks the user-visible request path.
  * No FK on ``target_id`` — referenced rows may be deleted; the log
    preserves history regardless.
  * Payloads are JSON-bounded by the caller. Keep them small (single-
    digit KB at most); large payloads belong in a separate event
    bus, not the metadata DB.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.config import settings
from app.db.models import AuditLog

log = logging.getLogger(__name__)


# Paths we don't bother to audit — they're high-frequency and add only
# noise. ``/metrics`` is scraped every 15 s; healthchecks similar.
_NOISY_PATH_PREFIXES = (
    "/metrics",
    "/healthz",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
)


async def log_action(
    session: AsyncSession,
    *,
    action: str,
    user_id: UUID | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    status: str = "ok",
    payload: dict[str, Any] | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Write one row to ``audit_log``. Best-effort — exceptions are
    swallowed with a warning. Caller decides whether to flush /
    commit; we just ``session.add`` so the row joins the surrounding
    transaction. If the surrounding tx rolls back, the audit entry
    rolls back too, which is the correct semantic ("failed write +
    no audit row" is consistent)."""
    try:
        session.add(
            AuditLog(
                user_id=user_id,
                action=action[:64],
                target_kind=(target_kind or None) and target_kind[:32],
                target_id=(target_id or None) and target_id[:64],
                status=status if status in ("ok", "error", "denied") else "ok",
                payload=payload or {},
                client_ip=(client_ip or None) and client_ip[:64],
                user_agent=(user_agent or None) and user_agent[:255],
            )
        )
    except Exception:
        log.exception("audit.log_action(action=%s) failed", action)


def _should_audit(path: str) -> bool:
    return not any(path.startswith(p) for p in _NOISY_PATH_PREFIXES)


def _extract_user_id_from_request(request: Request) -> UUID | None:
    """Pull the authenticated user UUID from the JWT cheaply — without
    a DB round-trip. Returns ``None`` for anonymous / unparseable
    requests; the audit row stays anonymous in that case.

    We don't fail on bad tokens here — :mod:`api.deps` enforces auth
    on the actual route; the middleware is just observability."""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[len("bearer ") :].strip()
    try:
        from jose import jwt

        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALG],
        )
        sub = payload.get("sub")
        if isinstance(sub, str):
            try:
                return UUID(sub)
            except ValueError:
                return None
    except Exception:
        return None
    return None


class AuditMiddleware(BaseHTTPMiddleware):
    """Append one audit row per HTTP request (minus noisy paths).

    Runs AFTER the response so we can stamp the status_code. Uses a
    dedicated short-lived session to avoid sharing with the request's
    own DB transaction (which may have rolled back on a 4xx/5xx).
    """

    def __init__(self, app, sessionmaker_factory=None) -> None:
        super().__init__(app)
        self._sessionmaker = sessionmaker_factory

    def _maker(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is None:
            engine = create_async_engine(
                settings.DATABASE_URL, pool_pre_ping=True
            )
            self._sessionmaker = async_sessionmaker(
                engine, expire_on_commit=False
            )
        return self._sessionmaker

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        response = await call_next(request)
        if not _should_audit(path):
            return response

        # Compose a coarse action key. The route template (e.g.
        # ``/workspaces/{workspace_id}``) lives on
        # ``request.scope["route"].path``; falling back to the
        # concrete path is fine — it's lower cardinality than user-
        # supplied query strings.
        route = request.scope.get("route")
        path_pattern = (
            getattr(route, "path", path) if route is not None else path
        )
        action = f"http.{request.method.upper()}.{path_pattern}"[:64]

        # Audit-row status reflects the HTTP outcome class.
        if 200 <= response.status_code < 400:
            row_status = "ok"
        elif response.status_code in (401, 403):
            row_status = "denied"
        else:
            row_status = "error"

        user_id = _extract_user_id_from_request(request)
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")

        try:
            Session = self._maker()
            async with Session() as session:
                await log_action(
                    session,
                    action=action,
                    user_id=user_id,
                    target_kind="http",
                    target_id=path[:64],
                    status=row_status,
                    payload={"status_code": response.status_code},
                    client_ip=client_ip,
                    user_agent=user_agent,
                )
                await session.commit()
        except Exception:
            # Audit failure NEVER breaks the user-visible response.
            log.warning(
                "audit middleware: write failed for %s %s",
                request.method, path,
                exc_info=True,
            )

        return response


__all__ = ["log_action", "AuditMiddleware"]
