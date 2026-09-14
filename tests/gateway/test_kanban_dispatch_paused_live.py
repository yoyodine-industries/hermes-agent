"""Live ``kanban.dispatch_paused`` gate on the embedded kanban dispatcher.

The dispatcher reads most of its config once at boot, so before this key the only
way to stop it without a restart was the global emergency stop, which halts every
subsystem. These tests pin the two contracts that make the scoped key usable:
it is re-read on every tick (so it takes effect, and clears, without a restart),
and it never hides the global stop.
"""

from __future__ import annotations

import asyncio
import logging

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

PAUSE_ENV = "HERMES_KANBAN_DISPATCH_PAUSED"
LOGGER = "gateway.run"


async def _instant_sleep(_delay, *_args, **_kwargs):
    """Stand-in for ``asyncio.sleep`` so the watcher's boot settle costs no wall time."""
    return None


class _RecordingDispatcher:
    """Stands in for the per-tick board worker; records the work the loop asked for."""

    def __init__(self, *_args, **_kwargs):
        self.auto_decompose_calls = 0
        self.tick_once_calls = 0

    def auto_decompose_tick(self, per_tick):
        self.auto_decompose_calls += 1

    def tick_once(self):
        self.tick_once_calls += 1
        return []

    def ready_nonempty(self):
        return False


class _WatcherHost(GatewayKanbanWatchersMixin):
    """Drives the real dispatcher watcher for a fixed tick budget."""

    def __init__(self, ticks, on_tick_end=None):
        self._running = True
        self._kanban_dispatcher_lock_handle = None
        self._ticks_to_run = ticks
        self._on_tick_end = on_tick_end
        self.ticks = 0

    async def _sleep_between_ticks(self, interval):
        # Called once per completed tick: count it, let the test modify config
        # underneath the loop, and stop, without any wall-clock wait.
        self.ticks += 1
        if self._on_tick_end is not None:
            self._on_tick_end(self.ticks)
        if self.ticks >= self._ticks_to_run:
            self._running = False


def _kanban_config(paused):
    return {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 60,
            "dispatch_paused": paused,
        }
    }


def _raising_after_boot(config):
    """A config loader that works for the boot read and fails on every tick after."""

    calls = {"n": 0}

    def _load():
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("config unreadable")
        return config

    return _load


def _drive_ticks(monkeypatch, config, ticks, *, on_tick_end=None, estop_engaged=False,
                 load_config=None):
    """Run the real watcher loop for *ticks* ticks; return the recording dispatcher.

    Only collaborators that leave the tick are replaced: the board worker, the
    machine-global dispatcher lock, the zombie reaper, the ESTOP sentinel and the
    watcher's initial settle. The loop, the halt gate and its log lines are the real
    code under test.
    """
    from hermes_cli import config as config_mod
    from hermes_cli import kanban_db_dispatch as kbd
    import agent.estop as estop_mod

    monkeypatch.setattr(
        config_mod, "load_config", load_config if load_config is not None else (lambda: config)
    )
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(
        "gateway.kanban_watchers._acquire_singleton_lock", lambda _path: (None, "unavailable")
    )
    # Sentinel state, not the code under test: `check_paused` itself still runs.
    monkeypatch.setattr(estop_mod, "is_engaged", lambda: estop_engaged)

    built = []

    def _factory(*args, **kwargs):
        dispatcher = _RecordingDispatcher(*args, **kwargs)
        built.append(dispatcher)
        return dispatcher

    monkeypatch.setattr("gateway.kanban_watchers._KanbanDispatcher", _factory)
    # The watcher's only other sleep is its boot settle; ticks are paced by
    # `_sleep_between_ticks`, which the host overrides.
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    host = _WatcherHost(ticks, on_tick_end=on_tick_end)
    asyncio.run(asyncio.wait_for(host._kanban_dispatcher_watcher(), timeout=10))

    assert host.ticks == ticks, "watcher did not run the requested number of ticks"
    assert len(built) == 1, "watcher did not build exactly one dispatcher"
    return built[0]


def _held_lines(caplog):
    return [r.getMessage() for r in caplog.records if "tick held" in r.getMessage()]


def test_paused_tick_does_no_work_and_says_so_every_tick(monkeypatch, caplog):
    """A paused tick claims nothing, is visible in the log, and lifts with no restart."""
    monkeypatch.delenv(PAUSE_ENV, raising=False)
    config = _kanban_config(paused=False)

    def _flip_between_ticks(completed_ticks):
        # Same loop, same process: the config value changes underneath it.
        config["kanban"]["dispatch_paused"] = completed_ticks == 1

    with caplog.at_level(logging.INFO, logger=LOGGER):
        dispatcher = _drive_ticks(
            monkeypatch, config, ticks=3, on_tick_end=_flip_between_ticks
        )

    # Ticks 1 and 3 dispatched; the paused tick 2 spawned and decomposed nothing.
    assert dispatcher.tick_once_calls == 2
    assert dispatcher.auto_decompose_calls == 2

    messages = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    held = [m for m in messages if "tick held" in m]
    assert len(held) == 1, messages
    assert "kanban.dispatch_paused" in held[0], held[0]
    assert len([m for m in messages if "PAUSED via" in m]) == 1, messages
    assert len([m for m in messages if "RESUMED" in m]) == 1, messages


def test_unreadable_config_fails_safe_and_never_hides_the_emergency_stop(monkeypatch, caplog):
    """A config read error halts the tick; an engaged estop is reported as itself."""
    monkeypatch.delenv(PAUSE_ENV, raising=False)

    # (a) Config unreadable mid-run: do not dispatch on a guess.
    with caplog.at_level(logging.INFO, logger=LOGGER):
        unreadable = _drive_ticks(
            monkeypatch, _kanban_config(paused=False), ticks=1,
            load_config=_raising_after_boot(_kanban_config(paused=False)),
        )

    assert unreadable.tick_once_calls == 0
    assert unreadable.auto_decompose_calls == 0
    held = _held_lines(caplog)
    assert len(held) == 1, held
    assert "unreadable config" in held[0], held[0]

    # (b) Both stops engaged: the emergency stop is named, not masked by the scoped key.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        stopped = _drive_ticks(
            monkeypatch, _kanban_config(paused=True), ticks=1, estop_engaged=True
        )

    assert stopped.tick_once_calls == 0
    held = _held_lines(caplog)
    assert len(held) == 1, held
    assert "emergency stop" in held[0], held[0]
    assert "kanban.dispatch_paused" in held[0], held[0]
