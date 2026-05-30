"""SMTP delivery + HTML rendering for scheduled dashboard reports.

Phase 29 — turns a Dashboard into an HTML digest and ships it via
SMTP. Two responsibilities split into pure functions so the unit
tests don't need a live mailserver:

  * :func:`render_dashboard_html` — build the HTML body from a
    dashboard's saved questions + their freshly-run AnswerDrafts.
  * :func:`send_email` — wrap stdlib ``email`` + ``smtplib`` so the
    Celery task is just ``render → send``.

SMTP config from :class:`app.config.Settings` — host/port/user/pass/
from/tls. If ``SMTP_HOST`` is empty, ``send_email`` raises a clear
``RuntimeError`` so the schedule's ``last_error`` reports the
misconfig instead of silently dropping mail.
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CardRender:
    """One question + its current rendered answer for the email."""
    title: str
    prompt: str
    headline: str | None
    body_md: str | None
    error: str | None = None


def render_dashboard_html(
    *,
    dashboard_name: str,
    dashboard_description: str | None,
    workspace_name: str,
    dashboard_url: str,
    cards: list[CardRender],
    generated_at_iso: str,
) -> str:
    """Build the HTML body for a scheduled dashboard digest.

    Kept inline (no jinja) so the template lives next to the code
    that drives it. Inline CSS for mail-client compatibility — most
    clients strip ``<style>`` blocks but inline ``style=""`` survives.
    """
    rows = []
    for c in cards:
        if c.error:
            block = (
                f'<tr><td style="padding:14px 16px;background:#3a1818;'
                f'border-radius:8px;color:#ffb4b0;">'
                f'<div style="font-size:14px;font-weight:600;">'
                f"{_esc(c.title)}</div>"
                f'<div style="font-size:12px;opacity:0.7;margin:4px 0 8px;">'
                f"{_esc(c.prompt)}</div>"
                f'<div style="font-size:13px;">⚠️ {_esc(c.error)}</div>'
                "</td></tr>"
            )
        else:
            headline = _esc(c.headline or "(no headline)")
            body = _esc(c.body_md or "")
            block = (
                f'<tr><td style="padding:14px 16px;background:#16181c;'
                f'border-radius:8px;color:#dfe2e6;">'
                f'<div style="font-size:14px;font-weight:600;color:#fff;">'
                f"{_esc(c.title)}</div>"
                f'<div style="font-size:12px;opacity:0.6;margin:4px 0 10px;">'
                f"{_esc(c.prompt)}</div>"
                f'<div style="font-size:15px;font-weight:500;color:#a3e0d6;'
                f'margin-bottom:6px;">{headline}</div>'
                f'<div style="font-size:13px;line-height:1.5;">{body}</div>'
                "</td></tr>"
            )
        rows.append(block)
        # spacer
        rows.append('<tr><td style="height:8px;"></td></tr>')

    body = "\n".join(rows) if rows else (
        '<tr><td style="color:#888;padding:18px;">'
        "No saved questions on this dashboard yet.</td></tr>"
    )
    desc = (
        f'<div style="font-size:13px;color:#aaa;margin-top:4px;">'
        f"{_esc(dashboard_description)}</div>"
        if dashboard_description
        else ""
    )
    return f"""<!doctype html>
<html><body style="background:#0e1014;font-family:Inter,Arial,
sans-serif;margin:0;padding:24px;color:#dfe2e6;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="max-width:640px;margin:0 auto;">
  <tr><td>
    <div style="font-size:11px;letter-spacing:.08em;text-transform:
                uppercase;color:#8aa;">{_esc(workspace_name)} · QueryMind</div>
    <h1 style="font-size:24px;margin:6px 0 0;font-weight:700;color:#fff;">
      {_esc(dashboard_name)}
    </h1>
    {desc}
    <div style="font-size:11px;color:#666;margin-top:6px;">
      Generated {_esc(generated_at_iso)}
    </div>
  </td></tr>
  <tr><td style="height:18px;"></td></tr>
  {body}
  <tr><td style="height:18px;"></td></tr>
  <tr><td style="font-size:12px;color:#789;">
    <a href="{_esc(dashboard_url)}" style="color:#7ab8ff;
       text-decoration:none;">Open live dashboard ↗</a>
  </td></tr>
</table>
</body></html>"""


def _esc(s: Any) -> str:
    """Minimal HTML-escape so user-supplied titles / prompts / body
    text don't break the surrounding markup."""
    out = str(s or "")
    return (
        out.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def send_email(
    *,
    to_addrs: list[str],
    subject: str,
    html_body: str,
    text_body: str | None = None,
) -> None:
    """Send an HTML email via the configured SMTP server.

    Picks STARTTLS when ``settings.SMTP_TLS`` is True, plain SMTP
    otherwise. ``to_addrs`` must be non-empty; ``settings.SMTP_HOST``
    must be configured — otherwise raises ``RuntimeError`` with the
    config var name so the schedule's ``last_error`` is actionable.
    """
    host = settings.SMTP_HOST
    if not host:
        raise RuntimeError(
            "SMTP_HOST is empty — scheduled reports cannot be delivered. "
            "Set SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / "
            "SMTP_FROM in .env."
        )
    if not to_addrs:
        raise ValueError("send_email: to_addrs is empty")
    sender = settings.SMTP_FROM or settings.SMTP_USER or "querymind@localhost"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(text_body or "Open the HTML version of this email.")
    msg.add_alternative(html_body, subtype="html")

    port = int(settings.SMTP_PORT or 587)
    try:
        with smtplib.SMTP(host, port, timeout=20) as srv:
            if settings.SMTP_TLS:
                srv.starttls()
            if settings.SMTP_USER:
                srv.login(settings.SMTP_USER, settings.SMTP_PASSWORD or "")
            srv.send_message(msg)
        log.info(
            "report_email: delivered subject=%r recipients=%d",
            subject, len(to_addrs),
        )
    except smtplib.SMTPException as e:
        # Surface the SMTP code/reason — the schedule's last_error
        # captures this verbatim so operators can debug without
        # tailing logs.
        raise RuntimeError(f"SMTP delivery failed: {e}") from e


__all__ = [
    "CardRender",
    "render_dashboard_html",
    "send_email",
]
