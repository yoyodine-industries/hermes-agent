"""D4 — a card parked by the unblock-loop breaker must never land in ``triage``.

``triage`` is the *spec shelf*: a card belongs there only because someone
deliberately parked a rough idea (``create_task(triage=True)`` / the dashboard's
Triage column). The unblock-loop breaker used to route a parked card into that
same column, where ``promote``/``complete``/``unblock`` all refused it — a status
with no supported exit, and the reason the board owner had to move cards with
hand-written SQL (t_29fe8903, t_e0c74c8c, t_49f79fae).

These tests pin both halves of the fix:
  * the breaker PARKS (``blocked``, kind and recurrence count readable on the row)
    instead of stashing the card on the spec shelf;
  * a card that IS in ``triage`` has a documented way out, and whichever refusal
    guard remains names that way out in its message.

``_route_to_loop_breaker`` replays the measured event sequence of live card
t_29fe8903, which tripped this breaker twice on the ops board — 2026-09-17
18:03:08 (`recurrences=2`) and 2026-09-19 12:38:49 (`recurrences=3`). The second
trip re-created the card's `triage` row 14 minutes after the board owner had
drained it, which is the state this file pins: claim -> block(capability) ->
unblock -> claim -> block(capability).
"""

from __future__ import annotations

import argparse

import pytest

from hermes_cli import kanban as kb_cli
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


def _route_to_loop_breaker(conn, task_id):
    """The measured t_29fe8903 sequence: same-kind block, unblock, re-block."""
    assert kb.claim_task(conn, task_id) is not None
    assert kb.block_task(conn, task_id, reason="approval row is not root", kind="capability")
    assert kb.unblock_task(conn, task_id)
    assert kb.claim_task(conn, task_id) is not None
    assert kb.block_task(conn, task_id, reason="approval row is not root", kind="capability")


def test_loop_breaker_parks_blocked_not_triage(conn):
    tid = kb.create_task(conn, title="D4 fixture: approval row is not root", assignee="cc-nova")
    _route_to_loop_breaker(conn, tid)

    row = kb.get_task(conn, tid)
    assert row.status == "blocked", "the loop breaker parks; triage is not a park state"
    # The trip must not lose why the card is sitting there.
    assert row.block_kind == "capability"
    assert row.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT

    trips = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"]
    assert len(trips) == 1
    assert trips[0].payload["recurrences"] == kb.BLOCK_RECURRENCE_LIMIT
    assert trips[0].payload["limit"] == kb.BLOCK_RECURRENCE_LIMIT
    assert trips[0].payload["kind"] == "capability"

    # The park must be releasable by an agent, not only by SQL: a human decision
    # lands, and `unblock` puts the card straight back in the pool.
    assert kb.unblock_task(conn, tid) is True
    assert kb.get_task(conn, tid).status == "ready"


def test_loop_broken_park_is_countable_and_stays_parked(conn):
    tid = kb.create_task(conn, title="D4 fixture", assignee="cc-nova")
    _route_to_loop_breaker(conn, tid)

    # The grooming sweep's own read (kanban_disposition.read_board) is a count out of
    # `tasks WHERE status = 'blocked'` plus the recurrence column; a park nobody can
    # count is still a trap.
    counted = conn.execute(
        "SELECT id, block_recurrences FROM tasks WHERE status = 'blocked'"
    ).fetchall()
    assert [(r["id"], r["block_recurrences"]) for r in counted] == [
        (tid, kb.BLOCK_RECURRENCE_LIMIT),
    ]
    assert kb.board_stats(conn)["by_status"].get("triage", 0) == 0
    assert kb.claim_task(conn, tid) is None, "a parked card must not be silently re-dispatched"


def _promote_ns(task_id, *, ids=None, reason=None, dry_run=False, as_json=False):
    return argparse.Namespace(
        task_id=task_id,
        reason=list(reason or []),
        ids=list(ids or []) or None,
        dry_run=dry_run,
        json=as_json,
    )


def test_triage_card_is_released_by_the_documented_promote_command(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="rough idea, spec'd by hand",
                             assignee="cc-nova", triage=True)
        assert kb.get_task(conn, tid).status == "triage"

    assert kb_cli._cmd_promote(_promote_ns(tid)) == 0

    with kbc.connect_closing() as conn:
        row = kb.get_task(conn, tid)
        assert row.status == "ready"
        assert row.assignee == "cc-nova"      # the exit preserves the assignee
        promoted = [e for e in kb.list_events(conn, tid) if e.kind == "promoted_manual"]
    assert len(promoted) == 1
    assert promoted[0].payload["from_status"] == "triage"


def test_triage_card_can_be_closed_out(conn):
    tid = kb.create_task(conn, title="work already landed", assignee="cc-nova", triage=True)
    assert kb.complete_task(conn, tid, summary="landed on another branch") is True
    assert kb.get_task(conn, tid).status == "done"


def test_unblock_refusal_names_the_supported_exits(kanban_home, capsys, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="rough idea", assignee="cc-nova", triage=True)

    assert kb_cli._cmd_unblock(argparse.Namespace(task_ids=[tid], reason=None)) != 0

    err = capsys.readouterr().err
    assert tid in err
    # The surviving guard must name the door instead of re-closing it.
    assert f"promote {tid}" in err
    assert f"complete {tid}" in err
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
