"""Webhook fan-out for scheduled reports (Phase 33).

Sister module to :mod:`app.services.report_email` — same input shape
(``list[CardRender]`` plus dashboard metadata), different transport.
Each URL receives a single POST with a body shaped for whichever
destination the URL's host matches:

  * Slack (``hooks.slack.com``) — block-kit payload with
    ``blocks: [{ type: "header" }, { type: "section" }, ...]``.
  * MS Teams (``*.webhook.office.com``) — MessageCard adaptive shape.
  * Discord (``discord.com/api/webhooks``) — embeds array.
  * Anything else — a generic JSON envelope with the raw card list.

The Celery task that drives this never lets one bad URL block the
others — :func:`fan_out_webhooks` returns a per-URL outcome list so
the schedule's ``last_status`` can summarise without raising.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.services.report_email import CardRender

log = logging.getLogger(__name__)

# Per-request budget. Most managed webhook receivers respond in well
# under a second; >10s usually means the receiver is overloaded or
# the URL is wrong. Failing fast keeps the Celery worker turning.
WEBHOOK_TIMEOUT_S = 10


@dataclass(slots=True)
class WebhookOutcome:
    """Per-URL delivery result. The Celery task aggregates these for
    ``ReportSchedule.last_status``."""
    url: str
    ok: bool
    status_code: int | None
    error: str | None = None


def parse_webhook_urls(raw: str | None) -> list[str]:
    """Split a TEXT field stored on ReportSchedule into clean URLs.

    Accepts newline OR comma separation; strips whitespace, blank
    lines, and the common "# comment" sentinel so admins can annotate
    URLs in the textarea without breaking the parser.
    """
    if not raw:
        return []
    chunks: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        chunks.append(s)
    return chunks


def _is_slack(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"hooks.slack.com", "slack.com"}


def _is_teams(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    # MS Teams hosts include both legacy (outlook.office.com) and the
    # new tenant subdomains under webhook.office.com.
    return host.endswith("webhook.office.com") or host.endswith(
        "office.com"
    )


def _is_discord(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host not in {"discord.com", "discordapp.com"}:
        return False
    return "/api/webhooks/" in url


def build_payload(
    *,
    url: str,
    dashboard_name: str,
    workspace_name: str,
    dashboard_url: str,
    cards: list[CardRender],
    generated_at_iso: str,
) -> dict[str, Any]:
    """Pick the payload shape based on the URL's host."""
    if _is_slack(url):
        return _slack_payload(
            dashboard_name=dashboard_name,
            workspace_name=workspace_name,
            dashboard_url=dashboard_url,
            cards=cards,
            generated_at_iso=generated_at_iso,
        )
    if _is_teams(url):
        return _teams_payload(
            dashboard_name=dashboard_name,
            workspace_name=workspace_name,
            dashboard_url=dashboard_url,
            cards=cards,
            generated_at_iso=generated_at_iso,
        )
    if _is_discord(url):
        return _discord_payload(
            dashboard_name=dashboard_name,
            workspace_name=workspace_name,
            dashboard_url=dashboard_url,
            cards=cards,
            generated_at_iso=generated_at_iso,
        )
    return _generic_payload(
        dashboard_name=dashboard_name,
        workspace_name=workspace_name,
        dashboard_url=dashboard_url,
        cards=cards,
        generated_at_iso=generated_at_iso,
    )


def _slack_payload(
    *,
    dashboard_name: str,
    workspace_name: str,
    dashboard_url: str,
    cards: list[CardRender],
    generated_at_iso: str,
) -> dict[str, Any]:
    # Slack block-kit. Header + context + one section per card.
    # Slack hard-caps blocks at 50; we trim past 45 to leave room for
    # the trailing "open dashboard" action block.
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": dashboard_name[:150],
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*{workspace_name}* · QueryMind · "
                        f"{generated_at_iso}"
                    ),
                }
            ],
        },
        {"type": "divider"},
    ]
    for c in cards[:45]:
        if c.error:
            text = f"*{c.title}*\n_{c.prompt}_\n:warning: {c.error}"
        else:
            text = (
                f"*{c.title}*\n_{c.prompt}_\n"
                f"*{c.headline or '(no headline)'}*\n"
                f"{c.body_md or ''}"
            )
        # Slack section text is capped at 3000 chars.
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text[:2900]},
            }
        )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Open live dashboard",
                    },
                    "url": dashboard_url,
                }
            ],
        }
    )
    return {
        "text": f"{dashboard_name} — scheduled report",
        "blocks": blocks,
    }


