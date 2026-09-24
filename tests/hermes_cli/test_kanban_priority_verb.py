"""`hermes kanban priority` — the priority tiebreaker is settable after creation.

Before this verb the only writer was the dashboard's PATCH route, so an operator
holding a board-wide re-prioritisation had no CLI mechanic for it (the verb list
ran create…archive with nothing that touched ``priority``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

ROOT = Path(__file__).parents[2]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _row(conn, task_id):
    return conn.execute(
        "SELECT status, assignee, priority FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()


def test_priority_verb_moves_the_tiebreaker_without_touching_the_lane(kanban_home):
    """The row and its audit trail agree, and nothing else about the card moves:
    priority re-orders who the dispatcher claims first, it does not make a card
    ready, park one, or reassign it."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="sweep me", assignee="alice")
        before = _row(conn, tid)

    kc.run_slash(f"priority {tid} 3")

    with kbc.connect_closing() as conn:
        after = _row(conn, tid)
        events = [
            r["payload"]
            for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'reprioritized'",
                (tid,),
            )
        ]
    assert after["priority"] == 3
    assert (after["status"], after["assignee"]) == (before["status"], before["assignee"])
    assert events, "the change is recorded on the audit trail"
    assert '"priority": 3' in events[-1]


def test_priority_verb_takes_a_bulk_id_list(kanban_home):
    with kbc.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"card {i}", assignee="alice") for i in range(3)]

    kc.run_slash(f"priority {ids[0]} 2 --ids {ids[1]} {ids[2]}")

    with kbc.connect_closing() as conn:
        assert [_row(conn, i)["priority"] for i in ids] == [2, 2, 2]


def test_priority_verb_covers_a_claimed_card_and_refuses_an_unknown_id(kanban_home):
    """A claimed card is re-prioritisable (it applies on the next dispatch, like the
    model override), and an unknown id changes nothing."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="running", assignee="alice")
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))

    kc.run_slash(f"priority {tid} 3")
    with kbc.connect_closing() as conn:
        assert _row(conn, tid)["priority"] == 3

    out = kc.run_slash("priority t_00000000 3")
    assert "t_00000000" in out, "the refusal names the id that did not resolve"


def test_priority_verb_runs_as_a_real_process(kanban_home):
    """End-to-end through `hermes kanban`, not just the in-process slash entry."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="e2e", assignee="alice")

    env = os.environ.copy()
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "priority", tid, "3"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, check=False, timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    with kbc.connect_closing() as conn:
        assert _row(conn, tid)["priority"] == 3
