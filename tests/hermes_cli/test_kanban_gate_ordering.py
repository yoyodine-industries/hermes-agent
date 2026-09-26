"""Acceptance test — a card that gates live work must not be starvable behind bulk.

PROVEN CASE (measured on a live kanban board's ``kanban.db``)
------------------------------------------------------------
A card was filed at 21:38:58 by the head of its chain (priority 85, closed
``done`` at 21:39:20) to carry a change to the landing. ``create_task`` gave the
child priority 0 — the BOTTOM lane — so it queued behind the 63 cards already in
the assignee's lane. At 21:39:56 a slot freed and ``_lane_rows`` ran
``ORDER BY priority DESC, created_at ASC``: the slot went to a priority-8 card
instead. The gating card was never dispatched — its ``stranded_in_ready``
diagnostic fired at 22:19:11 (age 2390s > threshold 1800s) and the card was
archived at 22:19:27, never run. The landing happened only because a human filed
a priority-95 replacement, which dispatched in 93s and completed in 203s.

THE RULE THESE TESTS PIN
------------------------
R1  An unset priority is the NORMAL lane (P2 == 1), never the bottom lane.
R2  A chain leg inherits its chain's urgency: it may be raised, never filed
    below the maximum priority along its parent chain.
R3  A card that other cards wait on is dispatched ahead of a card that gates
    nothing.

Test 1 is the evidence line: "a gate-card filed at low priority reaches a worker
without a human filing a high-priority successor".

Every test here FAILS on the unpatched tree and passes once the rule lands; the
red and green receipts are recorded with the change. They assert behaviour,
never the shape of the source, and they build their fixture through the public
``create_task`` / ``dispatch_once`` surface only.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME and kanban home with a clean DB, every assignee spawnable.

    ``HERMES_KANBAN_HOME`` is pinned explicitly on purpose: ``kanban_home()``
    resolves it *ahead of* ``Path.home()`` and is shared across profiles BY
    DESIGN (kanban_db.py:399), so an inherited value -- a kanban worker runs with
    one set -- would point every test at the caller's live board instead of the
    throwaway one.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home / "kanban"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    from hermes_cli import kanban_db as kb
    kb.init_db()
    assert str(kb.kanban_db_path()).startswith(str(tmp_path)), "fixture leaked off tmp_path"
    return kb


def _fake_spawn(*_args, **_kwargs):
    """Stand-in for the worker spawn — returns a fake PID."""
    return 4242


def _dispatch_one_slot(spawn_fn=_fake_spawn, **kwargs):
    """One tick with exactly ONE free slot, so the ordering rule alone decides."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect() as conn:
        return kbd.dispatch_once(
            conn, spawn_fn=spawn_fn, dry_run=False, max_in_progress=1, **kwargs,
        )


def test_chain_leg_filed_at_priority_zero_wins_the_only_free_slot(kanban_home):
    """R2 — the proven case. The leg carrying the landing is the NEWEST card in
    its lane and is filed at priority 0; it must still outrank ordinary work on
    an urgent chain, because the chain head's urgency is the chain's urgency."""
    kb = kanban_home
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        bulk = [
            kb.create_task(conn, title="bulk %d" % i, assignee="alice", priority=3)
            for i in range(4)
        ]
        head = kb.create_task(conn, title="chain head", assignee="alice", priority=85)
        kb.complete_task(conn, head, summary="head closed; the landing is still owed")
        leg = kb.create_task(
            conn, title="land it", assignee="alice", priority=0, parents=[head],
        )
        assert leg not in bulk

    res = _dispatch_one_slot()

    assert res.spawned, (
        "one free slot existed and nothing was dispatched: %r" % (res,)
    )
    assert res.spawned[0][0] == leg, (
        "the only free slot went to %s, not to the landing leg %s. The leg was "
        "filed at priority 0 under a chain head at priority 85 and inherited "
        "nothing, so it queued behind all %d bulk cards and behind every older "
        "card in its lane." % (res.spawned[0][0], leg, len(bulk))
    )


def test_unset_priority_is_the_normal_lane_not_the_bottom_lane(kanban_home):
    """R1 — ``tasks.priority DEFAULT 0`` and the filing defaults put every card
    filed without an explicit priority in the BOTTOM lane of
    ``ORDER BY priority DESC``. The schema's own contract documents
    "unset = P2 (normal)"; the code contradicts it."""
    kb = kanban_home
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="filed without a priority", assignee="alice")
        row = conn.execute("SELECT priority FROM tasks WHERE id = ?", (tid,)).fetchone()

    assert row["priority"] == 1, (
        "a card filed without a priority landed in lane %r. Lane 0 is the bottom "
        "of the dispatch order, so an omission silently means 'least urgent on "
        "the board'; unset must mean the normal lane (P2 == 1)." % (row["priority"],)
    )


def test_a_card_other_cards_wait_on_dispatches_ahead_of_older_bulk(kanban_home):
    """R3 — a card that other cards are blocked on gates live work, and that is
    a dispatch-ranking fact the board already carries in its links."""
    kb = kanban_home
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        bulk = kb.create_task(
            conn, title="older bulk, higher priority", assignee="alice", priority=1,
        )
        gate = kb.create_task(conn, title="gate: live work waits here", assignee="alice", priority=0)
        waiting = kb.create_task(
            conn, title="blocked on the gate", assignee="bob", priority=0, parents=[gate],
        )
        assert gate != bulk and waiting != gate

    res = _dispatch_one_slot()

    assert res.spawned, "one free slot existed and nothing was dispatched: %r" % (res,)
    assert res.spawned[0][0] == gate, (
        "the only free slot went to %s, not to the gate %s that another card is "
        "waiting on — a card that gates work was outranked by a leaf."
        % (res.spawned[0][0], gate)
    )
