"""I1/I2/I3/I4 — a shutdown that cannot finish must still exit (card t_0dc4f0d4, D3).

Measured failure (pid 10538, 2026-10-22): the drain timed out, the SessionDB close was skipped,
and the process then vanished into *ordinary teardown file I/O* in ``_stop_persist_exit_state``
— no ``exit_clean``, no ``exit_nonzero``, no watchdog dump. Two defects:

* the out-of-loop backstop did filesystem work (``mkdir`` + ``open`` + ``write`` + ledger/runtime
  writes) BEFORE ``os._exit``, so a wedged filesystem silenced the watchdog too (I2);
* ``_stop_impl`` disarmed that backstop in ``finally:``, so any phase raising left a live, dark
  process with the backstop switched off (I3).

These tests pin the exit door: after the deadline, nothing on the exit path may block, and every
teardown path funnels through it.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import pytest

import gateway.shutdown_watchdog as shutdown_watchdog_module
from gateway.run import GatewayRunner
from gateway.run_shutdown import GatewayShutdownMixin
from gateway.shutdown_watchdog import (
    ShutdownExitDoor,
    get_exit_diag_path,
    run_bounded_step,
)


def _dead_after(fn, *, timeout=3.0, join_timeout=5.0):
    """Run ``fn`` on a daemon thread and return (elapsed, error). Fails if it never returns."""
    box = {}

    def _run():
        box["error"] = None
        try:
            fn()
        except BaseException as exc:
            box["error"] = exc

    started = time.monotonic()
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        pytest.fail(f"{fn!r} did not return within {timeout}s — the exit path blocked")
    if box.get("error") is not None:
        raise box["error"]
    return time.monotonic() - started


# ---------------------------------------------------------------------------------------------
# I2 — the backstop is I/O-free on its exit path
# ---------------------------------------------------------------------------------------------


def test_exit_door_does_not_block_the_exit_on_a_wedged_dump(tmp_path, monkeypatch):
    """A dump write that blocks forever must not delay ``os._exit`` past the join budget."""
    door = ShutdownExitDoor.build(exit_code=11, dump_path=tmp_path / "logs" / "dump.log",
                                 diag_path=tmp_path / "logs" / "diag.log")
    exits = []
    monkeypatch.setattr(shutdown_watchdog_module.os, "_exit", lambda code: exits.append(code))

    release = threading.Event()

    def _wedged_dump(*args, **kwargs):
        release.wait(timeout=30.0)  # the wedged filesystem: open/write never completes

    monkeypatch.setattr(door, "_write_forensic_dump", _wedged_dump)

    elapsed = _dead_after(lambda: door.fire("test_wedged_dump"))
    release.set()

    assert exits == [11], "the exit door must os._exit whatever the forensic thread is doing"
    assert elapsed < door.join_budget_s + 1.5, f"exit door took {elapsed:.1f}s"


def test_exit_door_does_not_block_the_exit_on_a_wedged_stamp(tmp_path, monkeypatch):
    """``_mark_exited_quietly`` (ledger + runtime-status writes) may block; the exit may not."""
    door = ShutdownExitDoor.build(exit_code=12, dump_path=tmp_path / "logs" / "dump.log",
                                  diag_path=tmp_path / "logs" / "diag.log")
    exits = []
    monkeypatch.setattr(shutdown_watchdog_module.os, "_exit", lambda code: exits.append(code))
    release = threading.Event()
    monkeypatch.setattr(shutdown_watchdog_module, "_mark_exited_quietly",
                        lambda *a, **k: release.wait(timeout=30.0))

    elapsed = _dead_after(lambda: door.fire("test_wedged_stamp"))
    release.set()

    assert exits == [12]
    assert elapsed < door.join_budget_s + 1.5, f"exit door took {elapsed:.1f}s"


def test_exit_door_pre_opens_its_fds_so_the_deadline_path_writes_nothing_new(tmp_path, monkeypatch):
    """The dump + exit-diag fds are opened BEFORE the deadline (I/O is allowed there, never after).

    After the deadline the door may only ``os.write`` on those already-open fds — so a
    filesystem that refuses ``open``/``mkdir`` at deadline time cannot silence it.
    """
    dump = tmp_path / "logs" / "dump.log"
    diag = tmp_path / "logs" / "diag.log"
    door = ShutdownExitDoor.build(exit_code=1, dump_path=dump, diag_path=diag)
    assert door.dump_fd is not None and door.diag_fd is not None

    monkeypatch.setattr(shutdown_watchdog_module.os, "_exit", lambda code: None)

    def _forbid_open(path, *a, **k):
        raise OSError("filesystem wedged at deadline time")

    monkeypatch.setattr(shutdown_watchdog_module.os, "open", _forbid_open)
    monkeypatch.setattr(shutdown_watchdog_module.os, "makedirs", _forbid_open)

    door.fire("test_no_open_after_deadline")

    diag_lines = [json.loads(line) for line in diag.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert diag_lines, "the post-deadline path must stamp the exit path in gateway-exit-diag.log"
    assert diag_lines[-1]["path"] == "watchdog"
    assert diag_lines[-1]["reason"] == "test_no_open_after_deadline"
    assert "shutdown_watchdog_fired" in dump.read_text(encoding="utf-8")


def test_armed_shutdown_watchdog_fires_through_the_exit_door(tmp_path, monkeypatch):
    """``arm_shutdown_watchdog`` routes its deadline through the same door (one exit path)."""
    door = ShutdownExitDoor.build(exit_code=7, dump_path=tmp_path / "logs" / "dump.log",
                                  diag_path=tmp_path / "logs" / "diag.log")
    exits, fired = [], threading.Event()

    def _exit(code):
        exits.append(code)
        fired.set()

    monkeypatch.setattr(shutdown_watchdog_module.os, "_exit", _exit)
    done = threading.Event()
    shutdown_watchdog_module.arm_shutdown_watchdog(0.15, done_event=done, door=door)
    assert fired.wait(timeout=6.0), "the armed watchdog never exited"
    assert exits == [7]
    assert get_exit_diag_path(tmp_path).name == "gateway-exit-diag.log"


# ---------------------------------------------------------------------------------------------
# I1 — no-dark: a teardown that cannot finish still exits within the leash
# ---------------------------------------------------------------------------------------------


def test_wedged_teardown_still_exits_within_the_leash(monkeypatch, tmp_path):
    """I1: the process terminates within ``effective_stop_watchdog_delay``. There is no third outcome.

    This is the Oct 22 shape: the drain is done, a later phase blocks forever in ordinary I/O. The
    exit must come from the armed backstop, through the door, inside the leash.
    """
    import contextlib

    from gateway import run_shutdown as rs

    exits = []
    monkeypatch.setattr(shutdown_watchdog_module.os, "_exit", lambda code: exits.append(code))

    order = []
    _patch_phases(monkeypatch, raising_phase=None, order=order)

    async def _wedged_drain(self, timeout, ctx):
        await asyncio.Event().wait()  # never returns: the teardown is wedged, not slow

    monkeypatch.setattr(GatewayShutdownMixin, "_stop_drain_active_work", _wedged_drain, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    leash = 0.25
    monkeypatch.setattr(rs, "_effective_watchdog_leash", lambda runner: leash, raising=False)
    door = ShutdownExitDoor.build(exit_code=1, dump_path=tmp_path / "logs" / "dump.log",
                                  diag_path=tmp_path / "logs" / "diag.log")
    monkeypatch.setattr(rs, "ShutdownExitDoor", type("D", (), {"build": staticmethod(lambda **k: door)}))

    double = _TeardownDouble()
    started = time.monotonic()

    def _run():
        with contextlib.suppress(BaseException):
            asyncio.run(double._stop_impl())

    threading.Thread(target=_run, daemon=True, name="wedged-teardown").start()
    deadline = time.monotonic() + 10.0
    while not exits and time.monotonic() < deadline:
        time.sleep(0.02)
    elapsed = time.monotonic() - started

    assert exits == [1], "a wedged teardown must still exit through the backstop (I1)"
    assert elapsed < leash + 2.0, f"exit came at {elapsed:.1f}s, outside leash {leash}s + 2s"
    assert "gateway.exit_watchdog" in (tmp_path / "logs" / "diag.log").read_text(encoding="utf-8")
    assert "shutdown_watchdog_fired" in (tmp_path / "logs" / "dump.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# I5 — a step's failure is logged WITH the step's name, and one wedge does not eat the rest
# ---------------------------------------------------------------------------------------------


def test_run_bounded_step_returns_false_and_names_the_step(caplog):
    """A wedged step is reported by name and does not hang the caller."""
    release = threading.Event()
    with caplog.at_level("WARNING"):
        ok = run_bounded_step("remove_pid_file", lambda: release.wait(timeout=30.0), timeout_s=0.2)
        release.set()
    assert ok is False
    assert "remove_pid_file" in caplog.text


def test_run_bounded_step_returns_true_on_a_normal_step():
    seen = []
    assert run_bounded_step("clean_step", lambda: seen.append(1), timeout_s=2.0) is True
    assert seen == [1]


# ---------------------------------------------------------------------------------------------
# I3 — no silent disarm: a teardown phase that raises hard-exits, naming the phase
# ---------------------------------------------------------------------------------------------


class _TeardownDouble(GatewayShutdownMixin):
    """Minimal runner for ``_stop_impl``; the ``_stop_*`` phases are monkeypatched on GatewayRunner."""

    def __init__(self):
        self._restart_requested = False
        self._restart_drain_timeout = 0.01
        self._running = True
        self._draining = False
        self._shutdown_watchdog_done = None

    def _shutdown_watchdog_snapshot(self, ctx):
        return {}


def _patch_phases(monkeypatch, *, raising_phase, order):
    from gateway.run_shutdown import GatewayShutdownMixin

    async def _ok(self, *a, **k):
        if a and hasattr(a[0], "elapsed"):
            pass
        order.append("ok")

    async def _begin(self, ctx):
        ctx.started_at = time.monotonic()
        order.append("begin")

    async def _drain(self, timeout, ctx):
        order.append("drain")

    async def _interrupt(self, ctx):
        order.append("interrupt")

    async def _finalize(self, ctx):
        order.append("finalize")

    async def _release(self, ctx):
        order.append("release")

    async def _persist(self, ctx):
        order.append("persist")

    phases = {
        "begin_teardown": _begin,
        "drain_active_work": _drain,
        "interrupt_remaining_work": _interrupt,
        "finalize_agents_and_adapters": _finalize,
        "release_runtime_state": _release,
        "persist_exit_state": _persist,
    }
    for name, fn in phases.items():
        if name == raising_phase:
            async def _boom(self, *a, _name=name, **k):
                raise RuntimeError(f"{_name} exploded")

            fn = _boom
        monkeypatch.setattr(GatewayShutdownMixin, f"_stop_{name}", fn, raising=False)
    monkeypatch.setattr(GatewayShutdownMixin, "_stop_quiesce_and_close_session_dbs",
                        lambda self, timeout, ctx: order.append("quiesce"), raising=False)
    return GatewayShutdownMixin


def test_teardown_phase_that_raises_hard_exits_naming_the_phase(monkeypatch):
    """I3: no exception in a teardown phase may leave a live dark process with the backstop off."""
    fired = []

    class _Door:
        join_budget_s = 2.0

        def fire(self, reason, **kwargs):
            fired.append(reason)

    order = []
    _patch_phases(monkeypatch, raising_phase="release_runtime_state", order=order)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    from gateway import run_shutdown as rs

    monkeypatch.setattr(rs, "ShutdownExitDoor", type("D", (), {"build": staticmethod(lambda **k: _Door())}),
                        raising=False)
    monkeypatch.setattr(rs, "arm_shutdown_watchdog", lambda *a, **k: threading.Event(), raising=False)

    double = _TeardownDouble()
    with pytest.raises(RuntimeError):
        asyncio.run(double._stop_impl())

    assert fired, "an abandoned teardown must go through the exit door, not disarm and return"
    assert "release_runtime_state" in fired[0]


def test_completed_teardown_disarms_without_firing_the_door(monkeypatch):
    fired = []

    class _Door:
        join_budget_s = 2.0

        def fire(self, reason, **kwargs):  # pragma: no cover - must not be reached
            fired.append(reason)

    order = []
    _patch_phases(monkeypatch, raising_phase=None, order=order)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    from gateway import run_shutdown as rs

    monkeypatch.setattr(rs, "ShutdownExitDoor", type("D", (), {"build": staticmethod(lambda **k: _Door())}),
                        raising=False)
    monkeypatch.setattr(rs, "arm_shutdown_watchdog", lambda *a, **k: threading.Event(), raising=False)

    double = _TeardownDouble()
    asyncio.run(double._stop_impl())

    assert fired == []
    assert double._shutdown_watchdog_done is not None and double._shutdown_watchdog_done.is_set()
    assert order[:2] == ["begin", "drain"]


# ---------------------------------------------------------------------------------------------
# I4 — identity released BEFORE the drain
# ---------------------------------------------------------------------------------------------


def test_pid_file_and_runtime_lock_are_released_before_the_drain(monkeypatch):
    """I4: a corpse must not look ``running`` to ``read_runtime_status`` nor hold the runtime lock."""
    from gateway import run_shutdown as rs
    from gateway.run_shutdown import GatewayShutdownMixin

    order = []

    class _Double:
        _restart_requested = False
        _running = True
        _draining = False

        def _clear_plugin_message_injector(self):
            pass

        def _mark_api_runs_shutdown_requested(self):
            pass

        async def _cancel_secondary_profile_reconnect_tasks(self):
            pass

        async def _notify_active_sessions_of_shutdown(self):
            order.append("notify")

    monkeypatch.setattr("gateway.status.remove_pid_file", lambda: order.append("remove_pid_file"))
    monkeypatch.setattr("gateway.status.release_gateway_runtime_lock", lambda: order.append("release_lock"))

    async def _drain(self, timeout, ctx):
        order.append("drain")

    monkeypatch.setattr(GatewayShutdownMixin, "_stop_drain_active_work", _drain, raising=False)

    double = _Double()
    ctx = GatewayShutdownMixin._StopContext(deferred_count=lambda: 0)
    asyncio.run(rs.GatewayShutdownMixin._stop_begin_teardown(double, ctx))

    assert "remove_pid_file" in order and "release_lock" in order
    assert max(order.index("remove_pid_file"), order.index("release_lock")) < len(order)
    # …and released before anything that can block: the drain runs later by definition.
    first_release = min(order.index("remove_pid_file"), order.index("release_lock"))
    assert first_release <= order.index("notify")
