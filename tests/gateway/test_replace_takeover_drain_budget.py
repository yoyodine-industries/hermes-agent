"""Regression: the ``--replace`` takeover must not SIGKILL a drain the old gateway is inside.

The incumbent's ``stop()`` path may legally spend ``cron_drain_timeout`` plus the cleanup reserve
on in-flight cron work. Killing it sooner destroys that job mid-write, and jobs.json then records
it as a permanent failure. The takeover therefore waits out the same budget the service manager's
``TimeoutStopSec`` is sized to (``resolve_systemd_timeout_stop_sec``) before escalating to SIGKILL.
"""

import pytest

from gateway import run as gateway_run
from gateway import status
from gateway.restart import (
    CRON_DRAIN_CLEANUP_RESERVE_S,
    DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT,
    SYSTEMD_STOP_HEADROOM_S,
    SYSTEMD_TIMEOUT_STOP_SEC_FLOOR,
)

TARGET_PID = 4242


def _expected_wait(cron_drain: float) -> float:
    """The takeover's contract, derived here independently of the production resolver: cover the
    incumbent's drain (cron floor + cleanup reserve) plus the service manager's headroom, never
    below the TimeoutStopSec floor."""
    return max(SYSTEMD_TIMEOUT_STOP_SEC_FLOOR,
               (cron_drain + CRON_DRAIN_CLEANUP_RESERVE_S) + SYSTEMD_STOP_HEADROOM_S)


def test_replace_exit_wait_budget_covers_the_cron_drain_floor(monkeypatch):
    """A configured cron floor must survive into the takeover budget (the raw ``0`` chat default
    must not be what sizes the wait)."""
    monkeypatch.setenv("HERMES_CRON_DRAIN_TIMEOUT", "120")

    budget = gateway_run._resolve_replace_exit_wait_budget()

    assert budget >= 120 + CRON_DRAIN_CLEANUP_RESERVE_S, (
        "the takeover wait must cover the cron drain the incumbent may still be inside"
    )


def test_replace_exit_wait_budget_never_falls_back_to_a_20s_wait(monkeypatch):
    """With no cron override the budget still covers the default cron floor — the superseded
    hardcoded 20s SIGTERM window was shorter than the drain this process may legally spend."""
    monkeypatch.delenv("HERMES_CRON_DRAIN_TIMEOUT", raising=False)
    monkeypatch.delenv("HERMES_RESTART_DRAIN_TIMEOUT", raising=False)

    budget = gateway_run._resolve_replace_exit_wait_budget()

    assert budget >= DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT + CRON_DRAIN_CLEANUP_RESERVE_S
    assert budget > 20.0


@pytest.mark.asyncio
async def test_takeover_does_not_sigkill_before_the_drain_budget(monkeypatch):
    """A target that never exits must be SIGKILLed only after the full drain budget has elapsed.

    Real code path (``_start_gateway_replace_existing_instance`` + ``_wait_for_pid_exit``); the
    process clock is faked at the ``asyncio.sleep`` seam so the assertion is about the wait the
    code performs, not about wall-clock.
    """
    cron_drain = 120.0
    monkeypatch.setenv("HERMES_CRON_DRAIN_TIMEOUT", str(int(cron_drain)))
    clock = {"t": 0.0}
    events: list[tuple[bool, float]] = []

    async def _fake_sleep(delay):
        clock["t"] += delay

    def _fake_terminate(pid, force=False, expected_start_time=None):
        events.append((force, round(clock["t"], 3)))

    monkeypatch.setattr(gateway_run.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(gateway_run, "_replace_target_belongs_to_other_profile", lambda _pid: False)
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)  # never exits on its own
    monkeypatch.setattr(status, "get_process_start_time", lambda _pid: 111)
    monkeypatch.setattr(status, "write_takeover_marker", lambda _pid: None)
    monkeypatch.setattr(status, "_snapshot_gateway_children", lambda _pid: [])
    monkeypatch.setattr(status, "terminate_pid", _fake_terminate)

    replaced = await gateway_run._start_gateway_replace_existing_instance(TARGET_PID, True)

    assert replaced is False, "a target that outlives SIGKILL must abort the replacement"
    assert [force for force, _ in events] == [False, True], "SIGTERM first, SIGKILL on timeout"
    sigkill_at = events[1][1]
    assert sigkill_at >= cron_drain + CRON_DRAIN_CLEANUP_RESERVE_S, (
        "SIGKILL was sent while the incumbent could still be draining cron work"
    )
    expected = _expected_wait(cron_drain)
    assert abs(sigkill_at - expected) <= 0.5, (
        f"takeover waited {sigkill_at}s, not the {expected}s drain leash it must honour"
    )


@pytest.mark.asyncio
async def test_takeover_stops_waiting_as_soon_as_the_target_exits(monkeypatch):
    """The budget is a cap, not a fixed delay: an ordinary exit must not be waited out."""
    monkeypatch.setenv("HERMES_CRON_DRAIN_TIMEOUT", "120")
    clock = {"t": 0.0}
    alive = iter([True, False])
    events: list[bool] = []

    async def _fake_sleep(delay):
        clock["t"] += delay

    monkeypatch.setattr(gateway_run.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(gateway_run, "_replace_target_belongs_to_other_profile", lambda _pid: False)
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: next(alive))
    monkeypatch.setattr(status, "get_process_start_time", lambda _pid: 111)
    monkeypatch.setattr(status, "write_takeover_marker", lambda _pid: None)
    monkeypatch.setattr(status, "_snapshot_gateway_children", lambda _pid: [])
    monkeypatch.setattr(status, "terminate_pid", lambda pid, force=False, **kw: events.append(force))
    monkeypatch.setattr(status, "reap_gateway_children", lambda children, **kw: 0)
    monkeypatch.setattr(status, "remove_pid_file", lambda: None)
    monkeypatch.setattr(status, "release_all_scoped_locks", lambda **kw: 0)

    replaced = await gateway_run._start_gateway_replace_existing_instance(TARGET_PID, True)

    assert replaced is True
    assert events == [False], "a target that exited on SIGTERM must not be SIGKILLed"
    assert clock["t"] < 120 + CRON_DRAIN_CLEANUP_RESERVE_S, (
        "an exited target must not be waited out for the whole budget"
    )
