"""Tests for the OneDrive device-flow HTTP wrappers.

The two routes are thin façades over services.cloud_auth, so we mock
the helpers at module boundary and verify the route translates the
raw MS response into our discriminated status shape (pending /
slow_down / expired / denied / ok).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.api.cloud_auth import (
    OneDrivePollResponse,
    _payload_for_test_inspection,
)


# ── _payload_for_test_inspection ─────────────────────────────────


def test_payload_authorization_pending_maps_to_pending() -> None:
    out = _payload_for_test_inspection(
        {
            "error": "authorization_pending",
            "error_description": "User hasn't finished signing in.",
        }
    )
    assert out.status == "pending"
    assert "User hasn't" in (out.detail or "")
    assert out.access_token is None


def test_payload_slow_down_maps_to_slow_down() -> None:
    out = _payload_for_test_inspection({"error": "slow_down"})
    assert out.status == "slow_down"


def test_payload_expired_token_maps_to_expired() -> None:
    out = _payload_for_test_inspection({"error": "expired_token"})
    assert out.status == "expired"


def test_payload_access_denied_maps_to_denied() -> None:
    out = _payload_for_test_inspection(
        {"error": "access_denied", "error_description": "User refused"}
    )
    assert out.status == "denied"
    assert out.detail == "User refused"


def test_payload_unknown_error_maps_to_error() -> None:
    out = _payload_for_test_inspection({"error": "some_other_thing"})
    assert out.status == "error"


def test_payload_success_emits_tokens_and_expires_at() -> None:
    raw = {
        "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
        "refresh_token": "0.AAAAA-refresh",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "Files.Read.All offline_access",
    }
    out = _payload_for_test_inspection(raw)
    assert out.status == "ok"
    assert out.access_token.startswith("eyJ0")
    assert out.refresh_token == "0.AAAAA-refresh"
    assert out.expires_in == 3600
    # ISO 8601 with Z suffix, no microseconds.
    assert out.expires_at is not None
    assert out.expires_at.endswith("Z")
    parsed = datetime.fromisoformat(out.expires_at.replace("Z", "+00:00"))
    # The expiry is roughly an hour in the future (allow generous skew
    # for slow CI machines).
    delta = (parsed - datetime.now(timezone.utc)).total_seconds()
    assert 3000 < delta < 3700


def test_payload_success_handles_missing_refresh_token() -> None:
    # Some MS responses (rare — happens when scope=offline_access was
    # omitted on the start call) come back without a refresh_token.
    # We should still emit "ok" with refresh_token="" so the frontend
    # can persist an access_token-only DocSource.
    raw = {
        "access_token": "tok",
        "expires_in": 3600,
    }
    out = _payload_for_test_inspection(raw)
    assert out.status == "ok"
    assert out.access_token == "tok"
    assert out.refresh_token == ""


def test_payload_success_defaults_expires_in_when_missing() -> None:
    # If MS somehow omits expires_in, fall back to 3600 (1 hour).
    raw = {"access_token": "tok"}
    out = _payload_for_test_inspection(raw)
    assert out.status == "ok"
    assert out.expires_in == 3600


# ── shape of the response model ───────────────────────────────────


def test_poll_response_supports_optional_token_fields() -> None:
    # Pending shape — no tokens, only status+detail.
    p = OneDrivePollResponse(status="pending", detail="waiting")
    assert p.status == "pending"
    assert p.access_token is None

    # Success shape — tokens populated.
    s = OneDrivePollResponse(
        status="ok",
        access_token="t",
        refresh_token="r",
        expires_in=3600,
        expires_at="2026-05-29T13:00:00Z",
    )
    assert s.access_token == "t"
