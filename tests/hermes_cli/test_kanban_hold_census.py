"""The held-card census behind ``hermes status`` / ``hermes doctor`` (v3 §6.9).

Visibility is the requirement the lane-scoped gate is judged on: an operator must be able to
read "the stop is holding N cards, on THESE boards, for THESE lanes" from the two surfaces
they already run — without opening a board, and without a count that quietly skips a board it
could not read.

The counts here come from REAL dispatch ticks writing the same ``skipped_lockdown`` task
events the dispatcher writes in production: a census test built on hand-inserted rows would
pass against a reader that disagrees with the writer about the dedupe key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import estop
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_holds as kh


@pytest.fixture(autouse=True)
def operator_context(monkeypatch):
    """This suite arms holds, so it must not run inside a dispatched worker's inherited env."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME carrying a kanban DB, with the fleet root out of the sentinel path."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nobody")
    estop._logged_components.clear()
    kb.init_db()
    return home


def _arm_lockdown(profiles) -> str:
    return estop.acquire(
        owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
        allow={"profiles": list(profiles)})


def _spawn_recorder(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    return fake_spawn


def _hold(assignee: str, *, board: str | None = None) -> str:
    """Create one held card on ``board`` and let a real tick record the refusal."""
    with kbc.connect(board=board) as conn:
        task_id = kb.create_task(conn, title=f"held for {assignee}", assignee=assignee)
        res = kbd.dispatch_once(conn, board=board, spawn_fn=_spawn_recorder([]))
    assert (task_id, assignee) in res.skipped_lockdown, res.skipped_lockdown
    return task_id


# ── the census reads the boards' own events, under the live engagement ──────


def test_a_board_that_never_dispatched_holds_nothing(kanban_home, tmp_path):
    """No store on disk = nothing has ever been refused there: 0, not "unreadable"."""
    held = kh.census(engagement="whatever", paths=[("ghost", tmp_path / "ghost" / "kanban.db")])

    assert held.total == 0
    assert held.unreadable == ()
    assert kh.clause(held) == "held cards: none"


def test_the_census_counts_the_newest_event_per_card_on_every_board(
    kanban_home, all_assignees_spawnable,
):
    kb.create_board("ops")
    _arm_lockdown(["default"])

    _hold("yoyoflow")
    _hold("yoyoflow")
    _hold("platform-worker")
    _hold("platform-worker", board="ops")

    held = kh.census(engagement=estop.engagement_key())

    assert [(b.board, b.cards) for b in held.boards] == [("default", 3), ("ops", 1)], held.boards
    assert held.total == 4
    assert held.lanes() == {"yoyoflow": 2, "platform-worker": 2}
    assert kh.clause(held) == (
        "held cards: 4 (default: 3, ops: 1) — held lanes: platform-worker x2, yoyoflow x2"
    )


def test_a_re_arm_makes_the_previous_engagement_stale(kanban_home, all_assignees_spawnable):
    """The count answers for THIS stop: a lifted-and-re-armed hold does not inherit the old.

    The task events ARE the record of refusal, so a re-armed stop counts nothing until its own
    tick re-records the cards it refuses; what it must never do is report the previous
    engagement's rows as if the new stop had starved them.
    """
    handle = _arm_lockdown(["default"])
    _hold("yoyoflow")
    first = estop.engagement_key()
    assert kh.census(engagement=first).total == 1

    estop.release(handle=handle)
    _arm_lockdown(["default"])
    second = estop.engagement_key()
    assert second != first, "a re-arm is a new engagement"
    assert kh.census(engagement=first).total == 1, "…the old rows are still the old engagement's"
    assert kh.census(engagement=second).total == 0, "an old engagement's events are not the live hold"

    # A tick under the new engagement re-records the cards it refuses — both of them, the one
    # that was already parked included — so the census counts them again.
    _hold("yoyoflow")
    assert kh.census(engagement=second).total == 2


def test_an_unreadable_board_is_named_never_raised(kanban_home, all_assignees_spawnable):
    """A status line survives a board it cannot read, and does not call it empty."""
    _arm_lockdown(["default"])
    _hold("yoyoflow")
    kb.create_board("junk")
    (kb.board_dir("junk") / "kanban.db").write_bytes(b"not a sqlite database")

    held = kh.census(engagement=estop.engagement_key())

    assert held.unreadable == ("junk",), held.boards
    assert held.total == 1, "the readable boards are still counted"
    assert kh.clause(held) == (
        "held cards: 1 (default: 1, junk: unreadable) — held lanes: yoyoflow x1"
    )


def test_a_total_hold_names_every_lane_held_instead_of_a_lane_list(
    kanban_home, all_assignees_spawnable,
):
    """One lane list under a TOTAL halt would read as the scoped stop this line rules out."""
    estop.acquire(owner="operator", mode=estop.MODE_ESTOP, reason="total halt")
    _hold("yoyoflow")

    held = kh.census(engagement=estop.engagement_key())
    scoped = kh.clause(held)
    total = kh.clause(held, total_hold=True)

    assert scoped.endswith("— held lanes: yoyoflow x1"), scoped
    assert total == "held cards: 1 (default: 1) — total halt, every lane held", total


# ── the two surfaces that render it ─────────────────────────────────────────


def test_hermes_status_names_the_held_cards_with_the_board(kanban_home, all_assignees_spawnable):
    from hermes_cli.status import _estop_status_line

    kb.create_board("ops")
    _arm_lockdown(["default"])
    _hold("yoyoflow")
    _hold("platform-worker", board="ops")

    line = _estop_status_line()
    assert line is not None, "an armed stop always renders the banner"

    assert "lockdown, lanes admitted: default" in line
    assert "held cards: 2 (default: 1, ops: 1)" in line
    assert "held lanes: platform-worker x1, yoyoflow x1" in line


def test_hermes_status_says_none_when_the_stop_holds_nothing(kanban_home):
    """An armed stop that has refused nothing yet says so — it does not omit the clause."""
    from hermes_cli.status import _estop_status_line

    _arm_lockdown(["default"])

    line = _estop_status_line()
    assert line is not None
    assert "held cards: none" in line


def test_hermes_status_says_total_halt_beside_the_count(kanban_home, all_assignees_spawnable):
    from hermes_cli.status import _estop_status_line

    estop.acquire(owner="operator", mode=estop.MODE_ESTOP, reason="total halt")
    _hold("yoyoflow")

    line = _estop_status_line()
    assert line is not None

    assert "TOTAL halt, no exemptions" in line
    assert "held cards: 1 (default: 1) — total halt, every lane held" in line
    assert "held lanes:" not in line, "a total halt must not name lanes as if they were singled out"


def test_the_doctor_row_renders_clear_when_nothing_holds(kanban_home, capsys):
    from hermes_cli.doctor_estop import _check_emergency_stop

    finding = _check_emergency_stop(False)
    out = capsys.readouterr().out

    assert "Emergency stop" in out and "clear" in out
    assert finding.issues == [] and finding.manual_issues == [], "a stop is not a fault to fix"


def test_the_doctor_row_renders_the_hold_and_the_held_cards(
    kanban_home, all_assignees_spawnable, capsys,
):
    from hermes_cli.doctor_estop import _check_emergency_stop

    _arm_lockdown(["default"])
    _hold("yoyoflow")

    finding = _check_emergency_stop(False)
    out = capsys.readouterr().out

    assert "lockdown, lanes admitted: default" in out, out
    assert "held cards: 1 (default: 1)" in out, out
    assert finding.issues == [] and finding.manual_issues == []


def test_the_doctor_row_is_registered_as_its_own_section():
    """The row has to be reachable from `hermes doctor`, not just importable."""
    from hermes_cli import doctor

    titles = [title for title, _check in doctor.DOCTOR_CHECKS]
    assert "Emergency Stop" in titles
