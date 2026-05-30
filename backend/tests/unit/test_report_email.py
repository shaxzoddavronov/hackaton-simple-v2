"""Phase 29 — scheduled report email rendering + SMTP delivery.

We test the pure-Python render + send pieces (mocking smtplib so
no live SMTP needed). The end-to-end Celery sweep is exercised
indirectly through ``_is_due``'s croniter behaviour.
"""
from __future__ import annotations

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from app.services.report_email import (
    CardRender,
    _esc,
    render_dashboard_html,
    send_email,
)


# ── _esc ─────────────────────────────────────────────────────────


def test_esc_handles_html_metacharacters() -> None:
    out = _esc('<script>alert("x")</script>')
    assert "&lt;script&gt;" in out
    assert "&quot;x&quot;" in out
    assert "<script>" not in out


def test_esc_handles_none() -> None:
    assert _esc(None) == ""


def test_esc_handles_numbers() -> None:
    assert _esc(42) == "42"


def test_esc_keeps_unicode() -> None:
    # We escape HTML metacharacters but pass through Cyrillic /
    # Uzbek / other non-ASCII text — the resulting HTML is UTF-8
    # and the mail client renders it correctly.
    assert _esc("Привет, мир!") == "Привет, мир!"
    assert _esc("Salom dunyo") == "Salom dunyo"


# ── render_dashboard_html ────────────────────────────────────────


def test_render_html_contains_dashboard_name_and_workspace() -> None:
    html = render_dashboard_html(
        dashboard_name="Daily ops",
        dashboard_description=None,
        workspace_name="acme-prod",
        dashboard_url="https://qm.example.com/d/123",
        cards=[],
        generated_at_iso="2026-05-30T08:00:00Z",
    )
    assert "Daily ops" in html
    assert "acme-prod" in html
    assert "https://qm.example.com/d/123" in html
    assert "2026-05-30T08:00:00Z" in html


def test_render_html_empty_dashboard_emits_placeholder() -> None:
    html = render_dashboard_html(
        dashboard_name="Daily ops",
        dashboard_description="ops overview",
        workspace_name="ws",
        dashboard_url="#",
        cards=[],
        generated_at_iso="now",
    )
    assert "No saved questions on this dashboard yet" in html
    # Description rendered in its own div.
    assert "ops overview" in html


def test_render_html_renders_card_headline_and_body() -> None:
    cards = [
        CardRender(
            title="Refund queue",
            prompt="How many refunds today?",
            headline="14 refunds today",
            body_md="Up 30% week-over-week.",
        )
    ]
    html = render_dashboard_html(
        dashboard_name="Daily ops",
        dashboard_description=None,
        workspace_name="ws",
        dashboard_url="#",
        cards=cards,
        generated_at_iso="now",
    )
    assert "Refund queue" in html
    assert "14 refunds today" in html
    assert "30%" in html


def test_render_html_renders_error_card() -> None:
    cards = [
        CardRender(
            title="Broken q",
            prompt="impossible",
            headline=None,
            body_md=None,
            error="connection refused",
        )
    ]
    html = render_dashboard_html(
        dashboard_name="d", dashboard_description=None,
        workspace_name="ws", dashboard_url="#",
        cards=cards, generated_at_iso="now",
    )
    assert "Broken q" in html
    assert "connection refused" in html
    assert "⚠️" in html


