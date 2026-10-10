"""Regression: the host budget is allocated by GLOBAL CARD PRIORITY, not rotation.

Card t_323b14a5. The defect: ``host_budget_shares`` handed one guaranteed slot
to each board in ROTATION order and the whole REMAINDER to the board visited
first, so the rotation head — not the priority column — took the bulk of the
host budget. A single operator-designated (999999) card on a low-traffic board
therefore waited behind whatever board happened to rotate to the head, and a
board whose oldest card was a band lower still won the whole remainder.

The fix (``gateway/kanban_watchers_dispatcher.py``) ranks EVERY startable card
on EVERY dispatchable board by ``priority DESC`` and hands the free host slots
out down that one list, bounded by each board's remaining ``max_spawn``
capacity. ``board_visit_order`` keeps the head-of-line RANK and rotates only
INSIDE a priority band, so equal-priority work still round-robins; every board
is still visited for reclaim, promotion, decomposition and health whether or
not it won a slot.

NOTE on the band VALUES used by the pure-allocator tests: 999999 and 999000 are
the operator's reserved-tranche priorities (``TRANCHE_FLOOR``..``TRANCHE_TOP``).
The allocator is band-agnostic — it reads whatever ``priority`` a card carries —
so the unit tests exercise the literal values the design names, while the
end-to-end dispatcher tests use ordinary-band priorities (900000 / 800000) so
they need no tranche entitlement. Both prove the same mechanism.

Precondition: this module only proves anything when ``hermes_cli`` / ``gateway``
import from THIS tree (the fix's worktree), never the install venv's editable
copy — asserted by ``test_imports_resolve_to_this_tree``.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import gateway.kanban_watchers_dispatcher as kwd
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


def _settings(**overrides):
    """A ``_DispatcherSettings`` with the real fields, overridable by name."""
    values = dict(
        interval=60.0, max_spawn=8, max_in_progress=None, failure_limit=3,
        stale_timeout_seconds=0, reconcile_orphans=True, default_assignee=None,
        max_in_progress_per_profile=None, lane_fair_spawn=True,
        designated_pool_reserve=1, max_spawn_by_board={},
    )
    values.update(overrides)
    return kwd._DispatcherSettings(**values)


def _seed(board, n, priority=0):
    """Create *n* startable cards on *board*; return their ordered ids."""
    ids = []
    with kbc.connect(board=board) as conn:
        for i in range(n):
            ids.append(kb.create_task(
                conn, title=f"{board}-{i}", assignee="alice", priority=priority,
            ))
    return ids


def _running(board):
    with kbc.connect(board=board) as conn:
        return kbd.count_running_tasks(conn)


def _recorder(record):
    """A ``_default_spawn`` stand-in recording ``(board, task_id)`` per spawn."""
    def fake_spawn(task, workspace, board=None):
        record.append((board, task.id))
        return 42

    return fake_spawn


def test_imports_resolve_to_this_tree():
    """The tree under test is the one that answers — not the install venv."""
    import hermes_cli

    repo = Path(__file__).resolve().parents[2]
    assert Path(hermes_cli.__file__).resolve().parents[1] == repo
    assert Path(kwd.__file__).resolve().parents[1] == repo


# ---------------------------------------------------------------------------
# The allocator, in isolation
# ---------------------------------------------------------------------------


def test_allocator_ranks_a_top_band_card_above_a_deep_lower_band_queue():
    """The DoD core: 10 free, 8x999000 on defcon (cap 8), 1x999999 on ops (cap 2)."""
    cards = [("999000", "defcon")] * 8 + [("999999", "ops")]

    shares = kwd.host_budget_shares_by_priority(10, cards, {"defcon": 8, "ops": 2})

    assert shares == {"defcon": 8, "ops": 1}


def test_allocator_priority_wins_even_when_the_lower_board_is_supplied_first():
    """Rotation head first must not decide the budget — only priority may."""
    cards = [("999000", "defcon")] * 8 + [("999999", "ops")]

    # One slot: the single ops card takes it, not defcon's rotation-first queue.
    assert kwd.host_budget_shares_by_priority(1, cards, None) == {"ops": 1}


def test_allocator_round_robins_equal_priority_between_boards():
    """Equal priorities interleave per slot, so no board starves the other."""
    cards = [("0", "defcon")] * 20 + [("0", "ops")] * 20

    shares = kwd.host_budget_shares_by_priority(10, cards, {"defcon": 8, "ops": 2})

    assert shares == {"defcon": 8, "ops": 2}


def test_allocator_respects_capacity_and_does_not_strand_slots():
    """A capped board neither consumes nor strands a slot a lower board can use."""
    cards = [("0", "a")] * 20 + [("0", "b")] * 3

    shares = kwd.host_budget_shares_by_priority(10, cards, {"a": 2, "b": 8})

    assert shares == {"a": 2, "b": 3}


def test_allocator_without_free_budget_allocates_nothing():
    assert kwd.host_budget_shares_by_priority(0, [("0", "a")], None) == {}
    assert kwd.host_budget_shares_by_priority(None, [("0", "a")], None) == {}


# ---------------------------------------------------------------------------
# Visit order: rank is fixed, rotation moves only inside a band
# ---------------------------------------------------------------------------


def test_visit_order_ranks_by_priority():
    dispatcher = kwd._KanbanDispatcher(kb, _settings())

    assert dispatcher.board_visit_order({"low": 0, "high": 5}) == ["high", "low"]


def test_visit_order_rotates_only_within_an_equal_priority_band():
    dispatcher = kwd._KanbanDispatcher(kb, _settings())

    seen = set()
    for _ in range(2):
        seen.add(tuple(dispatcher.board_visit_order({"a": 0, "b": 0})))

    assert seen == {("a", "b"), ("b", "a")}


def test_visit_order_never_rotates_a_board_past_a_higher_band():
    """A rotated band cannot lift a lower board above a higher one."""
    dispatcher = kwd._KanbanDispatcher(kb, _settings())

    for _ in range(5):
        order = dispatcher.board_visit_order({"low": 1, "mid": 1, "high": 9})
        assert order.index("high") == 0


# ---------------------------------------------------------------------------
# End to end through the dispatcher's tick
# ---------------------------------------------------------------------------


def test_tick_spawns_the_higher_priority_board_first(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A 900000 card on ops spawns before any 800000 card on defcon."""
    kb.create_board("defcon")
    kb.create_board("ops")
    _seed("defcon", 8, priority=800000)
    ops_ids = _seed("ops", 1, priority=900000)

    record: list = []
    monkeypatch.setattr(kbd, "_default_spawn", _recorder(record))

    dispatcher = kwd._KanbanDispatcher(kb, _settings(
        max_in_progress=10, max_spawn=8,
        max_spawn_by_board={"defcon": 8, "ops": 2},
    ))
    dispatcher.tick_once()

    assert record, "nothing spawned"
    boards = [board for board, _ in record]
    assert record[0] == ("ops", ops_ids[0]), "ops card must spawn first"
    assert boards.count("defcon") == 8
    assert boards[1:] == ["defcon"] * 8, "every defcon card follows the ops card"


