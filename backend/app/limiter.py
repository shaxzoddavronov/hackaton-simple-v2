"""Process-wide slowapi ``Limiter`` instance.

This module exists purely to break an import cycle: ``app.main`` imports
the routers in ``app.api.*``, and those routers need ``limiter`` to
decorate their endpoints. If we created the ``Limiter`` inside
``app.main`` and re-exported it, the routers would have to import from
``app.main`` while ``app.main`` is still executing its module body — a
classic circular import.

Putting the singleton here means both sides import from a leaf module
that has no QueryMind-internal deps beyond ``app.config``.

Rate-limit key strategy
-----------------------
* If the request carries an ``Authorization: Bearer <token>`` header,
  we key on the first 20 chars of the token. That's enough to make the
  bucket per-user without decoding the JWT in the hot path (which would
  add CPU + a config dependency to every request).
* Otherwise we fall back to the client IP via slowapi's default helper.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings


def _key_func(request) -> str:
    """Return a stable per-caller key for the rate-limit bucket."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        # Token prefix is unique-enough per user without a JWT decode.
        return f"tok:{auth[7:27]}"
    return f"ip:{get_remote_address(request)}"


# ``default_limits=[]`` so no endpoint is implicitly limited — we
# decorate the sensitive routes explicitly. ``fixed-window`` is the
# Redis-friendly strategy (single INCR + EXPIRE per request);
# moving-window would be more accurate but costs an extra round trip.
limiter = Limiter(
    key_func=_key_func,
    storage_uri=settings.RATE_LIMIT_STORAGE_URL,
    default_limits=[],
    strategy="fixed-window",
)