def test_render_html_escapes_user_supplied_strings() -> None:
    """User-supplied titles / prompts can contain HTML metachars —
    they must be escaped so the email isn't injectable."""
    cards = [
        CardRender(
            title="<script>X</script>",
            prompt='alert("nope")',
            headline="ok",
            body_md="<b>not bold</b>",
        )
    ]
    html = render_dashboard_html(
        dashboard_name="d", dashboard_description=None,
        workspace_name="ws", dashboard_url="#",
        cards=cards, generated_at_iso="now",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;not bold&lt;/b&gt;" in html


def test_render_html_multilingual_passthrough() -> None:
    cards = [
        CardRender(
            title="Возвраты",
            prompt="Сколько возвратов?",
            headline="14 ta qaytarish",
            body_md="O'tgan haftaga nisbatan 30% ko'p.",
        )
    ]
    html = render_dashboard_html(
        dashboard_name="Kunlik", dashboard_description=None,
        workspace_name="ws", dashboard_url="#",
        cards=cards, generated_at_iso="now",
    )
    assert "Возвраты" in html
    assert "qaytarish" in html
    assert "O'tgan haftaga" in html


# ── send_email ────────────────────────────────────────────────────


def test_send_email_raises_when_host_unset(monkeypatch) -> None:
    from app.services import report_email

    monkeypatch.setattr(report_email.settings, "SMTP_HOST", "")
    with pytest.raises(RuntimeError, match="SMTP_HOST is empty"):
        send_email(to_addrs=["a@b"], subject="x", html_body="x")


def test_send_email_requires_recipients(monkeypatch) -> None:
    from app.services import report_email

    monkeypatch.setattr(report_email.settings, "SMTP_HOST", "smtp.local")
    with pytest.raises(ValueError, match="to_addrs"):
        send_email(to_addrs=[], subject="x", html_body="x")


def test_send_email_calls_smtp_with_starttls(monkeypatch) -> None:
    """Configured for TLS → starttls + login + send_message."""
    from app.services import report_email

    monkeypatch.setattr(report_email.settings, "SMTP_HOST", "smtp.local")
    monkeypatch.setattr(report_email.settings, "SMTP_PORT", 587)
    monkeypatch.setattr(report_email.settings, "SMTP_USER", "u")
    monkeypatch.setattr(report_email.settings, "SMTP_PASSWORD", "p")
    monkeypatch.setattr(report_email.settings, "SMTP_FROM", "from@x")
    monkeypatch.setattr(report_email.settings, "SMTP_TLS", True)

    fake_srv = MagicMock()
    fake_smtp = MagicMock()
    fake_smtp.return_value.__enter__.return_value = fake_srv
    monkeypatch.setattr(smtplib, "SMTP", fake_smtp)

    send_email(
        to_addrs=["alice@x"], subject="hi", html_body="<p>hello</p>"
    )
    fake_srv.starttls.assert_called_once()
    fake_srv.login.assert_called_once_with("u", "p")
    fake_srv.send_message.assert_called_once()


def test_send_email_skips_starttls_when_tls_false(monkeypatch) -> None:
    from app.services import report_email

    monkeypatch.setattr(report_email.settings, "SMTP_HOST", "smtp.local")
    monkeypatch.setattr(report_email.settings, "SMTP_USER", "")
    monkeypatch.setattr(report_email.settings, "SMTP_FROM", "from@x")
    monkeypatch.setattr(report_email.settings, "SMTP_TLS", False)

    fake_srv = MagicMock()
    fake_smtp = MagicMock()
    fake_smtp.return_value.__enter__.return_value = fake_srv
    monkeypatch.setattr(smtplib, "SMTP", fake_smtp)

    send_email(to_addrs=["a@b"], subject="x", html_body="x")
    fake_srv.starttls.assert_not_called()
    fake_srv.login.assert_not_called()
    fake_srv.send_message.assert_called_once()


def test_send_email_wraps_smtp_failure(monkeypatch) -> None:
    """SMTPException becomes a RuntimeError so the schedule's
    last_error can capture the reason verbatim."""
    from app.services import report_email

    monkeypatch.setattr(report_email.settings, "SMTP_HOST", "smtp.local")
    monkeypatch.setattr(report_email.settings, "SMTP_FROM", "from@x")
    monkeypatch.setattr(report_email.settings, "SMTP_TLS", False)
    monkeypatch.setattr(report_email.settings, "SMTP_USER", "")

    fake_srv = MagicMock()
    fake_srv.send_message.side_effect = smtplib.SMTPRecipientsRefused(
        {"a@b": (550, b"User unknown")}
    )
    fake_smtp = MagicMock()
    fake_smtp.return_value.__enter__.return_value = fake_srv
    monkeypatch.setattr(smtplib, "SMTP", fake_smtp)

    with pytest.raises(RuntimeError, match="SMTP delivery failed"):
        send_email(to_addrs=["a@b"], subject="x", html_body="x")


# ── _is_due (cron evaluator) ─────────────────────────────────────


def test_is_due_with_no_last_fire(monkeypatch) -> None:
    """Brand-new schedule → due if the cron has a firing in the last
    minute. We use ``* * * * *`` (every minute) so any current time
    counts as due."""
    from app.workers.report_task import _is_due
    from datetime import datetime, timezone

    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
    assert _is_due("* * * * *", None, now) is True


def test_is_due_skips_when_last_fire_is_recent() -> None:
    """A schedule that fired 30 seconds ago and runs hourly is NOT
    due yet."""
    from app.workers.report_task import _is_due
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 5, 30, 12, 0, 30, tzinfo=timezone.utc)
    last = now - timedelta(seconds=30)
    # Hourly at minute=0.
    assert _is_due("0 * * * *", last, now) is False


def test_is_due_fires_on_minute_boundary() -> None:
    """Hourly schedule (minute=0) fires exactly at 12:00 → due
    at 12:00 if not yet fired this hour."""
    from app.workers.report_task import _is_due
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 5, 30, 12, 0, 5, tzinfo=timezone.utc)
    last = now - timedelta(hours=1, minutes=30)
    assert _is_due("0 * * * *", last, now) is True


def test_is_due_rejects_bad_cron() -> None:
    from app.workers.report_task import _is_due
    from datetime import datetime, timezone

    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
    assert _is_due("not a cron", None, now) is False
