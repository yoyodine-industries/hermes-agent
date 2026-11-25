"""The dispatch ceiling is a HOST property, not a property of the invoking seat.

SEV1 card ``t_001338fa``. MEASURED 2026-11-25, same command, same board:

    hermes                       kanban --board platform dispatch --dry-run
        -> Spawned: 0 ; Held (board ceiling max_spawn=8 met: 8 running)
    hermes -p financially-stl    kanban --board platform dispatch --dry-run
        -> Spawned: 5183   (no hold, no cap)
    hermes -p research-stl       kanban --board platform dispatch --dry-run
        -> Spawned: 5183   (no hold, no cap)

The caps were built from the INVOKING profile's ``config.yaml`` alone, and the
lane profiles carry no ``kanban:`` block — so every cap resolved to ``None``,
and ``None`` means UNLIMITED on the spawn path. The five thousand agents were a
config-resolution defect, not a backlog.

The ruling this file guards (platform-stl, same card):
  1. the HOST (root) config is the BASELINE, whoever invoked the command;
  2. the invoking profile overrides PER KEY, but may only NARROW — raising the
     fleet ceiling is the host's decision (``kanban.allow_profile_cap_raise``);
  3. an UNSET cap must never mean unlimited — it resolves to the host floor.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

import pytest


@pytest.fixture()
def kanban_home_with_profiles(monkeypatch):
    """A fresh HERMES_HOME with a root config, three lane profiles and a kanban DB."""
    test_home = tempfile.mkdtemp(prefix="kanban_host_caps_")
    for prof in ("default", "financially-stl", "research-stl", "platform-coder"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
        with open(os.path.join(test_home, "profiles", prof, "config.yaml"), "w") as fh:
            fh.write("{}\n")  # identity marker: a bare dir is not a profile
    monkeypatch.setenv("HERMES_HOME", os.path.join(test_home, "profiles", "financially-stl"))
    saved = {m: sys.modules[m] for m in list(sys.modules)
             if m.startswith("hermes_cli") or m.startswith("hermes_state")
             or m == "hermes_constants"}
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    from hermes_cli import kanban_db_dispatch
    # The purge above drops the conftest's memory-sample neutralizer; re-apply it
    # so the derived host cap cannot depend on the test runner's live memory.
    monkeypatch.setattr(kanban_db_dispatch, "_system_memory_sample", dict)
    yield test_home
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    sys.modules.update(saved)


def _write_root_config(test_home: str, body: str) -> None:
    with open(os.path.join(test_home, "config.yaml"), "w") as fh:
        fh.write(body)


# ---------------------------------------------------------------------------
# (a) the kanban block ABSENT -> the host floor, never unlimited
# ---------------------------------------------------------------------------

def test_absent_block_resolves_to_the_host_floor_never_none(kanban_home_with_profiles):
    """The exact shape of a lane seat: no ``kanban:`` block anywhere.

    ``None`` here is what planned 5,183 spawns, so the assertion is not just
    "a number" — it is the floor, and it is the same number the root declares.
    """
    from hermes_cli import kanban_db_dispatch as kbd

    caps = kbd.resolve_dispatch_caps({}, host_kanban_cfg={})
    assert caps["max_spawn"] == kbd.DEFAULT_HOST_MAX_SPAWN == 8
    assert caps["max_in_progress"] == 8
    assert caps["max_spawn"] is not None


def test_absent_block_takes_the_root_config_as_the_baseline(kanban_home_with_profiles):
    """The ROOT file is read even though the ACTIVE profile is a lane seat."""
    from hermes_cli import kanban_db_dispatch as kbd

    _write_root_config(
        kanban_home_with_profiles,
        "kanban:\n  max_spawn: 6\n  max_in_progress: 6\n  max_in_progress_per_profile: 3\n",
    )
    caps = kbd.resolve_dispatch_caps({})
    assert caps["max_spawn"] == 6
    assert caps["max_in_progress"] == 6
    assert caps["max_in_progress_per_profile"] == 3


# ---------------------------------------------------------------------------
# (b) a profile block NARROWS -> it wins
# ---------------------------------------------------------------------------

def test_profile_block_narrows_the_host_ceiling(kanban_home_with_profiles):
    from hermes_cli import kanban_db_dispatch as kbd

    caps = kbd.resolve_dispatch_caps(
        {"max_spawn": 4}, host_kanban_cfg={"max_spawn": 8, "max_in_progress": 8}
    )
    assert caps["max_spawn"] == 4
    assert caps["clamped_to_host"] == []


# ---------------------------------------------------------------------------
# (c) host 8 + profile 24 -> the host binds unless the HOST delegates
# ---------------------------------------------------------------------------

def test_profile_cannot_raise_the_host_ceiling(kanban_home_with_profiles):
    """Default ruling: the host floor binds, and the refusal is recorded."""
    from hermes_cli import kanban_db_dispatch as kbd

    caps = kbd.resolve_dispatch_caps(
        {"max_spawn": 24, "max_in_progress": 64},
        host_kanban_cfg={"max_spawn": 8, "max_in_progress": 8},
    )
    assert caps["max_spawn"] == 8
    assert caps["max_in_progress"] == 8
    assert caps["clamped_to_host"] == ["max_spawn", "max_in_progress"]


def test_host_can_explicitly_delegate_the_raise(kanban_home_with_profiles):
    """``kanban.allow_profile_cap_raise: true`` in the HOST file is the one door.

    It is deliberately NOT set on this estate: a seat that raises the fleet
    ceiling must be a host decision, never a side effect of the seat's config.
    """
    from hermes_cli import kanban_db_dispatch as kbd

    caps = kbd.resolve_dispatch_caps(
        {"max_spawn": 24},
        host_kanban_cfg={"max_spawn": 8, "allow_profile_cap_raise": True},
    )
    assert caps["max_spawn"] == 24
    assert caps["clamped_to_host"] == []


# ---------------------------------------------------------------------------
# the ONLY difference is WHICH PROFILE INVOKED IT -> the plan is the same bound
# ---------------------------------------------------------------------------

def test_the_invoking_profile_does_not_change_the_plan(kanban_home_with_profiles):
    """Two seats, one board, one host: the spawn bound must be identical.

    A lane seat with NO block (the measured defect) and the orchestrator seat
    declaring the same ceiling must plan the same tick. A seat asking for MORE
    is held to the host value, so it cannot diverge either.
    """
    from hermes_cli import kanban_db_dispatch as kbd

    host = {"max_spawn": 8, "max_in_progress": 8, "max_in_progress_per_profile": 8}
    orchestrator = kbd.resolve_dispatch_caps({"max_spawn": 8}, host_kanban_cfg=host)
    lane_no_block = kbd.resolve_dispatch_caps({}, host_kanban_cfg=host)
    lane_greedy = kbd.resolve_dispatch_caps({"max_spawn": 5000}, host_kanban_cfg=host)

    for caps in (orchestrator, lane_no_block, lane_greedy):
        assert caps["max_spawn"] == 8
        assert caps["max_in_progress"] == 8
    assert lane_no_block["max_spawn"] == orchestrator["max_spawn"]


def test_a_real_tick_is_bounded_from_a_lane_seat(kanban_home_with_profiles):
    """Plan-level proof: 12 ready cards, and BOTH seats spawn at most the cap.

    This is the measured defect at the level it was measured: run the tick the
    way each seat's CLI would, and compare ``len(res.spawned)``.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    kb.init_db()
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(12):
            kb.create_task(conn, title=f"card-{i}", assignee="default")

    host = {"max_spawn": 8, "max_in_progress": 8}
    plans = {}
    for seat, profile_cfg in (
        ("orchestrator", {"max_spawn": 8}),
        ("lane-no-block", {}),
        ("lane-greedy", {"max_spawn": 5000}),
    ):
        caps = kbd.resolve_dispatch_caps(profile_cfg, host_kanban_cfg=host)
        with kbc.connect_closing() as conn:
            res = kbd.dispatch_once(
                conn,
                dry_run=True,
                spawn_fn=lambda *a, **k: 4242,
                max_spawn=caps["max_spawn"],
                max_in_progress=caps["max_in_progress"],
                max_in_progress_per_profile=caps["max_in_progress_per_profile"],
            )
        plans[seat] = len(res.spawned)

    assert plans["lane-no-block"] == plans["orchestrator"], plans
    assert plans["lane-greedy"] == plans["orchestrator"], plans
    assert plans["lane-no-block"] <= 8, plans


