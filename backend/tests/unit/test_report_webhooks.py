"""Phase 33 — webhook fan-out for scheduled reports.

Tests cover:
  * URL parsing (newline + comma + comment + whitespace tolerance)
  * Host-based payload dispatch (Slack / Teams / Discord / generic)
  * Slack block-kit shape and Slack's 45-section trim
  * Discord 10-embed cap
  * Per-URL outcome isolation: one bad URL doesn't poison the others
  * URL hygiene: rejects non-http schemes and missing netloc
  * httpx transport mock — verify the right body lands at the URL
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.report_email import CardRender
from app.services.report_webhooks import (
    WebhookOutcome,
    _is_discord,
    _is_slack,
    _is_teams,
    build_payload,
    fan_out_webhooks,
    parse_webhook_urls,
)


# ── Card fixture ─────────────────────────────────────────────────


def _cards(n: int = 2) -> list[CardRender]:
    out: list[CardRender] = []
    for i in range(n):
        out.append(
            CardRender(
                title=f"Q{i}",
                prompt=f"How many orders today (q{i})?",
                headline=f"{i * 100} orders",
                body_md=f"Body **markdown** for question {i}.",
            )
        )
    return out


# ── parse_webhook_urls ───────────────────────────────────────────


def test_parse_urls_empty_returns_empty() -> None:
    assert parse_webhook_urls("") == []
    assert parse_webhook_urls(None) == []


def test_parse_urls_splits_on_newline() -> None:
    raw = "https://a.example/hook\nhttps://b.example/hook"
    assert parse_webhook_urls(raw) == [
        "https://a.example/hook",
        "https://b.example/hook",
    ]


def test_parse_urls_splits_on_commas() -> None:
    raw = "https://a.example, https://b.example"
    assert parse_webhook_urls(raw) == [
        "https://a.example",
        "https://b.example",
    ]


def test_parse_urls_ignores_comments_and_blanks() -> None:
    raw = (
        "# main slack\nhttps://hooks.slack.com/services/T/A/x\n"
        "\n"
        "  # disabled — broken\n"
        "https://b.example/hook\n"
    )
    assert parse_webhook_urls(raw) == [
        "https://hooks.slack.com/services/T/A/x",
        "https://b.example/hook",
    ]


def test_parse_urls_trims_whitespace() -> None:
    assert parse_webhook_urls("  https://a.example   ") == [
        "https://a.example"
    ]


# ── host-based dispatch ─────────────────────────────────────────


def test_is_slack_recognises_hook_url() -> None:
    assert _is_slack("https://hooks.slack.com/services/T1/B1/abc")


def test_is_slack_rejects_other_hosts() -> None:
    assert not _is_slack("https://example.com/slack")


def test_is_teams_recognises_office_subdomain() -> None:
    assert _is_teams(
        "https://acme.webhook.office.com/webhookb2/abc/IncomingWebhook/x/y"
    )


def test_is_discord_requires_api_webhooks_path() -> None:
    assert _is_discord(
        "https://discord.com/api/webhooks/123/abc"
    )
    assert not _is_discord("https://discord.com/")


# ── build_payload — Slack ───────────────────────────────────────


def test_build_payload_slack_uses_blocks() -> None:
    payload = build_payload(
        url="https://hooks.slack.com/services/T/A/X",
        dashboard_name="Sales",
        workspace_name="Acme",
        dashboard_url="https://app.example/d/1",
        cards=_cards(3),
        generated_at_iso="2026-06-02T00:00:00",
    )
    assert "blocks" in payload
    assert payload["blocks"][0]["type"] == "header"
    # action block at the tail
    assert payload["blocks"][-1]["type"] == "actions"
    assert (
        payload["blocks"][-1]["elements"][0]["url"]
        == "https://app.example/d/1"
    )


def test_build_payload_slack_caps_sections() -> None:
    payload = build_payload(
        url="https://hooks.slack.com/services/T/A/X",
        dashboard_name="Big",
        workspace_name="W",
        dashboard_url="#",
        cards=_cards(60),  # exceeds the 45-section cap
        generated_at_iso="2026-06-02T00:00:00",
    )
    sections = [
        b for b in payload["blocks"] if b.get("type") == "section"
    ]
    assert len(sections) == 45


def test_build_payload_slack_renders_card_error() -> None:
    cards = [
        CardRender(
            title="Broken",
            prompt="x",
            headline=None,
            body_md=None,
            error="connection refused",
        )
    ]
    payload = build_payload(
        url="https://hooks.slack.com/services/T/A/X",
        dashboard_name="D",
        workspace_name="W",
        dashboard_url="#",
        cards=cards,
        generated_at_iso="t",
    )
    rendered = json.dumps(payload)
    assert "connection refused" in rendered
    assert ":warning:" in rendered


# ── build_payload — Teams ───────────────────────────────────────


def test_build_payload_teams_is_messagecard() -> None:
    payload = build_payload(
        url="https://acme.webhook.office.com/webhookb2/abc/x/y",
        dashboard_name="Sales",
        workspace_name="Acme",
        dashboard_url="https://app.example/d/1",
        cards=_cards(2),
        generated_at_iso="2026-06-02T00:00:00",
    )
    assert payload["@type"] == "MessageCard"
    assert payload["title"] == "Sales"
    assert len(payload["sections"]) == 2
    assert payload["potentialAction"][0]["targets"][0]["uri"] == (
        "https://app.example/d/1"
    )


# ── build_payload — Discord ─────────────────────────────────────


def test_build_payload_discord_caps_at_10_embeds() -> None:
    payload = build_payload(
        url="https://discord.com/api/webhooks/123/xyz",
        dashboard_name="Big",
        workspace_name="W",
        dashboard_url="#",
        cards=_cards(20),
        generated_at_iso="t",
    )
    assert len(payload["embeds"]) == 10


def test_build_payload_discord_includes_dashboard_url() -> None:
    payload = build_payload(
        url="https://discord.com/api/webhooks/123/xyz",
        dashboard_name="D",
        workspace_name="W",
        dashboard_url="https://app.example/d/1",
        cards=_cards(1),
        generated_at_iso="t",
    )
    assert "https://app.example/d/1" in payload["content"]


# ── build_payload — generic ─────────────────────────────────────


def test_build_payload_generic_includes_full_cards() -> None:
    payload = build_payload(
        url="https://example.com/incoming",
        dashboard_name="D",
        workspace_name="W",
        dashboard_url="https://app.example/d/1",
        cards=_cards(2),
        generated_at_iso="2026-06-02T00:00:00",
    )
    assert payload["dashboard"] == "D"
    assert payload["workspace"] == "W"
    assert payload["url"] == "https://app.example/d/1"
    assert len(payload["cards"]) == 2
    assert payload["cards"][0]["title"] == "Q0"


# ── fan_out_webhooks — happy path ───────────────────────────────


def test_fan_out_returns_one_outcome_per_url() -> None:
    captured: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append((str(req.url), json.loads(req.content)))
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcomes = fan_out_webhooks(
        urls=[
            "https://hooks.slack.com/services/T/A/X",
            "https://acme.webhook.office.com/webhookb2/abc/x/y",
            "https://example.com/generic",
        ],
        dashboard_name="D",
        workspace_name="W",
        dashboard_url="https://app.example/d/1",
        cards=_cards(2),
        generated_at_iso="t",
        client=client,
    )
    assert len(outcomes) == 3
    assert all(o.ok for o in outcomes)
    # the Slack URL got the block-kit shape, not the generic shape
    slack_body = next(
        body for url, body in captured if "slack.com" in url
    )
    assert "blocks" in slack_body
    teams_body = next(
        body for url, body in captured if "webhook.office.com" in url
    )
    assert teams_body["@type"] == "MessageCard"
    generic_body = next(
        body for url, body in captured if url.endswith("/generic")
    )
    assert "cards" in generic_body


# ── fan_out_webhooks — failure isolation ────────────────────────


def test_fan_out_isolates_per_url_failures() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "fail.example" in str(req.url):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcomes = fan_out_webhooks(
        urls=[
            "https://hooks.slack.com/services/T/A/X",
            "https://fail.example/hook",
        ],
        dashboard_name="D",
        workspace_name="W",
        dashboard_url="#",
        cards=_cards(1),
        generated_at_iso="t",
        client=client,
    )
    assert len(outcomes) == 2
    slack = next(o for o in outcomes if "slack.com" in o.url)
    bad = next(o for o in outcomes if "fail.example" in o.url)
    assert slack.ok is True
    assert bad.ok is False
    assert bad.status_code == 500
    assert "boom" in (bad.error or "")


def test_fan_out_handles_transport_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcomes = fan_out_webhooks(
        urls=["https://nope.example/hook"],
        dashboard_name="D",
        workspace_name="W",
        dashboard_url="#",
        cards=_cards(1),
        generated_at_iso="t",
        client=client,
    )
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.ok is False
    assert o.status_code is None
    assert "dns failure" in (o.error or "")


# ── fan_out_webhooks — URL hygiene ──────────────────────────────


def test_fan_out_rejects_non_http_scheme() -> None:
    outcomes = fan_out_webhooks(
        urls=["ftp://example.com/hook"],
        dashboard_name="D",
        workspace_name="W",
        dashboard_url="#",
        cards=_cards(1),
        generated_at_iso="t",
    )
    assert len(outcomes) == 1
    assert outcomes[0].ok is False
    assert "http://" in (outcomes[0].error or "")


def test_fan_out_rejects_missing_netloc() -> None:
    outcomes = fan_out_webhooks(
        urls=["/relative/path"],
        dashboard_name="D",
        workspace_name="W",
        dashboard_url="#",
        cards=_cards(1),
        generated_at_iso="t",
    )
    assert len(outcomes) == 1
    assert outcomes[0].ok is False


def test_fan_out_empty_urls_returns_empty() -> None:
    assert fan_out_webhooks(
        urls=[],
        dashboard_name="D",
        workspace_name="W",
        dashboard_url="#",
        cards=_cards(1),
        generated_at_iso="t",
    ) == []
