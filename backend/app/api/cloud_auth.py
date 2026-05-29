"""HTTP wrappers around the OAuth helpers in :mod:`services.cloud_auth`.

Today only OneDrive (Microsoft Graph) needs a UI-driven auth flow —
Google Drive uses a service-account JSON key the user pastes directly
and Google Drive has no device-code grant we want to surface.

The two routes here let the frontend run the MS device-code flow
without the user touching curl:

  ``POST /cloud-auth/onedrive/start``
      Body: ``{client_id, tenant?}``. Calls Microsoft's devicecode
      endpoint and returns ``{device_code, user_code,
      verification_uri, expires_in, interval, message}``. The
      frontend shows the user_code + verification_uri in a modal so
      the user can finish auth in a separate tab.

  ``POST /cloud-auth/onedrive/poll``
      Body: ``{client_id, device_code, tenant?}``. Polls MS's token
      endpoint once with the device_code. Returns one of:

        * ``{status: "pending"}``     — user hasn't finished yet
        * ``{status: "slow_down"}``   — caller should poll less often
        * ``{status: "expired"}``     — device_code dead; restart
        * ``{status: "denied"}``      — user refused consent
        * ``{status: "ok",
              access_token, refresh_token, expires_in,
              expires_at}``           — tokens ready; ``expires_at``
                                        is an absolute UTC ISO 8601
                                        timestamp so the frontend can
                                        persist it directly into the
                                        DocSource.config.

These routes never touch the metadata DB — they're a thin façade so
the browser doesn't have to talk to login.microsoftonline.com cross-
origin. The DocSource (with its access_token + refresh_token) is
created separately via ``POST /workspaces/{ws}/doc-sources`` once the
frontend has the tokens in hand.

Auth: routes require a valid user JWT, since they're privileged
(running the device flow against a third-party service on behalf of
the caller). Per-user rate limit is the same as ``/auth/*``
(5/minute) so a runaway loop can't hammer Microsoft's endpoint and
get our IP throttled.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_current_user
from app.db.models import User
from app.limiter import limiter
from app.services.cloud_auth import (
    ONEDRIVE_SCOPES,
    onedrive_device_flow_poll,
    onedrive_device_flow_start,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cloud-auth", tags=["cloud-auth"])


class OneDriveStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)
    tenant: str = Field(default="common")


class OneDriveStartResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    message: str


class OneDrivePollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)
    device_code: str = Field(min_length=1)
    tenant: str = Field(default="common")


class OneDrivePollResponse(BaseModel):
    # Discriminated by ``status``. We deliberately don't model the
    # token fields as required so the same model handles both pending
    # and ok responses.
    status: str
    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None
    expires_at: str | None = None
    detail: str | None = None


@router.post(
    "/onedrive/start",
    response_model=OneDriveStartResponse,
)
@limiter.limit("5/minute")
async def onedrive_start(
    request: Request,  # noqa: ARG001 — slowapi requires Request positional
    payload: OneDriveStartRequest,
    current_user: User = Depends(get_current_user),  # noqa: ARG001
) -> OneDriveStartResponse:
    try:
        body = await onedrive_device_flow_start(
            payload.client_id,
            tenant=payload.tenant,
            scopes=ONEDRIVE_SCOPES,
        )
    except RuntimeError as e:
        # MS returned a non-2xx with an error payload. Surface the
        # message verbatim — it's the most useful debug signal
        # (e.g. "AADSTS70016: client_id not found").
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    return OneDriveStartResponse(
        device_code=body["device_code"],
        user_code=body["user_code"],
        verification_uri=body["verification_uri"],
        expires_in=int(body.get("expires_in", 900)),
        interval=int(body.get("interval", 5)),
        message=body.get("message", ""),
    )


@router.post(
    "/onedrive/poll",
    response_model=OneDrivePollResponse,
)
@limiter.limit("30/minute")
async def onedrive_poll(
    request: Request,  # noqa: ARG001
    payload: OneDrivePollRequest,
    current_user: User = Depends(get_current_user),  # noqa: ARG001
) -> OneDrivePollResponse:
    try:
        body = await onedrive_device_flow_poll(
            payload.client_id,
            payload.device_code,
            tenant=payload.tenant,
        )
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    # MS encodes progress + final tokens in the same payload shape.
    # ``error`` is set while we wait; on success it's absent and
    # ``access_token`` arrives.
    err = body.get("error")
    if err:
        mapping = {
            "authorization_pending": "pending",
            "slow_down": "slow_down",
            "expired_token": "expired",
            "access_denied": "denied",
        }
        status_label = mapping.get(str(err), "error")
        return OneDrivePollResponse(
            status=status_label,
            detail=str(body.get("error_description") or err),
        )

    if "access_token" not in body:
        # Unexpected shape from MS — surface as 502 so the frontend
        # knows to stop polling.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Microsoft response missing access_token",
        )

    expires_in = int(body.get("expires_in", 3600))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return OneDrivePollResponse(
        status="ok",
        access_token=str(body["access_token"]),
        refresh_token=str(body.get("refresh_token") or ""),
        expires_in=expires_in,
        # ISO 8601 with 'Z' so the frontend doesn't have to guess the
        # tz. Strip microseconds for a cleaner display value.
        expires_at=expires_at.replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
    )


def _payload_for_test_inspection(
    body: dict[str, Any],
) -> OneDrivePollResponse:
    """Test-only helper that converts a raw MS response into the same
    OneDrivePollResponse shape the route returns. Saves a round trip
    through TestClient when verifying the mapping logic."""
    if "error" in body:
        mapping = {
            "authorization_pending": "pending",
            "slow_down": "slow_down",
            "expired_token": "expired",
            "access_denied": "denied",
        }
        return OneDrivePollResponse(
            status=mapping.get(str(body["error"]), "error"),
            detail=str(body.get("error_description") or body["error"]),
        )
    expires_in = int(body.get("expires_in", 3600))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return OneDrivePollResponse(
        status="ok",
        access_token=str(body["access_token"]),
        refresh_token=str(body.get("refresh_token") or ""),
        expires_in=expires_in,
        expires_at=expires_at.replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
    )
