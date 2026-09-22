"""Cross-board dispatch fairness: a deep queue on one board must not starve siblings.

The gateway dispatcher ticks every active board in ``list_boards`` order, and
each board's tick recomputes the host-level budget live. Before this change,
the first board in the order with spawnable work consumed every free host slot
each tick, so boards behind its ready queue were starved indefinitely — no
matter how long their cards had waited.

This file tests the two pieces that fix it:

1. :func:`kanban_db_dispatch.fair_share_spawn_budget` — the pure apportionment
   function (floor share + age-ordered remainder + count-cap redistribution).
2. The gateway coordinator (:meth:`_KanbanDispatcher._fair_share_caps`) and the
   ``tick_spawn_cap`` ceiling threaded through ``dispatch_once``.

The review-lane reservation inside a single board (:func:`_dispatch_once_locked`)
was the in-board precedent; this is its cross-board analog.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    return fake_spawn


# ---------------------------------------------------------------------------
# fair_share_spawn_budget — the pure apportionment function
# ---------------------------------------------------------------------------


def test_fair_share_empty_or_no_budget():
    assert kbd.fair_share_spawn_budget(6, []) == {}
    assert kbd.fair_share_spawn_budget(0, [("a", 10.0, 3)]) == {}
    assert kbd.fair_share_spawn_budget(-1, [("a", 10.0, 3)]) == {}


def test_fair_share_single_board_gets_full_budget():
    caps = kbd.fair_share_spawn_budget(6, [("a", 10.0, 20)])
    assert caps == {"a": 6}


def test_fair_share_equal_division():
    caps = kbd.fair_share_spawn_budget(6, [("a", 10.0, 20), ("b", 5.0, 20)])
    assert caps == {"a": 3, "b": 3}


def test_fair_share_remainder_goes_to_oldest_first():
    # 6 slots, 4 boards → floor 1 each, remainder 2 → oldest two get 2.
    work = [("young-1", 1.0, 20), ("old-1", 100.0, 20), ("young-2", 2.0, 20), ("old-2", 90.0, 20)]
    caps = kbd.fair_share_spawn_budget(6, work)
    assert caps["old-1"] == 2
    assert caps["old-2"] == 2
    assert caps["young-1"] == 1
    assert caps["young-2"] == 1
    assert sum(caps.values()) == 6


def test_fair_share_count_cap_redistributes_oldest_first():
    # A board with a short queue cannot absorb its floor share; the freed slots
    # flow to the board that still has work (here the only other one).
    caps = kbd.fair_share_spawn_budget(6, [("deep", 100.0, 20), ("shallow", 50.0, 1)])
    assert caps["shallow"] == 1
    assert caps["deep"] == 5
    assert sum(caps.values()) == 6


def test_fair_share_scarcity_returns_explicit_zero_not_omission():
    # Fewer slots than boards: every board with work must be present so the
    # dispatcher caps the losers to 0 instead of letting board order grab the
    # scarce slot behind the live host-cap backstop.
    work = [(f"b{i}", float(i), 20) for i in range(5)]  # b4 is oldest
    caps = kbd.fair_share_spawn_budget(2, work)
    assert caps["b4"] == 1
    assert caps["b3"] == 1
    assert caps["b0"] == 0
    assert caps["b1"] == 0
    assert caps["b2"] == 0
    assert set(caps.keys()) == {f"b{i}" for i in range(5)}
    assert sum(caps.values()) == 2


# ---------------------------------------------------------------------------
# board_spawnable_work — the per-board probe
# ---------------------------------------------------------------------------


def test_board_spawnable_work_count_and_oldest_age(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="a", assignee="alice")
        b = kb.create_task(conn, title="b", assignee="alice")
        c = kb.create_task(conn, title="c", assignee="bob")
        # Fix created_at so age ordering is deterministic against an explicit now.
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (1000.0, a))
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (2000.0, b))
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (3000.0, c))

        oldest_age, count = kbd.board_spawnable_work(conn, now=5000.0)
        assert count == 3
        # a (created 1000) is the oldest → age 4000 against now=5000.
        assert oldest_age == 4000.0


def test_board_spawnable_work_excludes_control_plane(kanban_home, monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kbc.connect() as conn:
        kb.create_task(conn, title="cc-lane", assignee="orion-cc")
        assert kbd.board_spawnable_work(conn) == (0.0, 0)


# ---------------------------------------------------------------------------
# tick_spawn_cap ceiling through dispatch_once
# ---------------------------------------------------------------------------


def test_dispatch_once_tick_spawn_cap_limits_spawns(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), tick_spawn_cap=1)

    assert len(spawns) == 1
    assert len(res.spawned) == 1


def test_dispatch_once_tick_spawn_cap_zero_spawns_nothing(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="a", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), tick_spawn_cap=0)

    assert not spawns
    assert not res.spawned


def test_dispatch_once_no_tick_spawn_cap_preserves_old_behavior(kanban_home, all_assignees_spawnable):
    # No cap → the full queue spawns (host/per-board caps unset), unchanged.
    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))

    assert len(spawns) == 3
    assert len(res.spawned) == 3


# ---------------------------------------------------------------------------
# The coordinator: _KanbanDispatcher._fair_share_caps + tick_once
# ---------------------------------------------------------------------------


def _make_dispatcher(max_in_progress):
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher

    settings = _DispatcherSettings(
        interval=60.0,
        max_spawn=None,
        max_in_progress=max_in_progress,
        failure_limit=3,
        stale_timeout_seconds=0,
        reconcile_orphans=True,
        default_assignee=None,
        max_in_progress_per_profile=None,
    )
    return _KanbanDispatcher(kb, settings)


def test_fair_share_caps_apportions_across_boards(kanban_home, all_assignees_spawnable, monkeypatch):
    kb.create_board("ops")
    kb.create_board("research")

    with kbc.connect(board="ops") as conn:
        for i in range(20):
            kb.create_task(conn, title=f"ops-{i}", assignee="alice")
    with kbc.connect(board="research") as conn:
        for i in range(2):
            kb.create_task(conn, title=f"research-{i}", assignee="bob")

    from gateway import kanban_watchers_dispatcher as mod

    monkeypatch.setattr(mod, "_board_slugs", lambda kb_: ["ops", "research"])

    dispatcher = _make_dispatcher(max_in_progress=6)
    caps = dispatcher._fair_share_caps(["ops", "research"])

    # The deep queue (ops, 20 cards) no longer absorbs the whole host budget:
    # research's 2 cards get slots too.
    assert caps["research"] == 2
    assert caps["ops"] == 4
    assert sum(caps.values()) == 6


def test_fair_share_caps_noop_without_host_cap(kanban_home, all_assignees_spawnable, monkeypatch):
    kb.create_board("ops")
    with kbc.connect(board="ops") as conn:
        kb.create_task(conn, title="ops-0", assignee="alice")

    from gateway import kanban_watchers_dispatcher as mod

    monkeypatch.setattr(mod, "_board_slugs", lambda kb_: ["ops"])

    dispatcher = _make_dispatcher(max_in_progress=None)
    assert dispatcher._fair_share_caps(["ops"]) == {}


def test_tick_once_passes_caps_to_dispatch(kanban_home, all_assignees_spawnable, monkeypatch):
    kb.create_board("ops")
    kb.create_board("research")

    with kbc.connect(board="ops") as conn:
        for i in range(20):
            kb.create_task(conn, title=f"ops-{i}", assignee="alice")
    with kbc.connect(board="research") as conn:
        kb.create_task(conn, title="research-0", assignee="bob")

    from gateway import kanban_watchers_dispatcher as mod

    monkeypatch.setattr(mod, "_board_slugs", lambda kb_: ["ops", "research"])

    captured: dict = {}

    def fake_dispatch_once(conn, **kwargs):
        captured[kwargs.get("board")] = kwargs.get("tick_spawn_cap")
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)

    dispatcher = _make_dispatcher(max_in_progress=6)
    dispatcher.tick_once()

    # ops is first in board order, yet research still receives a non-zero cap.
    assert captured["research"] >= 1
    assert captured["ops"] is not None
    assert captured["research"] is not None
    assert captured["ops"] + captured["research"] <= 6
