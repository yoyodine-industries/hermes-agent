"""The lane-scoped lockdown at the CRON seam.

A lockdown is not a cron halt: the tick keeps running and each due job is admitted or held by
its OWN lane (the profile whose store this tick is serving). A held job must not be advanced —
leaving it due is what makes it fire on the first tick after the lift instead of losing its
period — and the deferral must be visible, naming the job and its profile.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agent import estop


@pytest.fixture(autouse=True)
def operator_context(monkeypatch):
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nobody")
    estop._logged_components.clear()
    from cron import scheduler_tick

    scheduler_tick._LOCKDOWN_REPORTED.clear()
    return home


@pytest.fixture
def tick_harness(monkeypatch):
    """Drive ``scheduler.tick`` with one due job and a recorder instead of a worker pool."""
    from cron import scheduler, scheduler_tick

    job = {"id": "j-1", "name": "j-1"}
    seen: dict = {"advanced": [], "dispatched": [], "scans": 0}

    def _due():
        seen["scans"] += 1
        return [job]

    monkeypatch.setattr(scheduler, "get_due_jobs", _due)
    monkeypatch.setattr(scheduler, "advance_next_runs", lambda ids: seen["advanced"].extend(ids))
    monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda workers: None)

    def _submit(job, pool, fn):
        seen["dispatched"].append(job["id"])
        return None

    monkeypatch.setattr(scheduler, "_submit_with_guard", _submit)
    return seen


def test_a_held_lanes_job_is_deferred_and_never_dispatched(
    hermes_home, tick_harness, monkeypatch, caplog,
):
    from cron import scheduler, scheduler_tick

    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
                  allow={"profiles": ["default"]})
    monkeypatch.setattr(scheduler_tick, "_tick_lane", lambda: "yoyoflow")

    with caplog.at_level(logging.WARNING):
        assert scheduler.tick(verbose=False) == 0

    assert tick_harness["dispatched"] == [], "a held lane's job must not reach the executor"
    assert tick_harness["advanced"] == [], "and it must stay due, to fire after the lift"
    assert any(
        "j-1" in record.getMessage() and "yoyoflow" in record.getMessage()
        for record in caplog.records
    ), "the deferral must be RECORDED, naming the job and the profile"


def test_the_deferral_is_reported_once_per_engagement(
    hermes_home, tick_harness, monkeypatch, caplog,
):
    from cron import scheduler, scheduler_tick

    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
                  allow={"profiles": ["default"]})
    monkeypatch.setattr(scheduler_tick, "_tick_lane", lambda: "yoyoflow")

    with caplog.at_level(logging.WARNING):
        scheduler.tick(verbose=False)
        scheduler.tick(verbose=False)
    assert sum("held by the lane-scoped lockdown" in r.getMessage() for r in caplog.records) == 1


def test_an_allowlisted_lanes_job_still_fires(hermes_home, tick_harness, monkeypatch):
    from cron import scheduler, scheduler_tick

    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
                  allow={"profiles": ["default"]})
    monkeypatch.setattr(scheduler_tick, "_tick_lane", lambda: "default")

    scheduler.tick(verbose=False)

    assert tick_harness["dispatched"] == ["j-1"]
    assert tick_harness["advanced"] == ["j-1"]


def test_a_total_pause_still_halts_the_tick_entirely(hermes_home, tick_harness):
    from cron import scheduler

    estop.acquire(owner="operator", mode=estop.MODE_ESTOP, reason="total halt")
    assert scheduler.tick(verbose=False) == 0
    assert tick_harness["scans"] == 0, "a total halt never even scans for due jobs"


def test_a_lockdown_does_reach_the_per_job_gate(hermes_home, tick_harness):
    from cron import scheduler

    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
                  allow={"profiles": ["default"]})
    scheduler.tick(verbose=False)
    assert tick_harness["scans"] == 1, "a lockdown is not a halt: the tick runs"


def test_the_misfire_sweep_is_scoped_to_the_lane_it_serves(hermes_home, monkeypatch):
    """The housekeeping backstop fires only the lanes the lockdown admits — never a held one.

    The sweep reads one profile's cron store, so one lane decision covers the whole sweep.
    """
    from cron import scheduler_provider, scheduler_tick

    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
                  allow={"profiles": ["default"]})
    monkeypatch.setattr(scheduler_tick, "_tick_lane", lambda: "yoyoflow")

    class _Provider:
        pass

    assert scheduler_provider.fire_overdue_jobs(_Provider()) == 0

    monkeypatch.setattr(scheduler_tick, "_tick_lane", lambda: "default")
    # An admitted lane reaches the sweep body; the point of this test is that it is no longer
    # refused up front, so the call must get past the lane gate without raising.
    scheduler_provider.fire_overdue_jobs(_Provider())
