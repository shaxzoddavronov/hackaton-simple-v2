"""OAuth helpers for cloud document sources.

Today only OneDrive (Microsoft Graph) needs a code path here — the
Google Drive harvester uses a service-account JSON key and doesn't need
any user flow. We hand-roll the MS device-code flow with plain httpx
rather than pulling in ``msal`` because the surface area we need is
small (3 endpoints) and ``msal`` brings a lot of broker / cache
machinery we don't want.

Flow:
  1. The frontend POSTs the user's Azure-AD ``client_id`` and we call
     :func:`onedrive_device_flow_start` — Microsoft returns a
     ``user_code`` + ``verification_uri`` the user types into a phone /
     other browser.
  2. While the user is auth'ing, the backend (or the frontend, polling
     through us) calls :func:`onedrive_device_flow_poll` every
     ``interval`` seconds. Once the user finishes, this returns an
     ``access_token`` + ``refresh_token`` + ``expires_in``.
  3. We persist both tokens into ``DocSource.config`` along with
     ``expires_at`` (UTC ISO 8601). The harvest task checks
     ``expires_at`` before each crawl and invokes
     :func:`refresh_onedrive_token` if the token is within 60s of
     expiry, writing the refreshed tokens back into ``config``.

The Azure-AD app registration is the user's responsibility — see the
README. Required permission: ``Files.Read.All`` (delegated). Public
client / mobile + desktop flows enabled so the device-code grant works
without a client secret.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


_MS_AUTHORITY = "https://login.microsoftonline.com"
# Files.Read.All gives read access to OneDrive + SharePoint files the
# user can already see. offline_access is what unlocks refresh_token.
ONEDRIVE_SCOPES = "offline_access Files.Read.All"
_HTTP_TIMEOUT_S = 20.0


async def onedrive_device_flow_start(
    client_id: str,
    *,
    tenant: str = "common",
    scopes: str = ONEDRIVE_SCOPES,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Begin the MS device-code grant.

    Returns the raw JSON Microsoft sends back, which includes:

    * ``device_code``        — opaque token the caller polls with.
    * ``user_code``          — short alphanumeric the user types in.
    * ``verification_uri``   — URL the user opens (e.g.
      ``https://microsoft.com/devicelogin``).
    * ``expires_in``         — seconds until ``device_code`` is dead.
    * ``interval``           — minimum poll interval (seconds).
    * ``message``            — human-readable instructions.

    ``tenant`` defaults to ``"common"`` which works for both personal
    and work accounts; pass a specific tenant GUID when restricting to
    a single Azure-AD directory.
    """
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
    url = f"{_MS_AUTHORITY}/{tenant}/oauth2/v2.0/devicecode"
    try:
        resp = await client.post(
            url,
            data={"client_id": client_id, "scope": scopes},
            headers={"Accept": "application/json"},
        )
    finally:
        if own:
            await client.aclose()
    if resp.status_code >= 400:
        raise RuntimeError(
            f"device_flow_start failed: HTTP {resp.status_code} — {resp.text[:300]}"
        )
    return resp.json()


async def onedrive_device_flow_poll(
    client_id: str,
    device_code: str,
    *,
    tenant: str = "common",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Poll the token endpoint once with the device_code.

    The caller is expected to poll repeatedly at the ``interval`` MS
    returned from ``device_flow_start``. Microsoft signals progress
    with a structured ``error`` field:

    * ``authorization_pending`` — user hasn't completed the prompt yet.
      Caller should sleep ``interval`` and try again.
    * ``slow_down``             — caller is polling too fast; bump
      ``interval`` by 5 seconds.
    * ``expired_token``         — device_code is dead; restart the flow.
    * ``access_denied``         — user clicked 'No' on the consent
      screen.

    On success returns ``{access_token, refresh_token, expires_in,
    token_type, scope}``. ``expires_in`` is seconds; the caller is
    expected to compute and persist an absolute ``expires_at``.
    """
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
    url = f"{_MS_AUTHORITY}/{tenant}/oauth2/v2.0/token"
    try:
        resp = await client.post(
            url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device_code,
            },
            headers={"Accept": "application/json"},
        )
    finally:
        if own:
            await client.aclose()
    payload = resp.json()
    if resp.status_code >= 400 and "error" not in payload:
        raise RuntimeError(
            f"device_flow_poll failed: HTTP {resp.status_code} — {resp.text[:300]}"
        )
    return payload


async def refresh_onedrive_token(
    client_id: str,
    refresh_token: str,
    *,
    tenant: str = "common",
    scopes: str = ONEDRIVE_SCOPES,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Exchange a refresh_token for a fresh access_token.

    MS rotates the refresh_token on each refresh, so the caller MUST
    write the returned ``refresh_token`` back into storage (the old
    one stops working). The harvest task does this in
    ``workers/harvest_task.py``.
    """
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
    url = f"{_MS_AUTHORITY}/{tenant}/oauth2/v2.0/token"
    try:
        resp = await client.post(
            url,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
                "scope": scopes,
            },
            headers={"Accept": "application/json"},
        )
    finally:
        if own:
            await client.aclose()
    if resp.status_code >= 400:
        raise RuntimeError(
            f"refresh_onedrive_token failed: HTTP {resp.status_code} — {resp.text[:300]}"
        )
    return resp.json()


__all__ = [
    "ONEDRIVE_SCOPES",
    "onedrive_device_flow_start",
    "onedrive_device_flow_poll",
    "refresh_onedrive_token",
]
