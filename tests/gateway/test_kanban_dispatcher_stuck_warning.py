"""The embedded gateway dispatcher names the hold its CLI sibling names (#111910).

This drives the real ``GatewayRunner._kanban_dispatcher_watcher`` loop with a
stubbed tick source, so the assertion is on the line the gateway actually logs —
the copy that runs in production (``kanban.dispatch_in_gateway``) and the copy
that was printing a bare zero-spawn count six times on 2026-09-25.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gateway.kanban_watchers_dispatcher import _DispatcherSettings
from hermes_cli import kanban_db_dispatch as kbd


def _settings():
    return _DispatcherSettings(
        interval=1.0,
        max_spawn=None,
        max_in_progress=6,
        failure_limit=3,
        stale_timeout_seconds=0,
        reconcile_orphans=True,
        default_assignee=None,
        max_in_progress_per_profile=6,
    )


class _StubDispatcher:
    """One board, one tick result, ready queue always non-empty."""

    def __init__(self, result):
        self._result = result

    def tick_once(self):
        return [("ops", self._result)]

    def ready_nonempty(self):
        return True


def _warning_lines(result, *, ticks, seconds_per_tick):
    """Run the real watcher loop and return the "dispatcher stuck" lines."""
    import gateway.kanban_watchers as kw
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_dispatcher_lock_handle = None
    runner._kanban_dispatcher_boot = lambda: (dict, MagicMock(), {})
    runner._release_kanban_dispatcher_lock = MagicMock()

    clock = {"now": 1_700_000_000}
    state = {"ticks": 0}
    lines: list[str] = []

    async def fake_sleep(_delay):
        return None

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def tick_and_maybe_stop(_interval):
        state["ticks"] += 1
        clock["now"] += seconds_per_tick
        if state["ticks"] >= ticks:
            runner._running = False

    class _Capture(logging.Handler):
        def emit(self, record):
            if "dispatcher stuck" in record.getMessage():
                lines.append(record.getMessage())

    handler = _Capture()
    kw.logger.addHandler(handler)
    try:
        with patch.object(kw, "_resolve_dispatcher_settings", return_value=_settings()), \
                patch.object(kw, "_KanbanDispatcher", lambda *a, **k: _StubDispatcher(result)), \
                patch.object(kw, "_to_thread_process_service", side_effect=fake_to_thread), \
                patch.object(kw, "_resolve_auto_decompose_settings", return_value=(False, 0)), \
                patch.object(kw, "_kanban_dispatch_allowed", return_value=True), \
                patch.object(kw, "time", SimpleNamespace(time=lambda: clock["now"])), \
                patch.object(asyncio, "sleep", side_effect=fake_sleep), \
                patch.object(kbd, "reap_worker_zombies", return_value=[]):
            runner._sleep_between_ticks = tick_and_maybe_stop
            asyncio.run(runner._kanban_dispatcher_watcher())
    finally:
        kw.logger.removeHandler(handler)
    return lines, state["ticks"]


def test_a_full_fleet_names_the_capacity_hold():
    result = kbd.DispatchResult(capacity_hold="host_max_in_progress")

    lines, ticks = _warning_lines(result, ticks=12, seconds_per_tick=400)

    assert ticks == 12
    assert len(lines) == 1, lines
    line = lines[0]
    assert "0 workers spawned" in line
    assert "Last tick held back: at_capacity=host_max_in_progress." in line
    assert "at capacity" in line
    assert "Check profile health" not in line


def test_a_fault_hold_still_names_the_guard_and_advises_profile_health():
    result = kbd.DispatchResult(rate_limited=["t1"])

    lines, _ticks = _warning_lines(result, ticks=12, seconds_per_tick=400)

    assert len(lines) > 1, "a fault must keep warning on the normal cadence"
    line = lines[0]
    assert "Last tick held back: rate_limited=1." in line
    assert "Check profile health (venv, PATH, credentials)" in line
    assert "kanban list --status ready" in line
    assert "at capacity" not in line
