"""The lane-scoped lockdown at the CRON seam.

A lockdown is not a cron halt: the tick keeps running and each due job is admitted or held by
its OWN lane (the profile whose store this tick is serving). A held job must not be advanced —
leaving it due is what makes it fire on the first tick after the lift instead of losing its
period — and the deferral must be visible, naming the job and its profile.

STATE OF THE SEAM (measured 2026-10-11, card `t_babd2b2e`)
    The engine side is in place: ``agent.estop.work_admitted`` is the lane admission, and
    ``check_paused`` returns False under a scoped lockdown on purpose — "the tick RUNS ...
    the per-card / per-job lane gate holds everything else, which is what keeps a scoped stop
    from looking like an idle board".

    At the CRON seam that per-job gate is NOT implemented on ANY line: ``cron/`` carries no
    ``_tick_lane`` and no ``_LOCKDOWN_REPORTED``, and its only estop call is the GLOBAL
    ``check_paused("cron", ...)`` (``cron/scheduler_tick.py``), so a held lane's due jobs are
    dispatched too. What holds TODAY is asserted green below; the per-lane expectations carry
    ``xfail(strict=True)`` naming card `t_1da2450d`, the design card for the missing seam.
    ``strict=True`` means the moment the seam lands the test XPASSes and this file turns RED
    until the marker is deleted, so the marker cannot rot into a permanent excuse.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent import estop

# The seam this file specifies does not exist yet: the tick carries no lane gate, so a held
# lane's due job is still dispatched. Every marker names card `t_1da2450d`, and is strict.
_MISSING_SEAM = (
    "the cron tick carries no lane-scoped hold gate (no `_tick_lane`, no `_LOCKDOWN_REPORTED`), "
    "so a held lane's due job is still dispatched — card t_1da2450d"
)


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
    # Reset the engine's per-engagement log state. There is deliberately NO tick-side state to
    # reset here: the seam's own `_LOCKDOWN_REPORTED` does not exist (see the module docstring),
    # and referencing it is what made this file uncollectable before card t_babd2b2e.
    estop._logged_components.clear()
    estop._lockdown_logged.clear()
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


def _legacy_total(reason: str = "total halt") -> str:
    """Set up the PRE-RULING whole-body sentinel by hand, and return its handle.

    Arming ``mode: estop`` is not a supported operation any more (operator ruling 2026-10-10,
    card `t_e04b4c81`), so NO verb may re-create it — including a test. The gate still has to
    behave on one, because a total hold can be sitting on disk from older bytes and the boundary
    statement calls it an alarm to LIFT, so the state is written the way a pre-ruling install
    left it (a whole-body sentinel, no registry, no provenance) — exactly as
    ``tests/cron/test_estop.py::_legacy_total`` does.
    """
    body = {"reason": reason, "engaged_at": datetime.now(UTC).isoformat()}
    estop.sentinel_path().write_text(json.dumps(body) + "\n", encoding="utf-8")
    held = [hold for hold in estop.holds() if (hold.get("reason") or "") == reason]
    return held[0]["handle"] if held else ""


def test_a_lockdown_does_not_halt_the_tick(hermes_home, tick_harness):
    """Clause 1 at the cron seam, as the engine specifies it: a SCOPED hold is not a halt."""
    from cron import scheduler

    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
                  allow={"profiles": ["default"]})
    scheduler.tick(verbose=False)
    assert tick_harness["scans"] == 1, "a lockdown is not a halt: the tick runs"


def test_a_legacy_total_hold_still_halts_the_tick(hermes_home, tick_harness):
    """A pre-ruling total body is an ALARM, not a control — but the gate still stops on it."""
    from cron import scheduler

    assert _legacy_total("total halt"), "the legacy body must read as a hold"
    assert scheduler.tick(verbose=False) == 0
    assert tick_harness["scans"] == 0, "a total halt never even scans for due jobs"


@pytest.mark.xfail(strict=True, reason=_MISSING_SEAM)
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


@pytest.mark.xfail(strict=True, reason=_MISSING_SEAM)
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


@pytest.mark.xfail(strict=True, reason=_MISSING_SEAM)
def test_an_allowlisted_lanes_job_still_fires(hermes_home, tick_harness, monkeypatch):
    from cron import scheduler, scheduler_tick

    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
                  allow={"profiles": ["default"]})
    monkeypatch.setattr(scheduler_tick, "_tick_lane", lambda: "default")

    scheduler.tick(verbose=False)

    assert tick_harness["dispatched"] == ["j-1"]
    assert tick_harness["advanced"] == ["j-1"]


@pytest.mark.xfail(strict=True, reason=_MISSING_SEAM)
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
