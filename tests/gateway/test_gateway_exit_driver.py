"""Regression tests for the process-exit path (t_58fe16cb).

Live symptom: ``hermes gateway run`` logged its exit verdict and released the PID file, then never
exited — the process sat in the selector with no LISTEN sockets and no watchdog dump until it was
SIGKILLed, so launchd could not restart a PID that still looked alive.

Two halves are pinned here:

* the exit driver must not hand the process to ``asyncio.run``'s teardown, which joins the loop's
  default executor (the gateway's generic blocking pool) and can wait there forever;
* the exit leash must cover the window after ``stop()`` returned, where the stop-phase watchdog has
  already been disarmed, and must be released by the real exit path.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import gateway.run as gateway_run


class _ExitCalled(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _raise_exit(code: int) -> None:
    raise _ExitCalled(code)


# --------------------------------------------------------------------------------------
# The exit driver
# --------------------------------------------------------------------------------------

def test_exit_driver_does_not_wait_for_a_blocked_default_executor_worker():
    """A ``to_thread``/``run_in_executor(None, …)`` call that never returns must not hold the exit.

    This is the live shape: the drain interrupts an in-flight run, but the run's blocking call is
    still occupying a worker of the loop's default executor when the gateway coroutine returns. The
    driver must return anyway — ``os._exit`` ends that worker; a join only strands the PID.
    """
    started = threading.Event()
    release = threading.Event()
    worker_done = threading.Event()

    def _blocks_forever() -> None:
        started.set()
        try:
            release.wait(timeout=60)
        finally:
            worker_done.set()

    async def _start_gateway() -> bool:
        asyncio.get_running_loop().run_in_executor(None, _blocks_forever)
        for _ in range(500):  # bounded: fail loudly if the worker never starts
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        return False

    began = time.monotonic()
    try:
        result = gateway_run._run_gateway_until_verdict(_start_gateway())
        elapsed = time.monotonic() - began
    finally:
        release.set()

    assert started.is_set(), "the blocked worker never started; the test proved nothing"
    assert result is False
    assert elapsed < 5, "the exit driver waited on the default executor"
    assert not worker_done.is_set(), "the driver joined the blocked worker instead of leaving it"


def test_exit_driver_propagates_systemexit_codes():
    """The restart/fatal-config codes must arrive at the caller unchanged (main() routes them through
    ``os._exit``), even though the driver closes its loop on the way out."""
    async def _requests_restart() -> bool:
        raise SystemExit(75)

    with pytest.raises(SystemExit) as excinfo:
        gateway_run._run_gateway_until_verdict(_requests_restart())

    assert excinfo.value.code == 75


def test_exit_driver_closes_its_own_loop():
    """The driver owns its loop: it must not leave a running loop behind for the caller's exit path.
    Run on a worker thread so the test's own thread-local loop state is untouched."""
    captured: dict = {}
    outcome: dict = {}

    def _run() -> None:
        async def _returns_false() -> bool:
            captured["loop"] = asyncio.get_running_loop()
            return False

        outcome["result"] = gateway_run._run_gateway_until_verdict(_returns_false())

    thread = threading.Thread(target=_run, name="exit-driver-loop-test")
    thread.start()
    thread.join(timeout=10)

    assert outcome.get("result") is False
    assert captured["loop"].is_closed()


