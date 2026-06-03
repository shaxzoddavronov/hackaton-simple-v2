"""Celery task that fires due ReportSchedule rows.

Phase 29. Once a minute the Beat scheduler invokes
``run_due_report_schedules``. The task scans the report_schedules
table, picks rows whose ``cron`` evaluates as "due since
``last_fired_at``", runs each saved question on the linked
dashboard through the existing agent graph, renders an HTML digest
via :mod:`services.report_email`, and ships it via SMTP.

Failure isolation: a broken schedule (bad cron, dead SMTP, missing
dashboard) writes ``last_status='error'`` + ``last_error`` and
skips this firing — never poisons the loop. Successful sends bump
``last_fired_at`` to the moment the email was dispatched.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.report_task.run_due_report_schedules",
    bind=True,
)
def run_due_report_schedules(self) -> dict[str, int]:
    """Beat-driven sweep. Returns ``{checked, fired, failed}`` for
    observability."""
    return asyncio.run(_sweep_async())


async def _sweep_async() -> dict[str, int]:
    from sqlalchemy import select

    from app.db.models import (
        Dashboard, ReportSchedule, SavedQuestion, User, Workspace,
    )
    from app.services.report_email import (
        CardRender, render_dashboard_html, send_email,
    )

    eng = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    checked = fired = failed = 0
    now = datetime.now(timezone.utc)
    try:
        async with Session() as session:
            rows = (
                await session.execute(
                    select(ReportSchedule).where(
                        ReportSchedule.enabled.is_(True)
                    )
                )
            ).scalars().all()
            checked = len(rows)

            for sched in rows:
                if not _is_due(sched.cron, sched.last_fired_at, now):
                    continue

                try:
                    await _fire_one(session, sched, now)
                    sched.last_fired_at = now
                    sched.last_status = "ok"
                    sched.last_error = None
                    fired += 1
                except Exception as e:
                    log.exception(
                        "report_task: schedule %s failed", sched.id
                    )
                    sched.last_status = "error"
                    sched.last_error = str(e)[:1000]
                    failed += 1
                # Commit per schedule so a later failure doesn't
                # roll back the earlier successful sends.
                await session.commit()
    finally:
        await eng.dispose()

    log.info(
        "report_task: sweep checked=%d fired=%d failed=%d",
        checked, fired, failed,
    )
    return {"checked": checked, "fired": fired, "failed": failed}


def _is_due(
    cron: str,
    last_fired_at: datetime | None,
    now: datetime,
) -> bool:
    """Cron evaluator — uses croniter to find the next firing after
    the last actual fire (or 1 minute ago if never fired). If that
    next firing is in the past, the schedule is due.

    croniter is in the project's transitive dependencies (Celery
    itself ships it). Local import keeps the failure mode clean.
    """
    try:
        from croniter import croniter
    except ImportError:
        log.warning(
            "report_task: croniter not available — cannot evaluate "
            "schedule. Install with: pip install croniter"
        )
        return False

    base = last_fired_at or (now - _ONE_MINUTE)
    try:
        it = croniter(cron, base)
    except Exception as e:
        log.warning(
            "report_task: invalid cron %r: %s", cron, e
        )
        return False
    nxt = it.get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=timezone.utc)
    return nxt <= now


async def _fire_one(session, sched, now: datetime) -> None:
    """Render + send one schedule's email. Raises on any failure;
    caller catches and stamps ``last_error``."""
    from sqlalchemy import select

    from app.db.models import (
        Dashboard, SavedQuestion, User, Workspace,
    )
    from app.services.report_email import (
        CardRender, render_dashboard_html, send_email,
    )

    dash = await session.get(Dashboard, sched.dashboard_id)
    if dash is None:
        raise RuntimeError(
            f"dashboard {sched.dashboard_id} no longer exists"
        )
    ws = await session.get(Workspace, sched.workspace_id)
    owner = await session.get(User, sched.owner_id)

    qs = (
        await session.execute(
            select(SavedQuestion)
            .where(SavedQuestion.dashboard_id == sched.dashboard_id)
            .order_by(
                SavedQuestion.position.nulls_last(),
                SavedQuestion.created_at.asc(),
            )
        )
    ).scalars().all()

    cards: list[CardRender] = []
    for q in qs:
        try:
            answer = await _run_one_question(q)
            cards.append(
                CardRender(
                    title=q.title,
                    prompt=q.prompt,
                    headline=answer.get("headline"),
                    body_md=answer.get("body_md"),
                )
            )
        except Exception as e:
            cards.append(
                CardRender(
                    title=q.title,
                    prompt=q.prompt,
                    headline=None,
                    body_md=None,
                    error=str(e)[:300],
                )
            )

    base_url = (settings.PUBLIC_BASE_URL or "").rstrip("/")
    dash_url = (
        f"{base_url}/workspaces/{ws.id}/dashboards/{dash.id}"
        if base_url and ws is not None
        else "#"
    )
    html = render_dashboard_html(
        dashboard_name=dash.name,
        dashboard_description=dash.description,
        workspace_name=ws.name if ws is not None else "(unknown)",
        dashboard_url=dash_url,
        cards=cards,
        generated_at_iso=now.replace(microsecond=0).isoformat(),
    )

    to_addrs: list[str] = []
    if owner is not None and owner.email:
        to_addrs.append(owner.email)
    for extra in (sched.recipients or "").split(","):
        extra = extra.strip()
        if extra and "@" in extra and extra not in to_addrs:
            to_addrs.append(extra)

    # Phase 33 — webhook fan-out. A schedule with zero email
    # recipients is now valid as long as at least one webhook URL is
    # configured (Slack-only digests are a common request).
    from app.services.report_webhooks import (
        fan_out_webhooks,
        parse_webhook_urls,
    )

    webhook_urls = parse_webhook_urls(
        getattr(sched, "webhook_urls", "") or ""
    )

    if not to_addrs and not webhook_urls:
        raise RuntimeError(
            "no destinations — owner has no email, recipients is empty, "
            "and no webhook_urls are configured"
        )

    subject = f"[QueryMind] {dash.name} — daily digest"
    if to_addrs:
        send_email(to_addrs=to_addrs, subject=subject, html_body=html)

    if webhook_urls:
        outcomes = fan_out_webhooks(
            urls=webhook_urls,
            dashboard_name=dash.name,
            workspace_name=ws.name if ws is not None else "(unknown)",
            dashboard_url=dash_url,
            cards=cards,
            generated_at_iso=now.replace(microsecond=0).isoformat(),
        )
        bad = [o for o in outcomes if not o.ok]
        if bad:
            # Surface webhook failures via last_error but DON'T raise
            # — email may have succeeded and we want last_status='ok'
            # for the parts that worked. The caller logs+stamps
            # last_error from the RuntimeError we raise here ONLY if
            # EVERY destination failed.
            if not to_addrs and len(bad) == len(outcomes):
                detail = "; ".join(
                    f"{o.url}: {o.error}" for o in bad[:3]
                )
                raise RuntimeError(
                    f"all webhook deliveries failed: {detail}"
                )
            # Partial failure — log but treat the firing as a success.
            log.warning(
                "report_task: %d/%d webhooks failed for schedule %s",
                len(bad), len(outcomes), sched.id,
            )


async def _run_one_question(q) -> dict:
    """Run a SavedQuestion through the agent graph and return the
    AnswerDraft as a dict.

    Doesn't use the chat SSE — we don't need streaming for a batch
    email. Goes straight at the LangGraph entry point.
    """
    from app.agents.graph import build_graph
    from app.agents.state import GraphState

    graph = build_graph()
    init: GraphState = {
        "user_message": q.prompt,
        "active_workspace_id": q.workspace_id,
        "active_connection_id": q.connection_id,
        "conversation_history": [],
    }  # type: ignore[typeddict-item]
    final = await graph.ainvoke(init)
    answer = final.get("answer")
    if answer is None:
        return {
            "headline": "(no answer)",
            "body_md": final.get("error_message") or "Agent returned nothing.",
        }
    return {
        "headline": getattr(answer, "headline", None) or "",
        "body_md": getattr(answer, "body_md", None) or "",
    }


from datetime import timedelta

_ONE_MINUTE = timedelta(minutes=1)


__all__ = ["run_due_report_schedules"]