def _teams_payload(
    *,
    dashboard_name: str,
    workspace_name: str,
    dashboard_url: str,
    cards: list[CardRender],
    generated_at_iso: str,
) -> dict[str, Any]:
    # MS Teams MessageCard. Adaptive Cards would be nicer but require
    # the receiver to support them; MessageCard is the lowest common
    # denominator that every Teams webhook channel accepts.
    sections: list[dict[str, Any]] = []
    for c in cards:
        if c.error:
            text = f"⚠️ {c.error}"
        else:
            text = (
                f"**{c.headline or '(no headline)'}**\n\n"
                f"{c.body_md or ''}"
            )
        sections.append(
            {
                "activityTitle": c.title,
                "activitySubtitle": c.prompt,
                "text": text,
            }
        )
    return {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": f"{dashboard_name} — scheduled report",
        "themeColor": "0EA5E9",
        "title": dashboard_name,
        "text": (
            f"**{workspace_name}** · QueryMind · {generated_at_iso}"
        ),
        "sections": sections,
        "potentialAction": [
            {
                "@type": "OpenUri",
                "name": "Open live dashboard",
                "targets": [{"os": "default", "uri": dashboard_url}],
            }
        ],
    }


def _discord_payload(
    *,
    dashboard_name: str,
    workspace_name: str,
    dashboard_url: str,
    cards: list[CardRender],
    generated_at_iso: str,
) -> dict[str, Any]:
    # Discord caps each message at 10 embeds and each field at 1024
    # chars; trim aggressively.
    embeds: list[dict[str, Any]] = []
    for c in cards[:10]:
        if c.error:
            description = f"⚠️ {c.error}"
        else:
            description = (
                f"**{c.headline or '(no headline)'}**\n"
                f"{c.body_md or ''}"
            )
        embeds.append(
            {
                "title": c.title[:240],
                "description": description[:2000],
                "footer": {"text": c.prompt[:2000]},
            }
        )
    return {
        "content": (
            f"**{dashboard_name}** — scheduled report "
            f"({workspace_name}, {generated_at_iso})\n"
            f"{dashboard_url}"
        ),
        "embeds": embeds,
    }


def _generic_payload(
    *,
    dashboard_name: str,
    workspace_name: str,
    dashboard_url: str,
    cards: list[CardRender],
    generated_at_iso: str,
) -> dict[str, Any]:
    return {
        "dashboard": dashboard_name,
        "workspace": workspace_name,
        "url": dashboard_url,
        "generated_at": generated_at_iso,
        "cards": [
            {
                "title": c.title,
                "prompt": c.prompt,
                "headline": c.headline,
                "body_md": c.body_md,
                "error": c.error,
            }
            for c in cards
        ],
    }


def fan_out_webhooks(
    *,
    urls: list[str],
    dashboard_name: str,
    workspace_name: str,
    dashboard_url: str,
    cards: list[CardRender],
    generated_at_iso: str,
    client: httpx.Client | None = None,
) -> list[WebhookOutcome]:
    """POST the rendered report to every URL. One bad URL never
    blocks the others — every URL gets its own try/except boundary
    and we return per-URL outcomes.

    The optional ``client`` parameter exists for tests (MockTransport).
    In production we open a short-lived httpx.Client per call so
    failed-DNS lookups don't leak sockets.
    """
    if not urls:
        return []
    outcomes: list[WebhookOutcome] = []
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(WEBHOOK_TIMEOUT_S))
    try:
        for url in urls:
            outcomes.append(
                _post_one(
                    client=client,
                    url=url,
                    dashboard_name=dashboard_name,
                    workspace_name=workspace_name,
                    dashboard_url=dashboard_url,
                    cards=cards,
                    generated_at_iso=generated_at_iso,
                )
            )
    finally:
        if owns_client:
            client.close()
    return outcomes


def _post_one(
    *,
    client: httpx.Client,
    url: str,
    dashboard_name: str,
    workspace_name: str,
    dashboard_url: str,
    cards: list[CardRender],
    generated_at_iso: str,
) -> WebhookOutcome:
    # URL hygiene — reject anything that's not http(s) at the parse
    # layer so admins typos like "slack.com/.." (no scheme) don't
    # turn into surprise relative requests against the backend.
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return WebhookOutcome(
            url=url,
            ok=False,
            status_code=None,
            error="URL must start with http:// or https://",
        )
    payload = build_payload(
        url=url,
        dashboard_name=dashboard_name,
        workspace_name=workspace_name,
        dashboard_url=dashboard_url,
        cards=cards,
        generated_at_iso=generated_at_iso,
    )
    try:
        resp = client.post(
            url,
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    except httpx.HTTPError as e:
        log.warning(
            "webhook: transport error url=%s err=%s", url, e
        )
        return WebhookOutcome(
            url=url, ok=False, status_code=None, error=str(e)[:240]
        )
    ok = 200 <= resp.status_code < 300
    err = None
    if not ok:
        err = (resp.text or "")[:240] or f"HTTP {resp.status_code}"
        log.warning(
            "webhook: rejected url=%s status=%d body=%s",
            url, resp.status_code, err,
        )
    return WebhookOutcome(
        url=url,
        ok=ok,
        status_code=resp.status_code,
        error=err,
    )


__all__ = [
    "WebhookOutcome",
    "WEBHOOK_TIMEOUT_S",
    "build_payload",
    "fan_out_webhooks",
    "parse_webhook_urls",
]
