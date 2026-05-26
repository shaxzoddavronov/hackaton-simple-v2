from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "querymind",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.workers.profile_task",
        "app.workers.index_task",
        "app.workers.diff_task",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Schema profiling can take a while on large databases. The hard ceiling
    # guards a runaway task; the soft limit gives it a chance to clean up.
    task_time_limit=600,
    task_soft_time_limit=540,
)


# --- Beat schedule ---------------------------------------------------------
# Daily structural-drift check across every workspace. If a workspace's
# schema actually changed, the diff task re-profiles + enqueues a RAG
# re-index for just that workspace.
celery_app.conf.beat_schedule = {
    "rag-daily-schema-diff": {
        "task": "app.workers.diff_task.run_daily_diff",
        "schedule": crontab(
            minute=settings.RAG_DIFF_CHECK_MINUTE_UTC,
            hour=settings.RAG_DIFF_CHECK_HOUR_UTC,
        ),
    },
    # Reindex the REST API catalog once a day too — covers the case where
    # a deploy added/removed routes without anyone manually refreshing.
    "rag-daily-api-catalog": {
        "task": "app.workers.index_task.run_index_api_catalog",
        "schedule": crontab(
            minute=settings.RAG_DIFF_CHECK_MINUTE_UTC,
            hour=settings.RAG_DIFF_CHECK_HOUR_UTC,
        ),
    },
}
