"""Lane-scoped DEFCON lockdown — the hold registry, the admission predicate, the arming CLI.

Design: platform-stl ``LOCKDOWN_MODE_DESIGN.md`` rev 2 with the rev 3 amendment of 2026-09-27.

The rule under test: the stop is keyed on the LANE (the profile), NEVER on the board. The
prior form granted throughput BY BOARD — it would have run any card that sat on the exempt
board, whatever lane the card needed — which is why every admission assertion below is made
twice, once per board. The dispatch-side regressions (a starving lane is refused on the ops
board too, an allowlisted lane spawns on both) live in
``tests/hermes_cli/test_kanban_dispatch_lockdown.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agent import estop


@pytest.fixture(autouse=True)
def operator_context(monkeypatch):
    """Run every test in the OPERATOR's context.

    A dispatched kanban worker inherits ``HERMES_KANBAN_TASK``, and this suite is often
    executed from inside one (a worker running pytest). Arming refuses that context by
    design — the lane being held must not be the thing that arms or lifts its own hold — so
    the refusal is cleared here and asserted explicitly in its own test.

    The ACTOR is declared too (holder attribution): a hold is recorded under the name its
    actor is entitled to wear, and the operator's own session is the only venue entitled to
    the name ``operator`` — which is what lets these tests speak for named holders.
    """
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "operator")
    monkeypatch.setattr(estop, "_stdin_is_tty", lambda: True)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME, with the fleet root kept out of the candidate path list."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nobody")
    estop._logged_components.clear()
    return home


# ── the registry: several holds, independent lifetime ───────────────────────


def test_two_holds_of_different_modes_coexist_and_release_independently(hermes_home):
    total = estop.acquire(owner="operator", mode=estop.MODE_ESTOP, reason="runaway cron fan-out")
    scoped = estop.acquire(
        owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops update window",
        allow={"profiles": ["default", "platform-stl"]})

    state = estop.read_state()
    assert state.total is True, "one estop hold among lockdown holds is still a TOTAL halt"
    assert state.counts[estop.MODE_ESTOP] == 1 and state.counts[estop.MODE_LOCKDOWN] == 1
    assert set(state.owners) == {"operator", "ops-head"}

    first = estop.release(handle=scoped)
    assert first.released is True and first.cleared is False
    state = estop.read_state()
    assert state.counts[estop.MODE_LOCKDOWN] == 0 and state.total is True
    assert "ops-head" not in state.owners and "operator" in state.owners

    second = estop.release(handle=total)
    assert second.released is True and second.cleared is True
    assert estop.is_engaged() is False


def test_release_by_owner_leaves_a_co_holders_entry_untouched(hermes_home):
    """The 02:09:52 defect: one holder's release/admin write erasing another's scope."""
    other = estop.acquire(
        owner="yoyoflow:critical-section", mode=estop.MODE_LOCKDOWN, reason="train window",
        allow={"profiles": ["platform-stl"]})
    estop.engage(reason="operator stop")

    result = estop.release()  # the operator's own hold
    assert result.released is True
    assert result.cleared is False
    assert result.remaining_owners == ["yoyoflow:critical-section"]

    state = estop.read_state()
    assert [hold["handle"] for hold in state.holds] == [other]
    assert estop.work_admitted("yoyoflow:critical-section", state=state) is False, "lane key, not owner"


def test_expiry_of_one_hold_leaves_the_other_intact(hermes_home):
    estop.acquire(owner="window-job", mode=estop.MODE_LOCKDOWN, ttl="1s",
                  expires_at="2000-01-01T00:00:00+00:00", allow={"profiles": ["default"]})
    live = estop.acquire(owner="operator", mode=estop.MODE_ESTOP, reason="operator stop")

    state = estop.read_state()
    assert [hold["owner"] for hold in state.expired] == ["window-job"]
    assert state.total is True
    assert estop.is_engaged() is True, "a deadman lifted ITS hold, not the fleet"

    assert estop.release(handle=live).released is True
    assert estop.is_engaged() is False


def test_a_stale_handle_is_refused_by_name_and_releases_nothing(hermes_home):
    handle = estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN,
                           allow={"profiles": ["default"]})
    assert estop.release(handle=handle).released is True

    again = estop.release(handle=handle)
    assert again.released is False and again.stale is True
    assert handle in again.message, "a stale release must be refused BY NAME, never silently"


def test_engage_replaces_only_the_same_owners_previous_hold(hermes_home):
    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, allow={"profiles": ["default"]})
    estop.engage(reason="operator stop")   # owner=operator
    estop.engage(reason="operator stop again")

    state = estop.read_state()
    assert state.counts[estop.MODE_ESTOP] == 1, "a repeated operator pause stays ONE hold"
    assert state.counts[estop.MODE_LOCKDOWN] == 1, "and does not disturb the co-holder"


# ── fail-safe reads ─────────────────────────────────────────────────────────


def test_an_unreadable_body_reads_as_a_total_halt_with_the_defect_named(hermes_home):
    (hermes_home / "ESTOP").write_text("{not json", encoding="utf-8")

    state = estop.read_state()
    assert state.engaged is True and state.total is True
    assert state.defect == estop.DEFECT_UNREADABLE
    # A defective body reads as a TOTAL halt. The standing platform floor still applies (the
    # total flag governs); a lane outside the floor is held.
    assert estop.work_admitted("yoyoflow", board="defcon", state=state) is False
    assert estop.work_admitted("default", board="defcon", state=state) is True
    assert estop.is_engaged() is True


def test_a_stat_error_is_fail_safe_engaged_and_total(hermes_home, monkeypatch):
    target = hermes_home / "ESTOP"
    target.write_text("{}", encoding="utf-8")
    real_exists = Path.exists

    def _exists(self):
        if self == target:
            raise OSError("stat failed")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _exists)
    state = estop.read_state()
    assert state.engaged is True, "a sentinel we cannot stat is still a stop"
    assert state.total is True and state.defect == estop.DEFECT_STAT_ERROR


def test_the_body_keeps_the_pre_registry_top_level_fields(hermes_home):
    """Legacy readers (and the shipped body contract) still see the pause they expect."""
    estop.engage(reason="update window", ttl="45m", allow={"user_ids": ["u1"]})

    raw = json.loads((hermes_home / "ESTOP").read_text(encoding="utf-8"))
    assert raw["holds"], "the registry is the real body"
    assert raw["reason"] == "update window"
    assert raw["expires_at"] and raw["mode"] == estop.MODE_ESTOP
    assert raw["allow"]["user_ids"] == ["u1"]


# ── the admission predicate: LANE keyed, board blind ────────────────────────


def test_the_lane_decides_and_no_board_placement_ever_does(hermes_home):
    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
                  allow={"profiles": ["default", "platform-coder"]})
    state = estop.read_state()

    for board in (None, "defcon", "ops", "some-other-board"):
        assert estop.work_admitted("platform-coder", board=board, state=state) is True
        assert estop.work_admitted("yoyoflow", board=board, state=state) is False

    assert estop.work_admitted("platform-coder", state=state) is True, "case/space normalised"
    assert estop.work_admitted(" Platform-Coder ", state=state) is True
    assert estop.work_admitted("platform-stll", state=state) is False, "the 03:3x typo fails closed"
    assert estop.work_admitted(None, state=state) is False, "an unassigned card has no lane"
    assert estop.work_admitted("", state=state) is False


def test_a_total_hold_admits_only_the_standing_platform_floor(hermes_home):
    """A total hold is the panic button: its own ``allow`` list grants nothing beyond the
    standing platform floor, so whoever arms it cannot widen the stop — and a lane OUTSIDE
    the floor is held however it is named."""
    estop.acquire(owner="operator", mode=estop.MODE_ESTOP, reason="total",
                  allow={"profiles": ["research-stl", "financially-coder"]})
    state = estop.read_state()
    assert state.total is True

    # Named in the hold's own allow list, but NOT on the floor — still held.
    assert estop.work_admitted("research-stl", board="defcon", state=state) is False
    assert estop.work_admitted("financially-coder", state=state) is False

    # The floor is admitted whatever the hold's allow list says.
    for lane in sorted(estop.STANDING_ADMITTED_LANES):
        assert estop.work_admitted(lane, state=state) is True, lane
    # Case/space normalisation still applies on the total path.
    assert estop.work_admitted(" Platform-Coder ", state=state) is True
    assert estop.work_admitted("platform-stll", state=state) is False, "the 03:3x typo fails closed"
    assert estop.work_admitted(None, state=state) is False, "an unassigned card has no lane"
    assert estop.work_admitted("", state=state) is False


def test_lockdown_naming_no_lane_holds_every_lane_without_becoming_total(hermes_home):
    """An empty lockdown is NOT a resume and is NOT the total panic button either: the tick
    keeps running and every lane is refused (fail closed, and visible)."""
    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="allowlist lost", allow={})
    state = estop.read_state()

    assert state.mode == estop.MODE_LOCKDOWN and state.total is False
    assert estop.work_admitted("default", state=state) is False
    assert estop.work_admitted("platform-stl", board="ops", state=state) is False


def test_nothing_holds_means_every_lane_is_admitted(hermes_home):
    state = estop.read_state()
    assert state.engaged is False and state.total is False
    assert estop.work_admitted("yoyoflow", board="ops", state=state) is True
    assert estop.engagement_key()


# ── arming: mode, flags, validation, and who may arm ────────────────────────


def test_cli_pause_lockdown_requires_a_lane(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    rc = cmd_pause(argparse.Namespace(
        reason=None, lockdown=True, allow_profile=None, allow_user=None, ttl=None))
    assert rc == 2
    assert estop.is_engaged() is False, "a refused lockdown must not arm anything"
    out = capsys.readouterr().out
    assert "default" in out, "the refusal names the recommended lanes"


def test_cli_pause_lockdown_refuses_an_unknown_lane_by_name(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    rc = cmd_pause(argparse.Namespace(
        reason=None, lockdown=True, allow_profile=["platform-stll"], allow_user=None, ttl=None))
    assert rc == 2
    assert estop.is_engaged() is False, "a typo must not arm a stop that holds that lane"
    assert "platform-stll" in capsys.readouterr().out


def test_cli_pause_lockdown_arms_the_named_lanes(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    rc = cmd_pause(argparse.Namespace(
        reason="ops window", lockdown=True, allow_profile=["default"], allow_user=None, ttl=None))
    assert rc == 0

    state = estop.read_state()
    assert state.mode == estop.MODE_LOCKDOWN and state.total is False
    assert estop.work_admitted("default", board="ops", state=state) is True
    assert estop.work_admitted("yoyoflow", board="defcon", state=state) is False
    assert estop.check_paused("cron", estop.logger) is False, "a lockdown is not a cron halt"

    out = capsys.readouterr().out
    assert "board is NOT a key" in out
    assert "scope" in out


def test_cli_pause_without_lockdown_is_still_a_total_halt(hermes_home):
    from hermes_cli.subcommands.pause import cmd_pause

    assert cmd_pause(argparse.Namespace(
        reason=None, lockdown=False, allow_profile=None, allow_user=None, ttl=None)) == 0

    state = estop.read_state()
    assert state.total is True and state.counts[estop.MODE_ESTOP] == 1
    # The standing platform floor survives the panic button; every other lane is held.
    assert estop.work_admitted("default", board="defcon", state=state) is True
    assert estop.work_admitted("research-stl", board="defcon", state=state) is False
    # check_paused keeps its old whole-component meaning; the lane-aware sibling is what the
    # dispatch seams call, and it lets the floor-bearing components RUN.
    assert estop.check_paused("cron", estop.logger) is True
    assert estop.halt_entirely("cron", estop.logger) is False
    assert estop.halt_entirely("gateway-turn", estop.logger) is True


def test_cli_refuses_to_arm_or_lift_from_a_dispatched_worker(hermes_home, monkeypatch, capsys):
    """The allowlist is operator/ops-head owned: the lane a hold is starving — or the worker
    whose card it is starving — can never arm it or release it for itself."""
    from hermes_cli.subcommands.pause import cmd_pause, cmd_resume

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")

    rc = cmd_pause(argparse.Namespace(
        reason=None, lockdown=False, allow_profile=None, allow_user=None, ttl=None))
    assert rc == 3 and estop.is_engaged() is False
    assert "kanban task t_deadbeef" in capsys.readouterr().out

    assert cmd_resume(argparse.Namespace()) == 3
    assert estop.is_engaged() is False


def test_cli_resume_reports_a_co_holders_remaining_scope(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_resume

    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
                  allow={"profiles": ["default"]})
    estop.engage(reason="operator stop")

    assert cmd_resume(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "STILL HELD by ops-head" in out
    assert "NOT resumed" in out
    assert estop.work_admitted("platform-stl", state=estop.read_state()) is False


def test_cli_resume_refuses_a_stale_handle(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_resume

    handle = estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN,
                           allow={"profiles": ["default"]})
    assert cmd_resume(argparse.Namespace(handle=handle)) == 0
    estop.engage(reason="operator stop")   # a co-holder arms again

    assert cmd_resume(argparse.Namespace(handle=handle)) == 2
    assert handle in capsys.readouterr().out
    assert estop.is_engaged() is True, "a refused release must leave the live hold in force"


def test_status_line_renders_the_lockdown_scope(hermes_home):
    from hermes_cli.status import _estop_status_line

    estop.acquire(owner="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window", ttl="45m",
                  allow={"profiles": ["default", "platform-stl"], "user_ids": ["u1"]})
    line = _estop_status_line()

    assert "lockdown" in line
    assert "default" in line and "platform-stl" in line
    assert "ops window" in line and "u1" in line and "deadman" in line
    assert "ops-head" in line