# ---------------------------------------------------------------------------
# the CLI and the gateway both go through the same resolver
# ---------------------------------------------------------------------------

def test_cli_dispatch_passes_the_host_ceiling_from_a_lane_seat(
    kanban_home_with_profiles, monkeypatch,
):
    """``hermes -p <lane> kanban dispatch`` must pass the HOST cap to the tick."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban as kb_cli

    kb.init_db()
    # The lane seat's config: an EMPTY kanban block (its real config.yaml has none,
    # and load_config() fills only DEFAULT_CONFIG's None values in).
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})
    monkeypatch.setattr(
        kbd, "host_kanban_config",
        lambda: {"max_spawn": 8, "max_in_progress": 8, "max_in_progress_per_profile": 8},
    )

    captured = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False)
    kb_cli._cmd_dispatch(args)

    assert captured.get("max_spawn") == 8
    assert captured.get("max_in_progress") == 8
    assert captured.get("max_in_progress_per_profile") == 8


def test_cli_dispatch_clamps_a_greedy_profile_and_says_so(
    kanban_home_with_profiles, monkeypatch, capsys,
):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban as kb_cli

    kb.init_db()
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {"max_spawn": 24}})
    monkeypatch.setattr(kbd, "host_kanban_config", lambda: {"max_spawn": 8, "max_in_progress": 8})

    captured = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    kb_cli._cmd_dispatch(argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False))

    assert captured.get("max_spawn") == 8
    out = capsys.readouterr().out
    assert "max_spawn requested above the host ceiling" in out


def test_gateway_dispatcher_settings_are_host_bounded(kanban_home_with_profiles, monkeypatch):
    """A gateway booted as a lane seat enforces the host ceiling, not None."""
    from gateway import kanban_watchers_dispatcher as kwd
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    kb.init_db()
    monkeypatch.setattr(kbd, "host_kanban_config", lambda: {"max_spawn": 8, "max_in_progress": 8})

    lane = kwd._resolve_dispatcher_settings({"max_spawn": 24}, kb)
    assert lane.max_spawn == 8
    assert lane.max_in_progress == 8

    empty = kwd._resolve_dispatcher_settings({}, kb)
    assert empty.max_spawn == 8
    assert empty.max_in_progress == 8
