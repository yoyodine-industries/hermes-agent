"""A zero-spawn tick names WHY it spawned nothing (#111910).

Two arms, both from the live incident of 2026-09-25: a tick held back by a
legitimate CAPACITY limit (host ``kanban.max_in_progress``, board
``kanban.max_spawn``, per-profile cap) and a tick held back by a FAULT someone
has to act on.

The capacity arm used to record nothing at all: the host cap is checked before
the lane loop, so no per-task bucket could be filled, and the "dispatcher
stuck" warning printed a bare zero-spawn count for a fleet that was simply full
(324 ready cards behind 6 workers). Six alarms fired over that fleet, each
advising the reader to check profile health while the workers they should have
been checking were visibly running.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _running(conn, title):
    tid = kb.create_task(conn, title=title, assignee="worker")
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock='w', claim_expires=? WHERE id=?",
        (int(time.time()) + 3600, tid),
    )
    conn.commit()
    return tid


class TestCapacityHoldIsRecorded:
    """The refused-before-the-lane-loop cases must land somewhere on the result."""

    def test_host_cap_refusal_is_named_on_the_result(self, conn):
        _running(conn, "busy")
        res = kbd.DispatchResult()

        may_spawn, budget = kbd._tick_spawn_budget(
            conn, res, max_spawn=None, max_in_progress=1, board="default"
        )

        assert (may_spawn, budget) == (False, None)
        assert res.spawned == []
        assert res.capacity_hold == "host_max_in_progress"

    def test_board_cap_refusal_is_named_on_the_result(self, conn):
        _running(conn, "busy")
        res = kbd.DispatchResult()

        may_spawn, budget = kbd._tick_spawn_budget(
            conn, res, max_spawn=1, max_in_progress=None, board="default"
        )

        assert (may_spawn, budget) == (False, None)
        assert res.capacity_hold == "board_max_spawn"

    def test_a_tick_with_budget_is_not_capacity_held(self, conn):
        res = kbd.DispatchResult()

        may_spawn, budget = kbd._tick_spawn_budget(
            conn, res, max_spawn=None, max_in_progress=None, board="default"
        )

        assert may_spawn is True
        assert res.capacity_hold is None


class TestTheWarningNamesTheHold:
    def test_capacity_arm_reads_as_at_capacity(self, conn):
        _running(conn, "busy")
        res = kbd.DispatchResult()
        kbd._tick_spawn_budget(conn, res, max_spawn=None, max_in_progress=1, board="default")

        assert kbd.describe_suppression([res]) == "at_capacity=host_max_in_progress"

        hold = kbd.capacity_hold_reason([res])
        assert hold == "host_max_in_progress"
        remedy = kbd.stuck_warning_remedy(hold)
        assert "profile health" not in remedy
        assert "max_in_progress" in remedy

    def test_fault_arm_keeps_the_profile_health_advice(self):
        res = kbd.DispatchResult(respawn_guarded=[("t1", "recent_success")])

        assert kbd.describe_suppression([res]) == "recent_success=1"

        assert kbd.capacity_hold_reason([res]) == ""
        remedy = kbd.stuck_warning_remedy("")
        assert remedy.startswith("Check profile health (venv, PATH, credentials)")
        assert "kanban list --status ready" in remedy

    def test_per_profile_cap_alone_is_capacity(self):
        res = kbd.DispatchResult(skipped_per_profile_capped=[("t1", "worker", 6)])

        assert kbd.capacity_hold_reason([res]) == "max_in_progress_per_profile"
        assert "per_profile_capped=1" in kbd.describe_suppression([res])

    def test_memory_pressure_is_a_fault_not_capacity(self):
        res = kbd.DispatchResult(memory_pressure="critical")

        assert kbd.capacity_hold_reason([res]) == ""
        assert kbd.describe_suppression([res]) == "memory_pressure=critical"

    def test_capacity_cannot_mask_a_fault_in_the_same_tick(self):
        res = kbd.DispatchResult(
            capacity_hold="host_max_in_progress", rate_limited=["t1"]
        )

        assert kbd.capacity_hold_reason([res]) == ""
        described = kbd.describe_suppression([res])
        assert "rate_limited=1" in described
        assert "at_capacity=host_max_in_progress" in described

    def test_unassigned_and_nonspawnable_are_named(self):
        res = kbd.DispatchResult(
            skipped_unassigned=["t1"], skipped_nonspawnable=["t2", "t3"]
        )

        described = kbd.describe_suppression([res])
        assert "unassigned=1" in described
        assert "nonspawnable=2" in described
