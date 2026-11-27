"""``ensure_gateway_runtime`` waiting on the owner's published startup lease.

The measured failure (2026-11-27): a 565 s cold start against a 60 s base budget ended every
client at ``starting: deadline``, and a supervisor probe that could not answer ended them at
``service_manager_unavailable`` beside a healthy owner. These tests run the real ensure loop on
a VIRTUAL clock, so a ten-minute start is exercised without spending ten minutes.
"""
from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import time

import pytest

from hermes_cli.gateway_runtime_service import remaining


class _Clock:
    """Virtual time: ``sleep`` advances it and nothing waits in wall time."""

    def __init__(self, epoch: float):
        self.mono = 1_000.0
        self.epoch = epoch

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.mono += seconds

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.epoch + (self.mono - 1_000.0)


@pytest.fixture
def clock(monkeypatch):
    import time as time_module

    virtual = _Clock(time_module.time())
    monkeypatch.setattr(time_module, "sleep", virtual.sleep)
    monkeypatch.setattr(time_module, "monotonic", virtual.monotonic)
    monkeypatch.setattr(time_module, "time", virtual.time)
    return virtual


def _publish(home, clock, *, ready_by: float, renewals: int = 0, updated_at: float | None = None,
             phase: str = "store_open") -> None:
    """Write the record the owner's writer publishes (``gateway/startup_progress.py``)."""
    stamp = datetime.fromtimestamp(clock.time() if updated_at is None else updated_at, tz=UTC)
    startup = {"pid": os.getpid(), "boot_started_at": clock.epoch, "phase": phase,
               "phase_started_at": clock.epoch, "phase_expected_s": 300.0,
               "ready_by": ready_by, "renewals": renewals, "ready_at": None}
    record = {"gateway_state": "starting", "pid": os.getpid(),
              "updated_at": stamp.isoformat(), "startup": startup}
    (home / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")


def _live_owner(monkeypatch):
    monkeypatch.setattr("gateway.runtime_ownership.live_runtime_owner", lambda home: os.getpid())


def _probe(clock, runtime, *, ready_after: float, heartbeat=None):
    """The real probe's contract: spend ``remaining(deadline)`` first, then answer."""
    start = clock.mono

    def observe(home, deadline, replaced):
        remaining(deadline)  # an exhausted budget is the deadline verdict, as in the real probe
        if heartbeat is not None:
            heartbeat()
        state = "ready" if clock.mono - start >= ready_after else "starting"
        return runtime.GatewayDiscovery(state)
    return observe


def _no_spawn(runtime, monkeypatch):
    from hermes_cli import gateway_runtime_multiplex as multiplex

    monkeypatch.setattr(runtime, "_update_fenced", lambda homes: False)
    monkeypatch.setattr(runtime, "_starting_multiplexer_root", lambda home: None)
    monkeypatch.setattr(runtime, "_standalone_multiplexer_reason", lambda root: None)
    monkeypatch.setattr(multiplex, "multiplexer_serves_home", lambda home: None)
    monkeypatch.setattr(multiplex, "implied_host_root", lambda home: None)


def test_a_565s_cold_start_with_a_published_lease_is_waited_out_to_ready(clock, tmp_path, monkeypatch):
    """The measured start: 565 s of cold start, a lease ending at +580 s, a 60 s base budget."""
    from hermes_cli import gateway_runtime as runtime

    home = tmp_path / "profile"
    home.mkdir()
    _no_spawn(runtime, monkeypatch)
    _live_owner(monkeypatch)
    ready_by = clock.time() + 580.0
    _publish(home, clock, ready_by=ready_by)
    monkeypatch.setattr(runtime, "_observe_replacing_stale", _probe(
        clock, runtime, ready_after=565.0,
        heartbeat=lambda: _publish(home, clock, ready_by=ready_by)))
    start = clock.mono
    result = runtime.ensure_gateway_runtime(home)
    assert (result.state, result.reason_code) == ("ready", None), result
    assert clock.mono - start >= 565.0  # the base 60 s was left far behind, and never a deadline


def test_a_lease_whose_writer_froze_is_owner_start_stalled_within_95s(clock, tmp_path, monkeypatch):
    """``updated_at`` frozen: three missed heartbeats is dead, named by phase, not by supervisor."""
    from hermes_cli import gateway_runtime as runtime

    home = tmp_path / "profile"
    home.mkdir()
    _no_spawn(runtime, monkeypatch)
    _live_owner(monkeypatch)
    _publish(home, clock, ready_by=clock.time() + 600.0)  # never refreshed again
    monkeypatch.setattr(runtime, "_observe_replacing_stale",
                        _probe(clock, runtime, ready_after=10_000.0))
    start = clock.mono
    result = runtime.ensure_gateway_runtime(home)
    assert (result.state, result.reason_code) == ("inaccessible", "owner_start_stalled"), result
    assert clock.mono - start <= 95.0
    assert result.detail is not None
    assert result.detail.startswith("store_open, ") and "last renewal" in result.detail


def test_a_lease_that_never_renews_past_ready_by_plus_the_grace_is_owner_start_stalled(
        clock, tmp_path, monkeypatch):
    from hermes_cli import gateway_runtime as runtime

    home = tmp_path / "profile"
    home.mkdir()
    _no_spawn(runtime, monkeypatch)
    _live_owner(monkeypatch)
    ready_by = clock.time()  # already due, and ``renewals`` never moves
    _publish(home, clock, ready_by=ready_by)
    monkeypatch.setattr(runtime, "_observe_replacing_stale", _probe(
        clock, runtime, ready_after=10_000.0,
        heartbeat=lambda: _publish(home, clock, ready_by=ready_by)))
    start = clock.mono
    result = runtime.ensure_gateway_runtime(home)
    assert (result.state, result.reason_code) == ("inaccessible", "owner_start_stalled"), result
    assert clock.mono - start <= 35.0  # 30 s grace, then the next poll
    assert result.detail is not None and "last renewal" in result.detail


def test_a_wedged_control_socket_still_names_the_stalled_owner(clock, tmp_path, monkeypatch):
    """A socket that exists and never answers must not spend the whole horizon unobserved: the
    lease is still sampled, so the verdict names the phase instead of ``starting: deadline``."""
    from hermes_cli import gateway_runtime as runtime

    home = tmp_path / "profile"
    home.mkdir()
    _no_spawn(runtime, monkeypatch)
    _live_owner(monkeypatch)
    _publish(home, clock, ready_by=clock.time() + 600.0)  # never refreshed again

    def observe(home_, deadline, replaced):
        time.sleep(max(0.0, deadline - time.monotonic()))  # a probe that spends its budget
        return runtime.GatewayDiscovery("inaccessible", reason_code="control_timeout")
    monkeypatch.setattr(runtime, "_observe_replacing_stale", observe)
    start = clock.mono
    result = runtime.ensure_gateway_runtime(home)
    assert (result.state, result.reason_code) == ("inaccessible", "owner_start_stalled"), result
    assert clock.mono - start <= 130.0
    assert result.detail is not None and result.detail.startswith("store_open, ")


def test_a_supervisor_that_does_not_answer_never_aborts_a_live_start(clock, tmp_path, monkeypatch):
    """A control-interface failure is not evidence of an absent owner: the lock is."""
    from hermes_cli import gateway_runtime as runtime, gateway_runtime_service as service

    home = tmp_path / "profile"
    home.mkdir()
    _no_spawn(runtime, monkeypatch)
    _live_owner(monkeypatch)
    ready_by = clock.time() + 300.0
    _publish(home, clock, ready_by=ready_by)
    absent_then_starting = _probe(clock, runtime, ready_after=1.0,
                                  heartbeat=lambda: _publish(home, clock, ready_by=ready_by))
    answers = iter(["absent"])

    def observe(home_, deadline, replaced):
        answer = next(answers, None)
        return (runtime.GatewayDiscovery(answer) if answer is not None
                else absent_then_starting(home_, deadline, replaced))
    monkeypatch.setattr(runtime, "_observe_replacing_stale", observe)

    def unavailable(*_args, **_kwargs):
        raise service.RuntimeStartError("service_manager_unavailable")
    monkeypatch.setattr(service, "discover_existing_gateway_service", unavailable)
    monkeypatch.setattr("hermes_cli.gateway_runtime_discovery.missing_owner_state",
                        lambda home: "starting")
    result = runtime.ensure_gateway_runtime(home)
    assert (result.state, result.reason_code) == ("ready", None), result


def test_a_supervisor_that_does_not_answer_without_a_lock_holder_is_supervisor_unreachable(
        tmp_path, monkeypatch):
    from hermes_cli import gateway_runtime as runtime, gateway_runtime_service as service

    home = tmp_path / "profile"
    home.mkdir()
    _no_spawn(runtime, monkeypatch)
    monkeypatch.setattr(runtime, "_observe_replacing_stale",
                        lambda home_, deadline, replaced: runtime.GatewayDiscovery("absent"))

    def unavailable(*_args, **_kwargs):
        raise service.RuntimeStartError("service_manager_unavailable")
    monkeypatch.setattr(service, "discover_existing_gateway_service", unavailable)
    monkeypatch.setattr("hermes_cli.gateway_runtime_discovery.missing_owner_state",
                        lambda home: "absent")
    result = runtime.ensure_gateway_runtime(home, timeout=5.0)
    assert (result.state, result.reason_code) == ("inaccessible", "supervisor_unreachable"), result


def test_an_explicit_timeout_stays_a_hard_deadline_despite_a_live_lease(tmp_path, monkeypatch):
    """An explicit ``timeout=`` is the caller's own outer budget: a lease may not move it."""
    from hermes_cli import gateway_runtime as runtime

    home = tmp_path / "profile"
    home.mkdir()
    _no_spawn(runtime, monkeypatch)
    _live_owner(monkeypatch)
    _publish(home, _RealClock(), ready_by=time.time() + 600.0)

    def observe(home_, deadline, replaced):
        remaining(deadline)
        return runtime.GatewayDiscovery("starting")
    monkeypatch.setattr(runtime, "_observe_replacing_stale", observe)
    before = time.monotonic()
    result = runtime.ensure_gateway_runtime(home, timeout=5.0)
    elapsed = time.monotonic() - before
    assert (result.state, result.reason_code) == ("starting", "deadline"), result
    assert 4.5 <= elapsed <= 6.5


class _RealClock:
    """The published record's stamps come from the process clock that is actually running."""

    def __init__(self):
        self.epoch = time.time()

    def time(self) -> float:
        return time.time()
