"""A board deferred by the HOST worker budget must say so.

``kanban.max_in_progress`` is host-wide. When it is full, ``_tick_spawn_budget``
refuses the board outright — no spawn, no per-card suppression reason, nothing in
the board's own DB: from the outside a starved board looked exactly like an idle
one, and the only signal was a fleet-wide "0 workers spawned" line that named
nothing (2026-09-27).

Three contracts here:

* the refusal NAMES the cards it left behind (``deferred_host_capped``) — the
  fleet-wide warning and ``hermes kanban status`` can then say what is waiting,
* a board's SHARE of the budget bounds the tick while the host cap keeps
  bounding the fleet,
* a board that keeps losing the race for over an hour is REPORTED once, per
  board, and the clock restarts as soon as the board spawns again.

The companion gateway tests (``tests/gateway/test_kanban_dispatcher_board_order.py``)
cover the allocation that decides those shares.
"""

from __future__ import annotations

import functools
import logging
import sqlite3
import threading
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


def _deferred(*task_ids: str) -> kbd.DispatchResult:
    res = kbd.DispatchResult()
    res.deferred_host_capped = list(task_ids)
    return res


# ---------------------------------------------------------------------------
# 1. The refusal names the cards it deferred
# ---------------------------------------------------------------------------


def test_the_host_cap_denial_names_the_cards_it_deferred(kanban_home, all_assignees_spawnable):
    """Full host budget → the tick says WHICH cards it could not start."""
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        busy = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, busy) is not None

    spawns: list = []
    with kbc.connect() as conn:
        head = kb.create_task(conn, title="head", assignee="alice", priority=3)
        tail = kb.create_task(conn, title="tail", assignee="alice", priority=1)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=1,
        )

    assert spawns == []
    assert res.deferred_host_capped == [head, tail]


def test_a_board_with_no_share_records_the_same_deferral(kanban_home, all_assignees_spawnable):
    """Share 0 = more boards with work than slots: wait, but say so."""
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="waiting", assignee="alice")
        res = kbd.dispatch_once(
            conn,
            spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=4,
            host_budget_share=0,
        )

    assert spawns == []
    assert res.deferred_host_capped == [tid]


def test_a_spawnable_board_with_a_slot_records_no_deferral(kanban_home, all_assignees_spawnable):
    """The block must not fire for a board that is allowed to work."""
    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="waiting", assignee="alice")
        res = kbd.dispatch_once(
            conn,
            spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=4,
            host_budget_share=1,
        )

    assert len(spawns) == 1
    assert res.deferred_host_capped == []


def test_the_deferral_never_names_a_card_no_worker_could_start(
    kanban_home, all_assignees_spawnable,
):
    """Unassigned work waits for routing, not for a slot: it must not be named."""
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        busy = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, busy) is not None

    with kbc.connect() as conn:
        kb.create_task(conn, title="needs routing", assignee=None, priority=5)
        res = kbd.dispatch_once(conn, max_in_progress=1)

    assert res.deferred_host_capped == []


# ---------------------------------------------------------------------------
# 2. The share bounds the tick, the cap bounds the fleet
# ---------------------------------------------------------------------------


def test_the_share_bounds_the_tick(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(
            conn,
            spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=4,
            host_budget_share=1,
        )

    assert len(spawns) == 1
    assert len(res.spawned) == 1


def test_the_share_never_uncaps_the_fleet(kanban_home, all_assignees_spawnable):
    """A generous share cannot spend slots the host budget does not have."""
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        busy = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, busy) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="a", assignee="alice")
        res = kbd.dispatch_once(
            conn,
            spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=1,
            host_budget_share=4,
        )

    assert spawns == []
    assert res.deferred_host_capped != []


