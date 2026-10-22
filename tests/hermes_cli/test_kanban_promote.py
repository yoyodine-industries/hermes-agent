"""Tests for the kanban `promote` verb (issue #28822).

The realistic bug scenario from #28822 is: a child task ends up in
``todo`` with all its parents already ``done`` (because the
auto-promote daemon hasn't run, or a manual close raced it).
Direct-SQL setup is used to construct that state deterministically.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _stuck_todo(conn, *, parents_done=True, n_parents=1):
    """Build the #28822 scenario: child in 'todo' whose parents may
    have closed as 'done' without the auto-promote logic firing.
    """
    parent_ids = [
        kb.create_task(conn, title=f"parent{i}", assignee="setup")
        for i in range(n_parents)
    ]
    child_id = kb.create_task(
        conn, title="child", parents=parent_ids, assignee="setup"
    )
    assert kb.get_task(conn, child_id).status == "todo"
    if parents_done:
        for pid in parent_ids:
            conn.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (pid,)
            )
    return child_id, parent_ids


def test_promote_stuck_todo_succeeds(conn):
    child, _ = _stuck_todo(conn, parents_done=True)
    ok, err = kb.promote_task(conn, child, actor="tester")
    assert ok and err is None
    assert kb.get_task(conn, child).status == "ready"


def test_promote_refuses_undone_parent_and_names_the_real_remedy(conn):
    # #106195: promotion must never report a 'ready' that the first claim reverts.
    child, (parent,) = _stuck_todo(conn, parents_done=False)
    ok, err = kb.promote_task(conn, child, actor="tester", reason="recovery")
    assert not ok
    assert err and parent in err
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, child) is None  # still gated; nothing pretended




# ---------------------------------------------------------------------------
# CLI `_cmd_promote` — bulk via `--ids` (the issue's anti-respawn use case:
# promote all children of a closed parent in one command).
# ---------------------------------------------------------------------------


def _promote_ns(task_id, *, ids=None, reason=None, force=False,
                dry_run=False, as_json=False):
    return argparse.Namespace(
        task_id=task_id,
        reason=list(reason or []),
        ids=list(ids or []) or None,
        force=force,
        dry_run=dry_run,
        json=as_json,
    )


def test_cli_promote_bulk_ids_promotes_all(kanban_home, capsys):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        children = [
            kb.create_task(conn, title=f"c{i}", parents=[parent])
            for i in range(3)
        ]
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    rc = kb_cli._cmd_promote(_promote_ns(children[0], ids=children[1:]))
    assert rc == 0
    out = capsys.readouterr().out
    for c in children:
        assert c in out
    with kbc.connect() as conn:
        for c in children:
            assert kb.get_task(conn, c).status == "ready"


# ---------------------------------------------------------------------------
# `scheduled` — the third PARKED status (card t_7fb19093).
#
# A card parked in `scheduled` with NO clock fence is a HUMAN hold ("released by
# hand only"), and it used to have no promote door at all: `promote` answered the
# tautology "task is 'scheduled'; promote only applies to 'todo' or 'blocked'".
# The release condition is now judged by the two gates the board can read — the
# parent edges and the clock fence — and a refusal names the gate still standing.
# ---------------------------------------------------------------------------


def _scheduled_child(conn, *, parent_status="done", due_at=None, window_policy=None):
    """A card parked in `scheduled` (optionally clock-fenced) under one parent."""
    parent = kb.create_task(conn, title="parent", assignee="setup")
    child = kb.create_task(conn, title="child", parents=[parent], assignee="setup")
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (parent_status, parent))
    ok = kb.schedule_task(conn, child, reason="waiting on the r1 pull", due_at=due_at,
                          window_policy=window_policy)
    assert ok
    assert kb.get_task(conn, child).status == "scheduled"
    return child, parent


def test_promote_releases_a_scheduled_card_with_no_clock(conn):
    child, _ = _scheduled_child(conn)
    ok, err = kb.promote_task(conn, child, actor="platform-stl",
                              reason="its blocker discharged 2026-10-04")
    assert ok, err
    task = kb.get_task(conn, child)
    assert task.status == "ready"
    # The gate is discharged, so no stale fence may ride along: a passed/absent
    # `due_at` left behind would re-release (or falsely alarm about) a card that is
    # already in the queue — the amnesia `unblock_task` refuses.
    row = conn.execute("SELECT due_at, due_window_policy FROM tasks WHERE id=?",
                       (child,)).fetchone()
    assert row["due_at"] is None and row["due_window_policy"] is None


def test_promote_records_the_scheduled_release_on_the_event_stream(conn):
    child, _ = _scheduled_child(conn)
    ok, err = kb.promote_task(conn, child, actor="platform-stl", reason="discharged")
    assert ok, err
    ev = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='promoted_manual' "
        "ORDER BY id DESC LIMIT 1", (child,),
    ).fetchone()
    assert ev is not None
    payload = json.loads(ev["payload"])
    assert payload["from_status"] == "scheduled"
    assert payload["actor"] == "platform-stl"
    assert payload["cleared_due_at"] is None


def test_promote_refuses_a_scheduled_card_whose_parent_is_still_running(conn):
    child, parent = _scheduled_child(conn, parent_status="running")
    ok, err = kb.promote_task(conn, child, actor="platform-stl")
    assert not ok
    # The refusal names the LIVE GATE, with its status — never the bare tautology.
    assert err and parent in err
    assert "running" in err
    assert kb.get_task(conn, child).status == "scheduled"


def test_promote_refuses_a_scheduled_card_whose_clock_has_not_fired(conn):
    due_at = int(time.time()) + 3600
    child, _ = _scheduled_child(conn, due_at=due_at)
    ok, err = kb.promote_task(conn, child, actor="platform-stl")
    assert not ok
    # The refusal names the fire time and the ONE override, so the caller is routed
    # rather than left to guess at another verb.
    assert err and time.strftime("%Y-%m-%d %H:%M", time.localtime(due_at)) in err
    assert "unblock" in err
    assert kb.get_task(conn, child).status == "scheduled"


def test_promote_dry_run_validates_a_scheduled_release_without_writing(conn):
    child, _ = _scheduled_child(conn)
    ok, err = kb.promote_task(conn, child, actor="platform-stl", dry_run=True)
    assert ok and err is None
    assert kb.get_task(conn, child).status == "scheduled"
    assert not conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='promoted_manual'", (child,)
    ).fetchone()


def test_promote_releases_a_scheduled_card_whose_clock_has_fired(conn):
    due_at = int(time.time()) - 60
    child, _ = _scheduled_child(conn, due_at=due_at, window_policy="defer")
    ok, err = kb.promote_task(conn, child, actor="platform-stl")
    assert ok, err
    assert kb.get_task(conn, child).status == "ready"
    row = conn.execute("SELECT due_at, due_window_policy FROM tasks WHERE id=?",
                       (child,)).fetchone()
    assert row["due_at"] is None and row["due_window_policy"] is None


def test_promote_still_refuses_a_running_card_and_names_its_own_exit(conn):
    card = kb.create_task(conn, title="live", assignee="setup")
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (card,))
    ok, err = kb.promote_task(conn, card, actor="platform-stl")
    assert not ok
    assert err and "running" in err and "exit verb" in err


def test_worker_scope_still_refuses_the_promote_door(kanban_home, monkeypatch):
    """The new door is an OPERATOR door: nothing here widens worker scope.

    `promote` stays in the delegated-child denied vocabulary, and no new
    worker-facing verb was introduced to carry the release (the card expressly
    refuses widening `kanban_schedule` for this).
    """
    assert "promote" in kb_cli._DELEGATED_CHILD_DENIED_ACTIONS
    assert "release" not in kb_cli._HANDLERS
    # ... and the fence really fires for it, judged against the board it targets.
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(kanban_home))
    args = argparse.Namespace(kanban_action="promote", board=None)
    assert kb_cli._is_delegated_child_cli_mutation(args) is True