def test_asyncio_run_still_wedges_on_the_same_shape():
    """Canary: ``asyncio.run`` — the driver this replaces — blocks on that same worker.

    The child process is killed by the timeout, which *is* the assertion. If a future CPython stops
    joining the default executor, this skip fires instead of failing: the driver change stays correct
    either way, it would just no longer be load-bearing.
    """
    script = textwrap.dedent(
        """
        import asyncio, threading

        release = threading.Event()

        def _blocks_forever():
            release.wait(timeout=30)

        async def _start_gateway():
            asyncio.get_running_loop().run_in_executor(None, _blocks_forever)
            await asyncio.sleep(0.2)   # let the worker start
            return False

        asyncio.run(_start_gateway())
        print("RETURNED")
        """
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:
        return  # wedged in asyncio's teardown, exactly as the live process was
    assert "RETURNED" in proc.stdout, proc.stderr
    pytest.skip("this CPython no longer joins the default executor during asyncio.run() teardown")


# --------------------------------------------------------------------------------------
# The exit leash
# --------------------------------------------------------------------------------------

def test_arm_exit_leash_is_skipped_under_pytest(monkeypatch):
    """In-process tests drive start_gateway() to completion and never reach the exit backstop, so a
    leash armed in the tail would hard-exit the test worker minutes later."""
    import gateway.shutdown_watchdog as watchdog

    calls = []
    monkeypatch.setattr(watchdog, "arm_shutdown_watchdog", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(gateway_run, "_exit_leash_done", None)

    gateway_run._arm_exit_leash(object(), None, None, False)

    assert calls == []
    assert gateway_run._exit_leash_done is None


def test_arm_exit_leash_covers_the_exit_tail(monkeypatch):
    import gateway.shutdown_watchdog as watchdog

    captured = {}

    def _fake_arm(delay, **kwargs):
        captured["delay"] = delay
        captured.update(kwargs)
        return kwargs["done_event"]

    monkeypatch.setattr(watchdog, "arm_shutdown_watchdog", _fake_arm)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(gateway_run, "_exit_leash_done", None)

    gateway_run._arm_exit_leash(SimpleNamespace(is_closed=lambda: True), _finished_thread("cron"),
                                _finished_thread("housekeeping"), True)

    assert captured["delay"] == pytest.approx(
        gateway_run._SHUTDOWN_TAIL_BUDGET_S + watchdog.DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S)
    # The leash must outlast the bounded joins the tail itself performs.
    assert captured["delay"] > (
        gateway_run._CRON_SHUTDOWN_DRAIN_TIMEOUT + gateway_run._HOUSEKEEPING_SHUTDOWN_DRAIN_TIMEOUT)
    assert captured["exit_code"] == 1
    assert captured["name"] == "gateway-exit-leash"
    assert gateway_run._exit_leash_done is captured["done_event"]

    snapshot = captured["snapshot_fn"]()
    assert snapshot["phase"] == "gateway_exit_tail"
    assert snapshot["signal_initiated"] is True
    assert snapshot["threads"], "the dump is useless without the thread list"
    assert snapshot["cron_thread_alive"] is False  # the cron ticker is gone by the tail
    assert snapshot["housekeeping_thread_alive"] is False
    assert "pending_tasks" not in snapshot, "a closed loop has no tasks to enumerate"


def test_exit_backstop_disarms_the_exit_leash(monkeypatch):
    """``_exit_after_graceful_shutdown`` is the real exit point: release the leash there, or it would
    race the intentional exit and mark a clean life unclean with code 1."""
    from gateway import status as gateway_status

    monkeypatch.setattr(gateway_status, "remove_pid_file", Mock())
    monkeypatch.setattr(gateway_status, "release_gateway_runtime_lock", Mock())
    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "stdout", SimpleNamespace(flush=Mock()))
    monkeypatch.setattr(gateway_run.sys, "stderr", SimpleNamespace(flush=Mock()))
    done = threading.Event()
    monkeypatch.setattr(gateway_run, "_exit_leash_done", done)

    with pytest.raises(_ExitCalled) as excinfo:
        gateway_run._exit_after_graceful_shutdown(78)

    assert excinfo.value.code == 78
    assert done.is_set(), "the exit leash was still counting down at the exit"
    assert gateway_run._exit_leash_done is None


def test_disarm_exit_leash_is_idempotent():
    gateway_run._disarm_exit_leash()  # nothing armed: no raise, no state
    assert gateway_run._exit_leash_done is None


def _finished_thread(name: str) -> threading.Thread:
    """A real (already-exited) Thread, so the tail's cooperative joins return immediately."""
    thread = threading.Thread(target=lambda: None, name=name)
    thread.start()
    thread.join(timeout=5)
    return thread


def test_shutdown_tail_arms_the_exit_leash(monkeypatch):
    """The arm must happen in the tail — that is where stop() has returned and the stop-phase
    watchdog is already disarmed — and must carry the signal-initiated flag."""
    armed = []

    def _fake_arm(*args, **kwargs):
        armed.append((args, kwargs))

    async def _mcp_noop(timeout: float = 5.0) -> bool:
        return True

    monkeypatch.setattr(gateway_run, "_arm_exit_leash", _fake_arm)
    monkeypatch.setattr(gateway_run, "_shutdown_mcp_servers_nonblocking", _mcp_noop)
    monkeypatch.setattr(gateway_run, "_resolve_gateway_exit_verdict", lambda runner, signal: True)

    verdict = asyncio.run(gateway_run._start_gateway_shutdown_tail(
        SimpleNamespace(), None, threading.Event(), None,
        _finished_thread("cron-ticker"), _finished_thread("gateway-housekeeping"),
        threading.Event(), _finished_thread("planned-stop-watcher"), [True]))

    assert verdict is True
    assert len(armed) == 1, "the exit tail must arm the leash exactly once"
    (_loop, _cron, _housekeeping, signal_initiated), _kwargs = armed[0]
    assert signal_initiated is True
