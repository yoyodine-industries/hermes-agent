"""A card the unblock-loop breaker PARKED must stay parked — and must keep a
caller-armed time fence.

Measured on the live ops board (card t_2fa42d98, run 1579, 2026-09-21): the trip
wrote ``block_loop_detected`` and :func:`_route_block` parked the card in
``blocked``, and two seconds later the dispatcher promoted it straight back to
``ready`` — a respawn loop. ``recompute_ready`` asks :func:`_has_sticky_block`,
whose event filter only knew ``blocked``/``unblocked``, so the PARK read as "no
park at all" and the card was requeued; ``_resume_status_from_events`` then
answered ``ready``.

The same gate bites the time fence the other way: ``block_task`` armed
``due_at``/``due_window_policy`` only for a plain ``blocked`` landing, so a
caller that passes an explicit ``due_at`` on the call that happens to trip the
breaker got the fence silently dropped and the card parked forever.

Both are pinned here against the dispatcher's own sweep (``recompute_ready``),
not against a hand-built row.
"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _route_to_loop_breaker(conn, task_id, *, due_at=None, window_policy=None):
    """claim -> block(capability) -> unblock -> claim -> block(capability).

    The second same-kind block is the trip; ``due_at``/``window_policy`` ride on
    that call, which is what a worker's ``kanban_block(due_at=...)`` does.
    """
    assert kb.claim_task(conn, task_id) is not None
    assert kb.block_task(conn, task_id, reason="approval row is not root", kind="capability")
    assert kb.unblock_task(conn, task_id)
    assert kb.claim_task(conn, task_id) is not None
    assert kb.block_task(
        conn, task_id, reason="approval row is not root", kind="capability",
        due_at=due_at, window_policy=window_policy,
    )


def test_breaker_park_survives_the_ready_sweep(conn):
    """The pass that re-promoted t_2fa42d98 must leave the park alone."""
    parent = kb.create_task(conn, title="parent of the gated card", assignee="cc-nova")
    tid = kb.create_task(conn, title="gated card", assignee="cc-nova", parents=[parent])
    assert kb.complete_task(conn, parent, summary="done") is True
    kb.recompute_ready(conn)  # the tick that opens a child whose only parent finished
    assert kb.get_task(conn, tid).status == "ready", "the fixture needs an open child"

    _route_to_loop_breaker(conn, tid)

    row = kb.get_task(conn, tid)
    assert row.status == "blocked", "the breaker parks in blocked"
    assert row.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    assert row.due_at is None, "an un-fenced trip is not a due-armed park"

    # The dispatcher tick — the pass that requeued the measured card.
    assert kb.recompute_ready(conn) == 0, "recompute_ready re-promoted a parked card"
    assert kb.get_task(conn, tid).status == "blocked"
    assert kb.claim_task(conn, tid) is None, "a parked card must not be re-dispatched"


def test_breaker_park_keeps_the_callers_time_fence(conn):
    """An explicit ``due_at`` on the tripping call is a caller request, not a hint."""
    tid = kb.create_task(conn, title="fenced card", assignee="cc-nova")
    due = int(time.time()) + 3600

    _route_to_loop_breaker(conn, tid, due_at=due, window_policy="ambient")

    row = kb.get_task(conn, tid)
    assert row.status == "blocked"
    assert row.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    assert row.due_at == due, "the caller's time fence was dropped on the trip"
    assert row.due_window_policy == "ambient"

    trip = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"][-1]
    assert trip.payload["recurrences"] == kb.BLOCK_RECURRENCE_LIMIT
    assert trip.payload["due_at"] == due, "the trip's payload lost the fence"

    # Traced end to end: once the fence passes, the due waker's own read sees it.
    assert [t.id for t in kb.list_due_tasks(conn, now=due + 1, statuses=("blocked",))] == [tid]


def test_plain_block_still_arms_the_fence_and_parks(conn):
    """The non-trip path is unchanged: fence armed, park sticky."""
    tid = kb.create_task(conn, title="plain fenced block", assignee="cc-nova")
    due = int(time.time()) + 600
    assert kb.claim_task(conn, tid) is not None
    assert kb.block_task(conn, tid, reason="waiting on a human", kind="needs_input", due_at=due)

    row = kb.get_task(conn, tid)
    assert (row.due_at, row.due_window_policy) == (due, kb.DEFAULT_DUE_WINDOW_POLICY)
    assert kb.recompute_ready(conn) == 0
    assert kb.unblock_task(conn, tid) is True
    assert kb.get_task(conn, tid).status == "ready"
