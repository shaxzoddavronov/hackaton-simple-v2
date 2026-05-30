"""Phase 24 — real-time file watcher for folder DocSources.

We test the pure-Python pieces (debouncer, watchdog-availability
gate, supervisor's start/stop idempotency) without actually
triggering a kernel inotify event. The end-to-end "file change →
Celery enqueue" path is covered indirectly through the unit test
on `_DebouncedHandler` which is the only adapter we own.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from app.services import file_watcher
from app.services.file_watcher import (
    DEBOUNCE_S,
    WatcherSupervisor,
    _DebouncedHandler,
    _watchdog_available,
)


# ── _watchdog_available ──────────────────────────────────────────


def test_watchdog_available_reflects_real_install() -> None:
    """Whether ``watchdog`` is importable depends on the dev env. We
    just verify the helper returns a bool and doesn't crash."""
    out = _watchdog_available()
    assert isinstance(out, bool)


# ── _DebouncedHandler ────────────────────────────────────────────


def test_debouncer_fires_once_per_burst(monkeypatch) -> None:
    """A torrent of file events within ``DEBOUNCE_S`` collapses
    into one enqueue."""
    # Tighten the debounce window so the test doesn't wait 5s.
    monkeypatch.setattr(file_watcher, "DEBOUNCE_S", 0.1)
    enqueues: list[str] = []
    h = _DebouncedHandler("src-1", lambda sid: enqueues.append(sid))

    class _E:
        is_directory = False
        event_type = "modified"

    for _ in range(50):
        h.on_any_event(_E())

    # Wait for the timer to fire (debounce window + a small buffer).
    time.sleep(0.25)
    assert enqueues == ["src-1"]


def test_debouncer_ignores_directories() -> None:
    """Directory-level events would otherwise add noise for
    recursive watches."""
    enqueues: list[str] = []
    h = _DebouncedHandler("src-2", lambda sid: enqueues.append(sid))

    class _DirEvent:
        is_directory = True
        event_type = "created"

    h.on_any_event(_DirEvent())
    time.sleep(0.2)
    assert enqueues == []


def test_debouncer_ignores_irrelevant_event_types() -> None:
    enqueues: list[str] = []
    h = _DebouncedHandler("src-3", lambda sid: enqueues.append(sid))

    class _E:
        is_directory = False
        event_type = "opened"  # not in our set

    h.on_any_event(_E())
    time.sleep(0.2)
    assert enqueues == []


def test_debouncer_cancel_prevents_fire(monkeypatch) -> None:
    monkeypatch.setattr(file_watcher, "DEBOUNCE_S", 0.5)
    enqueues: list[str] = []
    h = _DebouncedHandler("src-4", lambda sid: enqueues.append(sid))

    class _E:
        is_directory = False
        event_type = "modified"

    h.on_any_event(_E())
    h.cancel()
    time.sleep(0.6)
    assert enqueues == []


def test_debouncer_enqueue_exception_swallowed(monkeypatch) -> None:
    """A failing enqueue mustn't crash the watcher thread."""
    monkeypatch.setattr(file_watcher, "DEBOUNCE_S", 0.05)

    def boom(_sid: str) -> None:
        raise RuntimeError("celery broker down")

    h = _DebouncedHandler("src-5", boom)

    class _E:
        is_directory = False
        event_type = "created"

    # No-raise — handler logs and continues.
    h.on_any_event(_E())
    time.sleep(0.15)
    # Re-trigger should still work after the failure.
    h.on_any_event(_E())
    time.sleep(0.15)


# ── WatcherSupervisor lifecycle ──────────────────────────────────


def test_ensure_running_noop_when_watchdog_missing(monkeypatch) -> None:
    """On hosts without watchdog the supervisor stays inert; daily
    Celery beat handles backstop crawl."""
    monkeypatch.setattr(
        file_watcher, "_watchdog_available", lambda: False
    )
    sup = WatcherSupervisor()
    sup.ensure_running()
    assert sup._observer is None  # type: ignore[attr-defined]
    sup.stop()  # must not raise


def test_ensure_running_starts_observer_when_available(monkeypatch) -> None:
    monkeypatch.setattr(
        file_watcher, "_watchdog_available", lambda: True
    )
    fake_obs = MagicMock()
    with patch(
        "watchdog.observers.Observer", return_value=fake_obs, create=True
    ):
        sup = WatcherSupervisor()
        sup.ensure_running()
        fake_obs.start.assert_called_once()
        # Idempotent — second call must not re-start.
        sup.ensure_running()
        fake_obs.start.assert_called_once()
        sup.stop()
        fake_obs.stop.assert_called_once()


@pytest.mark.asyncio
async def test_reload_returns_zero_when_watcher_inert(
    monkeypatch,
) -> None:
    """If watchdog can't be loaded, reload is a benign no-op."""
    monkeypatch.setattr(
        file_watcher, "_watchdog_available", lambda: False
    )
    sup = WatcherSupervisor()
    report = await sup.reload()
    assert report == {"added": 0, "removed": 0, "watched": 0}


@pytest.mark.asyncio
async def test_reload_arms_new_folder_sources(
    monkeypatch, tmp_path
) -> None:
    """Single-cycle reload arms the configured paths and reports
    additions. Uses a fake Observer so we don't actually hit the
    kernel."""
    monkeypatch.setattr(
        file_watcher, "_watchdog_available", lambda: True
    )

    # Two folder sources, one with a missing path (should be skipped).
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    sources = [
        {"id": "s1", "path": str(good_dir), "recursive": True},
        {"id": "s2", "path": str(tmp_path / "missing"), "recursive": False},
    ]

    async def fake_load() -> list[dict]:
        return sources

    monkeypatch.setattr(
        file_watcher, "_load_active_folder_sources", fake_load
    )

    fake_obs = MagicMock()
    fake_watch_token = object()
    fake_obs.schedule.return_value = fake_watch_token
    with patch(
        "watchdog.observers.Observer", return_value=fake_obs, create=True
    ):
        sup = WatcherSupervisor()
        report = await sup.reload()
        assert report["added"] == 1   # only the good dir
        assert report["watched"] == 1
        assert "s1" in sup._watches  # type: ignore[attr-defined]
        assert "s2" not in sup._watches  # type: ignore[attr-defined]
        # Second reload with the same source list should be a no-op.
        report2 = await sup.reload()
        assert report2["added"] == 0
        assert report2["removed"] == 0
        sup.stop()


@pytest.mark.asyncio
async def test_reload_removes_dropped_sources(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        file_watcher, "_watchdog_available", lambda: True
    )
    d = tmp_path / "x"
    d.mkdir()

    # First reload: source present.
    state = {"sources": [{"id": "s1", "path": str(d), "recursive": False}]}

    async def fake_load() -> list[dict]:
        return list(state["sources"])

    monkeypatch.setattr(
        file_watcher, "_load_active_folder_sources", fake_load
    )

    fake_obs = MagicMock()
    fake_obs.schedule.return_value = object()
    with patch(
        "watchdog.observers.Observer", return_value=fake_obs, create=True
    ):
        sup = WatcherSupervisor()
        await sup.reload()
        assert "s1" in sup._watches  # type: ignore[attr-defined]

        # Second reload: source removed from the DB.
        state["sources"] = []
        report = await sup.reload()
        assert report["removed"] == 1
        assert "s1" not in sup._watches  # type: ignore[attr-defined]
        fake_obs.unschedule.assert_called_once()
        sup.stop()
