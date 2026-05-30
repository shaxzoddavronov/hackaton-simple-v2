"""Real-time file-system watcher for folder DocSources.

Phase 24. Phase 14 indexed folder sources daily via Celery beat.
That's enough for archive-style use cases but feels slow when a
user drops a new policy PDF into the watched directory and expects
the chat to know about it within seconds. This module wires the
``watchdog`` package — cross-platform inotify / FSEvents /
ReadDirectoryChangesW — into the DocSource lifecycle so file
create / modify events trigger an incremental harvest.

Design:

  * One ``WatcherSupervisor`` per process. Started by the
    backend's startup hook and stopped on shutdown.
  * The supervisor reads all enabled folder DocSources from the
    DB and arms one observer per (workspace_id, source_id, path).
  * File events are debounced: rapid bursts of writes (a 100 MB
    file copy fires a thousand modify events) collapse into one
    enqueue per source per ``DEBOUNCE_S`` (default 5 s) window.
  * Each enqueue calls the same ``run_harvest_doc_source`` Celery
    task daily-recrawl uses. The harvester is wipe-and-reload by
    design, so a duplicate run is safe — at worst we waste a few
    seconds re-embedding chunks whose content_hash matches.
  * Add / remove DocSources at runtime: ``reload()`` re-syncs the
    observer set against the current DB state. Called on doc-
    source create / delete / config-change. ``ensure_running`` is
    idempotent so calls from multiple workers are safe.

What we do NOT watch yet:
  * SMB / cloud drive sources — those need polling or
    vendor-specific change-feed APIs. Out of scope for v1.
  * IMAP / Slack / Telegram — message archives don't have a
    push semantics we can hook into without per-vendor wiring.

Failure modes:
  * Folder doesn't exist or isn't readable → log + skip; the
    daily cron will pick it up later when the operator fixes
    the path.
  * watchdog backend fails (ulimits on Linux, missing kernel
    features in WSL1, etc.) → log once, fall back to daily-cron-
    only behaviour.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)


DEBOUNCE_S = 5.0  # collapse bursts of events into one harvest enqueue


def _watchdog_available() -> bool:
    """Return True iff the watchdog package is importable. Tests +
    CPU-only dev boxes skip the live-watch path when False."""
    try:
        import watchdog.observers  # noqa: F401

        return True
    except ImportError:
        return False


class _DebouncedHandler:
    """Adapt watchdog events to a single per-source enqueue.

    watchdog calls ``on_created`` / ``on_modified`` synchronously
    from a background thread. We translate each event into a
    "trigger" intent, debounced over ``DEBOUNCE_S`` so a torrent
    of writes doesn't enqueue hundreds of harvest jobs.
    """

    def __init__(self, source_id: str, enqueue_fn) -> None:
        self.source_id = source_id
        self._enqueue_fn = enqueue_fn
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    # watchdog FileSystemEventHandler interface — we keep create +
    # modify, ignore everything else (move/delete will be picked up
    # at the next crawl).
    def on_any_event(self, event) -> None:
        if event.is_directory:
            return
        et = getattr(event, "event_type", "")
        if et not in ("created", "modified", "moved"):
            return
        self._trigger()

    def _trigger(self) -> None:
        with self._lock:
            if self._timer is not None and self._timer.is_alive():
                # Already armed; let the existing timer fire.
                return
            self._timer = threading.Timer(DEBOUNCE_S, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        try:
            self._enqueue_fn(self.source_id)
        except Exception as e:
            log.warning(
                "file_watcher: enqueue failed for source=%s: %s",
                self.source_id, e,
            )

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


class WatcherSupervisor:
    """Singleton-per-process watcher controller.

    Reads enabled folder DocSources from the DB, starts a watchdog
    observer per (path, recursive) tuple, and dispatches debounced
    enqueues to ``run_harvest_doc_source.delay(source_id)``.

    Idempotent: calling ``ensure_running`` repeatedly is safe;
    ``reload`` reconciles the current observer set against the
    current DB state (sources added → watched; sources removed →
    unwatched). Shutdown via ``stop()``.
    """

    def __init__(self) -> None:
        self._observer: Any | None = None
        # Map source_id → (handler, watchdog watch token, root path)
        self._watches: dict[str, tuple[Any, Any, Path]] = {}
        self._lock = threading.Lock()

    # ── lifecycle ──────────────────────────────────────────────

    def ensure_running(self) -> None:
        with self._lock:
            if self._observer is not None:
                return
            if not _watchdog_available():
                log.info(
                    "file_watcher: watchdog package not installed — "
                    "real-time file watching disabled. Falls back to "
                    "daily Celery-beat recrawl."
                )
                return
            from watchdog.observers import Observer

            self._observer = Observer()
            self._observer.daemon = True
            self._observer.start()

    def stop(self) -> None:
        with self._lock:
            for src_id, (handler, _watch, _path) in self._watches.items():
                try:
                    handler.cancel()
                except Exception:
                    pass
            self._watches.clear()
            if self._observer is not None:
                try:
                    self._observer.stop()
                    self._observer.join(timeout=2.0)
                except Exception:
                    pass
                self._observer = None

    # ── reconcile against DB ───────────────────────────────────

    async def reload(self) -> dict[str, int]:
        """Sync watched paths against the current DB state.

        Returns a small report ``{"added": N, "removed": M,
        "watched": K}`` for observability. Safe to call from any
        thread; the inner DB read uses its own async session.
        """
        self.ensure_running()
        if self._observer is None:
            return {"added": 0, "removed": 0, "watched": 0}

        wanted = await _load_active_folder_sources()
        wanted_ids = {row["id"] for row in wanted}

        from app.workers.harvest_task import run_harvest_doc_source

        def _enqueue(source_id: str) -> None:
            log.info("file_watcher: enqueue harvest source=%s", source_id)
            run_harvest_doc_source.delay(source_id)

        added = 0
        removed = 0
        with self._lock:
            # Remove dropped sources first.
            for src_id in list(self._watches.keys()):
                if src_id not in wanted_ids:
                    handler, watch, _path = self._watches.pop(src_id)
                    handler.cancel()
                    try:
                        self._observer.unschedule(watch)
                    except Exception:
                        pass
                    removed += 1

            # Add newly-active sources.
            for row in wanted:
                src_id = row["id"]
                if src_id in self._watches:
                    continue
                root = Path(row["path"]).resolve()
                if not root.is_dir():
                    log.warning(
                        "file_watcher: source %s path %s not a directory "
                        "— skipping",
                        src_id, root,
                    )
                    continue
                handler = _DebouncedHandler(src_id, _enqueue)
                from watchdog.events import FileSystemEventHandler

                # Wrap _DebouncedHandler so watchdog finds the
                # standard interface. We delegate every event into
                # ``on_any_event`` on our debouncer.
                wrapper = FileSystemEventHandler()
                wrapper.on_any_event = handler.on_any_event  # type: ignore[assignment]
                try:
                    watch = self._observer.schedule(
                        wrapper,
                        str(root),
                        recursive=bool(row["recursive"]),
                    )
                except Exception as e:
                    log.warning(
                        "file_watcher: schedule failed for %s: %s",
                        root, e,
                    )
                    continue
                self._watches[src_id] = (handler, watch, root)
                added += 1

        return {
            "added": added,
            "removed": removed,
            "watched": len(self._watches),
        }


_supervisor = WatcherSupervisor()


def get_supervisor() -> WatcherSupervisor:
    return _supervisor


# ── helpers ────────────────────────────────────────────────────


async def _load_active_folder_sources() -> list[dict]:
    """Return the folder-kind DocSources we should watch.

    A DocSource is "watched" when:
      * source_kind == 'folder'
      * status != 'error'
      * config has a non-empty 'path'
      * config.realtime_watch (optional, default True) is truthy

    We deliberately include sources currently in status='harvesting'
    so a fresh-source initial-crawl gets real-time updates after
    its first pass completes.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.models import DocSource

    eng = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    out: list[dict] = []
    try:
        async with Session() as session:
            rows = (
                await session.execute(
                    select(DocSource).where(
                        DocSource.source_kind == "folder",
                        DocSource.status != "error",
                    )
                )
            ).scalars().all()
            for s in rows:
                cfg = dict(s.config or {})
                if not isinstance(cfg.get("path"), str) or not cfg["path"]:
                    continue
                if not cfg.get("realtime_watch", True):
                    continue
                out.append(
                    {
                        "id": str(s.id),
                        "path": cfg["path"],
                        "recursive": bool(cfg.get("recursive", True)),
                    }
                )
    finally:
        await eng.dispose()
    return out
