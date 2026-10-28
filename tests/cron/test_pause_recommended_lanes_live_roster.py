"""`hermes pause` may only ADVISE a lane that exists, and only FILE to a lane that resolves.

Card t_255cc087. Two retired handles were hard-coded in ``hermes_cli/subcommands/pause.py``:

* ``RECOMMENDED_LANES`` carried ``platform-worker`` — the lane the fleet RENAMED to ``default``,
  which the tuple already named. The refusal PRINTED it as advice ("recommended: …"), and the
  arm path's own lane gate then refused it (``_profile_is_real`` -> "Unknown profile(s) in
  --allow-profile … refusing to arm"): following the recommendation broke the pause it
  recommended. ``recommended_lanes()`` projects the set onto the LIVE roster.
* ``OPS_ALERT_ASSIGNEE`` carried ``yoyodine-majordomo``, a retired DISPLAY handle. The D9
  re-arm alert could not be FILED at all — ``kanban_db.create_task`` -> ``gate_assignee`` fails
  CLOSED on a handle that resolves nowhere (that refusal is the fix for the silent form; these
  two sites were the remaining hard-coded instances). It now names the live ops-head seat,
  ``default``, the lane that owns host administration.

SHAPE: **pin by member, keyed by the real list.** Every assertion below ITERATES the constants'
members and keys the judge on a list this module does not own — the kernel's
``kanban_gate_invariants.retired_identities`` set, and the arm/filing seams themselves. No test
here retypes the tuple, so a future rename cannot be re-introduced by hand and still read green.

WHAT THIS SUITE CANNOT JUDGE, and why (stated where the check lives, so a green run is not read
as completeness): the fleet's profile roster. ``tests/conftest.py`` sandboxes ``HERMES_HOME``
away from production and ``tests/home_io_guard.py`` REFUSES any file I/O against the real
``~/.hermes``, so no test in this repo can read the live roster — an assertion against it would
either fail as a test bug or pass vacuously in every harness. The membership itself is therefore
asserted two ways that DO hold here: the kernel's own retired-identity list (which is where both
dead handles of this card sit), and the derivation — the advice is drawn from the roster at print
time, which ``test_a_renamed_member_leaves_the_advice_instead_of_being_printed`` proves by
renaming a member in the predicate. The live membership is a MEASUREMENT, not a test:

    HERMES_HOME=~/.hermes <install venv>/python -c '…_profile_is_real(<member>)…'   # all True
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from hermes_cli.subcommands.pause import (
    OPS_ALERT_ASSIGNEE,
    OPS_BOARD,
    RECOMMENDED_LANES,
    cmd_pause,
    recommended_lanes,
)

#: Every handle this verb emits as an identity: the lanes it advises, and the lane it files to.
#: DERIVED from the module's constants, so a new member is judged without editing this file.
HANDLES = (*RECOMMENDED_LANES, OPS_ALERT_ASSIGNEE)

#: The handle both this card and its parent retired: a rename of the lane now named `default`.
RETIRED_PLATFORM_WORKER = "platform-worker"

#: The handle the ops alert was filed to before this card: retired with the whole display-handle
#: generation, and in the kernel's seed set.
RETIRED_OPS_HANDLE = "yoyodine-majordomo"


@pytest.fixture(autouse=True)
def operator_context(monkeypatch):
    """Run every test in the OPERATOR's context.

    This suite is routinely executed from inside a dispatched kanban worker (a worker running
    pytest), and ``agent.estop.arm_admission`` refuses that venue by design — the lane a hold is
    starving must not be the thing that arms it. The refusal is cleared here and is asserted
    where it belongs, in ``tests/cron/test_estop_lockdown.py``.
    """
    from agent import estop
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "operator")
    monkeypatch.setattr(estop, "_stdin_is_tty", lambda: True)


def _isolated_home(tmp_path, monkeypatch) -> Path:
    """A scratch HERMES_HOME: a hold armed by a test can never strand the fleet."""
    from agent import estop

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nobody")
    estop._logged_components.clear()
    return home


# ── 1. the kernel's own retired-identity list ───────────────────────────────


def test_no_member_is_a_retired_fleet_identity():
    """Both dead handles of this card sit in the KERNEL's retired set — ``yoyodine-majordomo``
    in the seed, ``platform-worker`` aliased on disk. Keyed on that list, never a retyped copy."""
    from hermes_cli.kanban_gate_invariants import retired_identities

    retired = retired_identities(OPS_BOARD)
    offenders = sorted(name for name in HANDLES if name in retired)
    assert offenders == [], (
        f"{offenders} is/are a RETIRED fleet identity (kanban_gate_invariants.retired_identities): "
        "`hermes pause` would print it as advice the arm path then refuses, or file a card onto a "
        "handle that resolves nowhere")


# ── 2. the advice is DERIVED from the roster, not a remembered list ─────────


def test_a_renamed_member_leaves_the_advice_instead_of_being_printed(monkeypatch, capsys):
    """Rename a member in the PREDICATE (never in the tuple) and it must leave the advice.

    This is the half of the acceptance a repo test can hold: the recommendation is DRAWN FROM
    the live roster at print time, so a rename cannot re-introduce a dead name by hand, and the
    drop is REPORTED rather than quietly shortening the list.
    """
    renamed = RECOMMENDED_LANES[-1]
    live = {name for name in RECOMMENDED_LANES if name != renamed} | {"default"}
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in live)

    projected = recommended_lanes()
    assert renamed not in projected
    assert set(projected) == live & set(RECOMMENDED_LANES)

    rc = cmd_pause(argparse.Namespace(
        reason=None, lockdown=True, allow_profile=None, allow_user=None, ttl=None))
    assert rc == 2, "a lockdown naming no lane must still refuse"
    out = capsys.readouterr().out

    advised = [line for line in out.splitlines() if "(recommended:" in line]
    assert advised, "the refusal must still name a recommendation"
    assert all(renamed not in line for line in advised), (
        "a renamed lane was ADVISED — the recommendation is not drawn from the live roster")
    assert f"no live profile named {renamed}" in out, "the drop is reported, not silent"


# ── 3. each recommended lane ARMS through the real CLI path ─────────────────


def test_each_recommended_lane_arms_and_the_lane_gate_refuses_a_dead_one(
        tmp_path, monkeypatch, capsys):
    """``hermes pause --lockdown --allow-profile <lane>`` arms for EVERY recommended lane.

    The roster is pinned at the seam the arm path itself consults
    (``hermes_cli.profiles.profile_exists``), and the CONTROL arm proves the lane gate is armed
    in this harness — a lane outside the roster is refused — so a pass here is a real pass and
    not the empty-roster stand-down.
    """
    live = {name for name in RECOMMENDED_LANES} | {OPS_ALERT_ASSIGNEE}
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in live)
    _isolated_home(tmp_path, monkeypatch)

    from agent import estop

    for name in recommended_lanes():
        rc = cmd_pause(argparse.Namespace(
            reason="t_255cc087 arm proof", lockdown=True, allow_profile=[name],
            allow_user=None, ttl=None))
        assert rc == 0, (
            f"{name} is RECOMMENDED but the lockdown refused to arm it: "
            f"{capsys.readouterr().out}")
        state = estop.read_state()
        assert estop.work_admitted(name, board=OPS_BOARD, state=state) is True, (
            f"the hold armed for {name} does not admit {name}: the lockdown would hold the very "
            "lane it meant to let through")
        capsys.readouterr()

    refused = cmd_pause(argparse.Namespace(
        reason="control", lockdown=True, allow_profile=[RETIRED_PLATFORM_WORKER],
        allow_user=None, ttl=None))
    assert refused == 2, (
        "CONTROL FAILED: the lane gate admitted a name outside the roster, so this harness would "
        "pass no matter what the constants said")
    assert RETIRED_PLATFORM_WORKER in capsys.readouterr().out


# ── 4. the ops re-arm alert is FILABLE, and the old handle is refused ───────


def test_the_ops_alert_files_and_the_handle_it_replaced_is_refused(tmp_path, monkeypatch):
    """The capability this card restores, proved end to end on a SCRATCH kanban home.

    The card lands for real (the ROW is read back, not just a green return), and the handle it
    replaced is refused by the same gate — the control that shows the gate was armed here, not
    standing down on an empty roster.
    """
    from hermes_cli.subcommands import pause
    from hermes_cli import kanban_db_boards as boards

    roster = {name for name in HANDLES}
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr("hermes_cli.kanban_db.list_profiles_on_disk", lambda: sorted(roster))
    meta = boards.create_board(OPS_BOARD, name="ops (t_255cc087 proof)")

    filed = pause._file_ops_alert(resume_at="2026-10-13T22:00:00Z", actor="bot:platform-coder",
                                  reason="card t_255cc087", order="t_255cc087", ttl=None)
    assert filed["filed"] is True, f"the D9 alert could not be filed: {filed['detail']}"

    conn = sqlite3.connect(meta["db_path"])
    try:
        row = conn.execute("select assignee, created_by, status from tasks where id = ?",
                           (filed["id"],)).fetchone()
    finally:
        conn.close()
    assert row is not None, "the filing reported an id with no row behind it"
    assert row[0] == OPS_ALERT_ASSIGNEE, f"the alert landed on {row[0]!r}"
    assert row[1] == "hermes-pause" and row[2] == "ready"

    monkeypatch.setattr(pause, "OPS_ALERT_ASSIGNEE", RETIRED_OPS_HANDLE)
    refused = pause._file_ops_alert(resume_at="2026-10-13T22:01:00Z", actor="bot:platform-coder",
                                    reason="card t_255cc087 control", order="t_255cc087", ttl=None)
    assert refused["filed"] is False, (
        f"{RETIRED_OPS_HANDLE} filed a card — the create gate is standing down, so this test "
        "would prove nothing")
    assert "cannot run" in refused["detail"] and RETIRED_OPS_HANDLE in refused["detail"]