def test_tick_round_robins_equal_priority_boards_without_starvation(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Two deep equal-priority queues both make progress — no board starves."""
    kb.create_board("a")
    kb.create_board("b")
    _seed("a", 30)
    _seed("b", 30)

    monkeypatch.setattr(kbd, "_default_spawn", _recorder([]))

    dispatcher = kwd._KanbanDispatcher(kb, _settings(max_in_progress=4, max_spawn=8))
    for _ in range(3):
        dispatcher.tick_once()

    assert _running("a") > 0
    assert _running("b") > 0


def test_tick_still_reclaims_a_starved_boards_stale_claim(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A board that won no slot is still visited for reclaim — never a frozen tick."""
    kb.create_board("high")
    kb.create_board("low")
    _seed("high", 8, priority=900000)

    with kbc.connect(board="low") as conn:
        stale = kb.create_task(conn, title="stale", assignee="alice")
        host = kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, stale, claimer=f"{host}:deadworker")
        kbd._set_worker_pid(conn, stale, 999999)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 3600, stale),
        )

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kbd, "_default_spawn", _recorder([]))

    dispatcher = kwd._KanbanDispatcher(kb, _settings(
        max_in_progress=8, max_spawn=8,
        max_spawn_by_board={"high": 8, "low": 2},
    ))
    results = dict(dispatcher.tick_once())

    # The starved board still ran its reclaim pass: the dead worker's claim is
    # retired and recorded, even though the host budget went to "high".
    assert results["low"].reclaimed == 1
    with kbc.connect(board="low") as conn:
        row = conn.execute(
            "SELECT claim_lock, status FROM tasks WHERE id = ?", (stale,)
        ).fetchone()
        kinds = [event.kind for event in kb.list_events(conn, stale)]
    assert row["claim_lock"] != f"{host}:deadworker"
    assert "reclaimed" in kinds


def test_tick_per_board_ceiling_still_bounds_each_board(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """The global list never lets a board exceed its own max_spawn ceiling."""
    kb.create_board("capped")
    _seed("capped", 30, priority=900000)

    monkeypatch.setattr(kbd, "_default_spawn", _recorder([]))

    dispatcher = kwd._KanbanDispatcher(kb, _settings(
        max_in_progress=10, max_spawn=8, max_spawn_by_board={"capped": 3},
    ))
    dispatcher.tick_once()

    assert _running("capped") == 3
