"""The top-level-only gate: the sentinel is READ, and both doors PARK.

Contract under test (ruling t_efa908e8; implemented by t_0d8f71ae):

* a card whose TITLE or BODY declares ``GATE-TOP-LEVEL-ONLY`` (line-leading,
  case-insensitive) must never be born, or spawned, dispatchable;
* the marker is PRECISE: a mid-line QUOTE of the token (this ruling's own card
  and ``t_1f28a622`` both quote it in prose) must NOT fire;
* the CREATE door PARKS such a card ``scheduled`` (due_at NULL) at birth -- it is
  never refused, because proposing work is legal;
* the RUN door -- which re-checks the card's LIVE text, so an amendment after
  filing is caught -- refuses the spawn, writes ONE deduped event + ONE comment,
  and PARKS the card; the hold is named on the tick's suppression line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_top_level_gate as tlg


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nobody")
    kb.init_db()
    return home


# --- the clause: precision ---------------------------------------------------

@pytest.mark.parametrize("title,body", [
    ("GATE-TOP-LEVEL-ONLY: run the gateway reload", ""),          # title-leading
    ("", "GATE-TOP-LEVEL-ONLY: run the gateway reload"),          # body-leading
    ("", "Intro line\n   GATE-TOP-LEVEL-ONLY: indented body line"),  # indented line
    ("", "gate-top-level-only: lowercase fires"),                 # case-insensitive
    ("", "GATE-TOP-LEVEL-ONLY"),                                   # bare token
])
def test_the_marker_fires(title, body):
    assert tlg.declares(title, body) is True


@pytest.mark.parametrize("title,body", [
    # the measured precision case: the token QUOTED mid-line is prose, not a park.
    ("Enforce GATE-TOP-LEVEL-ONLY at the create + dispatch doors", ""),
    ("", "A card declares `GATE-TOP-LEVEL-ONLY:` as line 1, but nothing reads it."),
    ("", "the sentinel GATE-TOP-LEVEL-ONLY is now enforced"),
    ("Rotate the compressor bloom filters", "nothing to see here"),
    ("", ""),
    (None, None),
])
def test_a_mid_line_quote_does_not_fire(title, body):
    assert tlg.declares(title, body) is False


def test_bytes_body_never_raises():
    # A real board store can hold a body as BLOB; the clause must coerce, not crash.
    assert tlg.declares(b"GATE-TOP-LEVEL-ONLY: top level", None) is True
    assert tlg.declares(None, b"just prose") is False


def test_park_comment_names_the_routes():
    msg = tlg.park_comment("t_x")
    assert "TOP-LEVEL-ONLY HELD" in msg and "t_x" in msg
    assert "hermes peer dm yoyodine/default" in msg
    assert "no_agent" in msg and "unblock" in msg


# --- the CREATE door ---------------------------------------------------------

def test_create_door_parks_a_marker_card(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="GATE-TOP-LEVEL-ONLY: needs a top-level run",
            body="the whole DoD needs the gateway process", assignee="default",
        )
        row = conn.execute(
            "SELECT status, due_at FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["status"] == "scheduled", "born parked, never dispatchable"
        assert row["due_at"] is None, "waiting on a human, not on time"
        ev = conn.execute(
            "SELECT count(*) AS n FROM task_events WHERE task_id = ? "
            "AND kind = 'top_level_gate_parked'", (tid,)).fetchone()["n"]
        assert ev == 1, "the park is recorded on the card"


def test_create_door_does_not_refuse_the_filing(kanban_home):
    """Proposing work is legal: the card EXISTS, it is only PARKED."""
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="plain title",
            body="GATE-TOP-LEVEL-ONLY:\nbody declares it", assignee="default",
        )
        assert conn.execute(
            "SELECT id FROM tasks WHERE id = ?", (tid,)).fetchone()["id"] == tid
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] == "scheduled"


def test_create_door_leaves_a_plain_card_ready(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="an ordinary card", body="no marker", assignee="default")
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] == "ready"


def test_create_door_does_not_park_a_todo_card(kanban_home):
    """Only a card that WOULD have been ``ready`` is parked; one gated behind a
    parent stays ``todo`` (its parent promotes it later, when the RUN door holds it)."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="open parent", assignee="default")
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (parent,))
        conn.commit()
        tid = kb.create_task(
            conn, title="GATE-TOP-LEVEL-ONLY: child", assignee="default", parents=[parent])
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] == "todo"


# --- the RUN door ------------------------------------------------------------

def _spawn_recorder(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242
    return fake_spawn


def _ready_marker_card(conn, title="plain"):
    """A card that is READY and carries the marker -- the amendment case."""
    tid = kb.create_task(conn, title=title, body="no marker yet", assignee="default")
    conn.execute(
        "UPDATE tasks SET title = ?, status = 'ready' WHERE id = ?",
        ("GATE-TOP-LEVEL-ONLY: needs a top-level run", tid),
    )
    conn.commit()
    return tid


def _held_events(conn, tid):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'top_level_only_held' "
        "ORDER BY id", (tid,)).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def test_run_door_holds_and_parks(kanban_home, all_assignees_spawnable):
    spawns = []
    with kbc.connect() as conn:
        tid = _ready_marker_card(conn)
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(spawns))
        assert res.spawned == []
        assert res.skipped_top_level_only == [tid]
        row = conn.execute(
            "SELECT status, due_at FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["status"] == "scheduled" and row["due_at"] is None
        assert len(_held_events(conn, tid)) == 1
        comments = conn.execute(
            "SELECT count(*) AS n FROM task_comments WHERE task_id = ?", (tid,)).fetchone()["n"]
        assert comments == 1
    assert spawns == [], "a worker must never be spawned for a marker card"


def test_run_door_passes_a_plain_card(kanban_home, all_assignees_spawnable):
    spawns = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="an ordinary card", assignee="default")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(spawns))
        assert [t for t, _a, _w in res.spawned] == [tid]
        assert res.skipped_top_level_only == []


def test_run_door_dedupe_across_a_rehold(kanban_home, all_assignees_spawnable):
    """The guard runs every tick; ONE event + ONE comment per hold episode."""
    with kbc.connect() as conn:
        tid = _ready_marker_card(conn)
        kbd.dispatch_once(conn, spawn_fn=_spawn_recorder([]))
        # re-admit by hand (a lane unblocked it): the hold must not spam the board
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        kbd.dispatch_once(conn, spawn_fn=_spawn_recorder([]))
        assert len(_held_events(conn, tid)) == 1
        assert conn.execute(
            "SELECT count(*) AS n FROM task_comments WHERE task_id = ?", (tid,)).fetchone()["n"] == 1


def test_run_door_dry_run_writes_nothing(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        tid = _ready_marker_card(conn)
        res = kbd.dispatch_once(conn, dry_run=True)
        assert res.skipped_top_level_only == [tid]
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] == "ready"
        assert _held_events(conn, tid) == []
        assert conn.execute(
            "SELECT count(*) AS n FROM task_comments WHERE task_id = ?", (tid,)).fetchone()["n"] == 0


def test_suppression_line_names_the_hold():
    res = kbd.DispatchResult()
    res.skipped_top_level_only.append("t_x")
    assert "skipped_top_level_only=1" in kbd.describe_suppression([res])