def test_a_share_of_two_lets_two_through(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b", "c", "d"):
            kb.create_task(conn, title=title, assignee="alice")
        kbd.dispatch_once(
            conn,
            spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=8,
            host_budget_share=2,
        )

    assert len(spawns) == 2


# ---------------------------------------------------------------------------
# 3. The report: the fleet-wide line names the hold
# ---------------------------------------------------------------------------


def test_describe_suppression_names_the_host_cap_hold():
    assert "host_cap_deferred=2" in kbd.describe_suppression([_deferred("t1", "t2")])


def test_describe_suppression_stays_quiet_when_nothing_was_deferred():
    assert "host_cap_deferred" not in kbd.describe_suppression([kbd.DispatchResult()])


# ---------------------------------------------------------------------------
# 4. The starvation clock
# ---------------------------------------------------------------------------


def test_the_clock_waits_for_the_deferral_to_stop_being_transient():
    clock = kbd.HostCapStarvationClock(defer_seconds=3600.0)
    res = _deferred("t_ops")

    assert clock.observe([("ops", res)], now=1_000.0) is None
    assert clock.observe([("ops", res)], now=1_000.0 + 3_599) is None

    line = clock.observe([("ops", res)], now=1_000.0 + 3_601)

    assert line is not None
    assert "ops" in line and "t_ops" in line and "60m" in line
    assert "kanban.max_in_progress" in line


def test_the_clock_does_not_repeat_itself_every_tick():
    """One line for one starvation: it must not become the new log spam."""
    clock = kbd.HostCapStarvationClock(defer_seconds=0.0)
    res = _deferred("t_ops")

    clock.observe([("ops", res)], now=1.0)
    first = clock.observe([("ops", res)], now=10.0)
    second = clock.observe([("ops", res)], now=20.0)

    assert first is not None
    assert second is not None
    assert "100" not in second  # age keeps counting from the first deferral


def test_the_clock_restarts_when_the_board_spawns_again():
    clock = kbd.HostCapStarvationClock(defer_seconds=60.0)
    deferred = _deferred("t_ops")
    idle = kbd.DispatchResult()

    assert clock.observe([("ops", deferred)], now=0.0) is None
    assert clock.observe([("ops", idle)], now=100.0) is None
    assert clock.observe([("ops", deferred)], now=10_000.0) is None
    assert clock.observe([("ops", deferred)], now=10_061.0) is not None


def test_a_tick_that_named_no_card_is_not_evidence_of_starvation():
    clock = kbd.HostCapStarvationClock(defer_seconds=0.0)
    empty = kbd.DispatchResult()

    assert clock.observe([("ops", empty)], now=0.0) is None
    assert clock.observe([("ops", empty)], now=1_000_000.0) is None


def test_the_clock_reports_the_board_that_has_waited_longest():
    clock = kbd.HostCapStarvationClock(defer_seconds=10.0)
    older = _deferred("t_aaa")
    newer = _deferred("t_bbb")

    clock.observe([("aaa", older)], now=0.0)
    clock.observe([("aaa", older), ("bbb", newer)], now=100.0)
    line = clock.observe([("aaa", older), ("bbb", newer)], now=200.0)

    assert line is not None and "aaa" in line and "t_aaa" in line


def test_the_clock_survives_a_result_without_the_field():
    """Robust to a stubbed result: an unknown tick is not a starvation."""
    clock = kbd.HostCapStarvationClock(defer_seconds=0.0)

    assert clock.observe([("ops", object())], now=0.0) is None
    assert clock.observe([("ops", None)], now=1.0) is None


def test_reset_forgets_every_board():
    clock = kbd.HostCapStarvationClock(defer_seconds=0.0)
    res = _deferred("t_ops")
    clock.observe([("ops", res)], now=0.0)

    clock.reset()

    assert clock.observe([("ops", res)], now=1.0) is None


# ---------------------------------------------------------------------------
# 5. The standalone daemon path reports it too
# ---------------------------------------------------------------------------


def test_the_daemon_reports_a_queue_that_keeps_losing_the_host_budget(
    kanban_home, monkeypatch, caplog,
):
    """``hermes kanban daemon`` is the other entry point into the same budget."""
    stop = threading.Event()
    ticks: dict = {"n": 0}

    monkeypatch.setattr(kbd, "dispatch_once", lambda conn, **kwargs: _deferred("t_ops_head"))
    monkeypatch.setattr(
        kbd,
        "HostCapStarvationClock",
        functools.partial(kbd.HostCapStarvationClock, defer_seconds=0.0),
    )

    def on_tick(res):
        ticks["n"] += 1
        if ticks["n"] >= 3:
            stop.set()

    with caplog.at_level(logging.WARNING):
        kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert ticks["n"] >= 3
    assert "t_ops_head" in caplog.text
