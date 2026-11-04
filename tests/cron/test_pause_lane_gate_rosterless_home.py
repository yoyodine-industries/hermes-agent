"""A hold armed into a SANDBOX still names lanes of the fleet that serves them (t_53aeba04).

The update flow rehearses its window against a sandbox ``HERMES_HOME``
(`hermes-update-dry-run-home`) precisely so a rehearsal can never strand the fleet. That home
carries no ``profiles/`` at all, so the arm path's lane gate answered ``profile_exists() ==
False`` for every lane and read that as an UNKNOWN id:

    ⛔ Unknown profile(s) in --allow-profile: platform-coder, platform-stl — refusing to arm.

Measured 2026-11-03: 18 consecutive `hermes-update` runs died in `drain_gate` on that sentence,
so no window ever opened. The gate is RIGHT to fail closed on a name the roster does not hold —
that is where a typo lives (:func:`test_a_rosterless_home_still_refuses_with_no_declared_roster`
below pins it) — but a sandbox is not a roster. The caller that knows which fleet serves the
lanes now DECLARES it (``HERMES_ESTOP_ROSTER_HOME``, and the yaan verb passes the fleet root),
and the gate judges names against the roster that would actually serve them.

Every check runs against a private ``HERMES_HOME``; no live sentinel is touched.
"""

from __future__ import annotations

import argparse

import pytest

from agent import estop


@pytest.fixture(autouse=True)
def operator_context(monkeypatch):
    """Run in the OPERATOR's context: ``arm_admission`` fences a dispatched worker by design."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    monkeypatch.delenv("HERMES_ESTOP_ROSTER_HOME", raising=False)
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "operator")
    monkeypatch.setattr(estop, "_stdin_is_tty", lambda: True)
    estop._logged_components.clear()


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    """A rehearsal-shaped home: a real directory, and NO ``profiles/`` under it."""
    home = tmp_path / "hermes-update-dry-run-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def fleet_home(tmp_path):
    """The roster's home: ``profiles/<lane>`` with the identity marker a live lane carries."""
    home = tmp_path / "fleet"
    for lane in ("platform-coder", "platform-stl"):
        (home / "profiles" / lane).mkdir(parents=True)
        (home / "profiles" / lane / "config.yaml").write_text("model: pinned\n", encoding="utf-8")
    return home


def _pause_args(**overrides) -> argparse.Namespace:
    args = dict(reason="card t_53aeba04 rehearsal", lockdown=False, allow_profile=None,
                allow_user=None, ttl=None, order=None, history=False, history_limit=20,
                json=False)
    args.update(overrides)
    return argparse.Namespace(**args)


def test_a_rosterless_home_still_refuses_with_no_declared_roster(sandbox_home, capsys):
    """THE CLOSED SIDE, unchanged: with nothing declared the gate answers from HERMES_HOME.

    This is the typo case, and it must keep failing closed -- a sandbox arm only changes
    behaviour once its caller DECLARES the roster that serves the lanes.
    """
    from hermes_cli.subcommands.pause import cmd_pause

    rc = cmd_pause(_pause_args(lockdown=True, allow_profile=["platform-coder"], ttl="5m"))

    out = capsys.readouterr().out
    assert rc == 2, out
    assert "Unknown profile(s) in --allow-profile: platform-coder" in out
    assert not estop.sentinel_path().exists()


def test_a_declared_roster_lets_the_sandbox_arm_the_real_lanes(
        sandbox_home, fleet_home, monkeypatch, capsys):
    """The rehearsal's own shape: hold lands in the sandbox, lanes judged by the fleet roster."""
    from hermes_cli.subcommands.pause import cmd_pause

    monkeypatch.setenv("HERMES_ESTOP_ROSTER_HOME", str(fleet_home))

    rc = cmd_pause(_pause_args(lockdown=True,
                               allow_profile=["platform-coder", "platform-stl"], ttl="5m"))

    out = capsys.readouterr().out
    assert rc == 0, out
    sentinel = estop.sentinel_path()
    assert sentinel == sandbox_home / "ESTOP", f"the hold must land in the SANDBOX: {sentinel}"
    assert estop.work_admitted("platform-coder") is True
    assert estop.work_admitted("platform-stl") is True
    assert estop.work_admitted("research-coder") is False, "a lockdown holds per item"


def test_a_declared_roster_still_refuses_a_lane_it_does_not_name(
        sandbox_home, fleet_home, monkeypatch, capsys):
    """Fidelity, not looseness: the declared roster is CONSULTED, so a typo still refuses."""
    from hermes_cli.subcommands.pause import cmd_pause

    monkeypatch.setenv("HERMES_ESTOP_ROSTER_HOME", str(fleet_home))

    rc = cmd_pause(_pause_args(lockdown=True, allow_profile=["platform-stll"], ttl="5m"))

    out = capsys.readouterr().out
    assert rc == 2, out
    assert "platform-stll" in out and "Unknown profile(s)" in out
    assert f"Roster judged: {fleet_home}/profiles" in out, (
        "the refusal must name the roster it judged:\n" + out)
    assert not estop.sentinel_path().exists()


def test_a_declared_LANE_home_steps_up_to_the_fleet_roster(tmp_path, monkeypatch):
    """A caller may declare a lane's own home; the roster is still the FLEET's (no lane roster)."""
    from hermes_cli.profiles import profile_exists, profiles_root_of

    fleet = tmp_path / "fleet"
    lane_home = fleet / "profiles" / "platform-coder"
    lane_home.mkdir(parents=True)
    (lane_home / "config.yaml").write_text("model: pinned\n", encoding="utf-8")

    assert profiles_root_of(lane_home) == fleet / "profiles"
    assert profiles_root_of(fleet) == fleet / "profiles"
    assert profile_exists("platform-coder", home=lane_home) is True
    assert profile_exists("nobody-here", home=lane_home) is False


def test_default_is_the_home_itself_and_always_resolves(tmp_path):
    """``default`` needs no profile dir -- but a garbage name is still refused by the roster."""
    from hermes_cli.profiles import profile_exists

    home = tmp_path / "fleet"
    (home / "profiles").mkdir(parents=True)
    assert profile_exists("default", home=home) is True
    assert profile_exists("not-a-lane", home=home) is False
    assert profile_exists("../escape", home=home) is False, "a path never becomes a lane"
