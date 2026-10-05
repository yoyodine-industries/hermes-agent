"""The STANDING PLATFORM FLOOR under a TOTAL estop (card t_0d2f9599).

Operator order 2026-10-01: *"Critical runs that don't impact the driver of the estop/lockdown
should always run, and in this lockdown/estop we need platform agents/bots and boards
enabled."*

A TOTAL ``estop`` used to be a fleet-wide blackout: ``work_admitted`` returned False for EVERY
lane, the cron tick returned before it even scanned, and the kanban dispatcher refused every
board. These tests pin the floor that replaces it:

* the four platform lanes are admitted under a total hold, and nobody else is;
* the cron tick and the kanban dispatcher RUN under a total hold and refuse per item by LANE
  (never a wholesale halt), so a critical job for a floor lane still fires;
* the panic button's own ``allow`` list grants nothing beyond the floor;
* ``check_paused`` keeps its old whole-component meaning; ``halt_entirely`` is the sibling the
  dispatch seams call, and it halts only a component with no floor work;
* a scoped ``lockdown`` behaves exactly as before.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agent import estop

FLOOR = ("default", "platform-stl", "platform-coder", "platform-worker")
OFF_FLOOR = (
    "research-stl",
    "research-coder",
    "financially-stl",
    "financially-coder",
    "yoyoflow",
    "engines-coder",
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
    estop._logged_components.clear()
    estop._lockdown_logged.clear()
    estop._floor_logged.clear()
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
    monkeypatch.setattr(scheduler_tick, "_LOCKDOWN_REPORTED", {})
    return seen


def _arm_total() -> estop.EstopState:
    estop.acquire(owner="operator", mode=estop.MODE_ESTOP, reason="total halt")
    state = estop.read_state()
    assert state.total is True and state.engaged is True
    return state


# ── the floor itself ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("lane", FLOOR)
def test_the_floor_lanes_are_admitted_under_a_total_hold(hermes_home, lane):
    state = _arm_total()
    assert estop.work_admitted(lane, board="defcon", state=state) is True


@pytest.mark.parametrize("lane", OFF_FLOOR)
def test_every_other_lane_is_held_under_a_total_hold(hermes_home, lane):
    state = _arm_total()
    assert estop.work_admitted(lane, board="defcon", state=state) is False


def test_the_floor_is_normalised_case_and_space(hermes_home):
    state = _arm_total()
    assert estop.work_admitted(" Platform-Coder ", state=state) is True
    assert estop.work_admitted("platform-stll", state=state) is False, "the near-miss typo fails closed"
    assert estop.work_admitted(None, state=state) is False, "an unassigned card has no lane"
    assert estop.work_admitted("", state=state) is False


def test_the_total_holds_own_allowlist_grants_nothing_beyond_the_floor(hermes_home):
    """The panic button must not be widenable by whoever arms it."""
    estop.acquire(owner="operator", mode=estop.MODE_ESTOP, reason="total",
                  allow={"profiles": ["research-stl", "financially-coder"]})
    state = estop.read_state()
    assert state.total is True
    assert estop.work_admitted("research-stl", state=state) is False
    assert estop.work_admitted("financially-coder", state=state) is False
    for lane in FLOOR:
        assert estop.work_admitted(lane, state=state) is True


# ── check_paused vs halt_entirely ────────────────────────────────────────────


def test_halt_entirely_halts_only_components_without_floor_work(hermes_home):
    _arm_total()
    for component in ("cron", "cron-misfire", "kanban"):
        assert estop.halt_entirely(component, estop.logger) is False, component
    for component in ("gateway-turn", "turn-start", "knows-nothing-of-the-floor"):
        assert estop.halt_entirely(component, estop.logger) is True, component
    # A caller's spelling cannot silently opt a component out of the floor.
    assert estop.halt_entirely("CRON", estop.logger) is False


def test_check_paused_keeps_its_whole_component_meaning(hermes_home):
    """Unchanged API: a total hold still stops a whole component; the seams use the sibling."""
    _arm_total()
    assert estop.check_paused("cron", estop.logger) is True
    assert estop.check_paused("kanban", estop.logger) is True


def test_halt_entirely_is_false_for_every_non_total_state(hermes_home):
    assert estop.halt_entirely("gateway-turn", estop.logger) is False, "clear"
    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
                  allow={"profiles": ["default"]})
    assert estop.halt_entirely("gateway-turn", estop.logger) is False, "lockdown is lane-scoped"
    assert estop.halt_entirely("cron", estop.logger) is False


# ── the cron tick runs, scoped by lane ───────────────────────────────────────


def test_a_total_hold_runs_the_cron_tick_and_holds_a_non_floor_lane(
    hermes_home, tick_harness, monkeypatch, caplog,
):
    from cron import scheduler, scheduler_tick

    _arm_total()
    monkeypatch.setattr(scheduler_tick, "_tick_lane", lambda: "research-stl")

    with caplog.at_level(logging.WARNING):
        assert scheduler.tick(verbose=False) == 0

    assert tick_harness["scans"] == 1, "the tick runs under a total hold"
    assert tick_harness["dispatched"] == [], "the held lane's job never reaches the executor"
    assert tick_harness["advanced"] == [], "and it stays due, to fire after the lift"
    assert any(
        "held by the TOTAL emergency stop" in r.getMessage()
        and "j-1" in r.getMessage()
        and "research-stl" in r.getMessage()
        for r in caplog.records
    ), "the deferral is RECORDED, naming the job and the profile"


def test_a_total_hold_fires_a_floor_lanes_job(hermes_home, tick_harness, monkeypatch):
    """The point of the floor: a platform lane's due job still fires — the disk-hygiene /
    maintenance jobs the DEFCON hold most needs keep running."""
    from cron import scheduler, scheduler_tick

    _arm_total()
    monkeypatch.setattr(scheduler_tick, "_tick_lane", lambda: "platform-coder")

    scheduler.tick(verbose=False)

    assert tick_harness["scans"] == 1
    assert tick_harness["dispatched"] == ["j-1"]
    assert tick_harness["advanced"] == ["j-1"]


def test_the_misfire_sweep_is_scoped_under_a_total_hold(hermes_home, monkeypatch, caplog):
    """The housekeeping backstop carries the floor too: it RUNS under a total hold and refuses
    a non-floor lane rather than skipping the sweep wholesale."""
    from cron import scheduler_provider, scheduler_tick

    _arm_total()

    class _Provider:
        pass

    monkeypatch.setattr(scheduler_tick, "_tick_lane", lambda: "research-stl")
    with caplog.at_level(logging.WARNING):
        assert scheduler_provider.fire_overdue_jobs(_Provider()) == 0
    assert any(
        "Misfire sweep held by the TOTAL emergency stop" in r.getMessage()
        and "research-stl" in r.getMessage()
        for r in caplog.records
    )

    # A floor lane reaches the sweep body instead of being refused up front.
    monkeypatch.setattr(scheduler_tick, "_tick_lane", lambda: "platform-coder")
    scheduler_provider.fire_overdue_jobs(_Provider())


# ── the kanban dispatch seam runs, scoped by lane ────────────────────────────


def test_a_total_hold_keeps_the_kanban_dispatch_seam_open(hermes_home):
    """The dispatcher seam must not short-circuit: it RUNS and the per-card lane gate refuses.
    Only a component with no floor work halts entirely."""
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed

    assert _kanban_dispatch_allowed() is True
    _arm_total()
    assert _kanban_dispatch_allowed() is True


# ── lockdown is untouched ────────────────────────────────────────────────────


def test_a_scoped_lockdown_behaves_exactly_as_before(hermes_home, tick_harness, monkeypatch):
    from cron import scheduler, scheduler_tick

    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
                  allow={"profiles": ["default"]})
    state = estop.read_state()
    assert state.total is False
    assert estop.work_admitted("default", state=state) is True
    assert estop.work_admitted("platform-stl", state=state) is False, "the allowlist still bites"

    monkeypatch.setattr(scheduler_tick, "_tick_lane", lambda: "default")
    scheduler.tick(verbose=False)
    assert tick_harness["dispatched"] == ["j-1"]
