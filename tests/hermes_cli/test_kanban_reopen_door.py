"""The done->live DOOR: ``kanban_db.reopen_task`` (design t_539913e4).

A card that was closed before its item was verified is a state the flow must be able to
undo - and the estate had NO CLI path for it (only the dashboard's private
``_set_status_direct`` and forbidden raw SQL). ``reopen_task`` is that door: ``done`` /
``archived`` -> a live status, with the done->live invariant (a live parent retracts its
closed descendants) applied by the SAME helper the dashboard path uses.

These pin:
* the guard - ONLY ``done`` / ``archived`` may be reopened; a live card is refused,
* the landing - re-gate on parents (``ready`` / ``todo``), or an explicit ``blocked``,
* the parked policy - a ``blocked`` landing is STICKY (``recompute_ready`` must NOT
  promote it straight back), the shape the SDLC repair depends on,
* the done->live invariant - closed descendants are retracted,
* ``dry_run`` validates and writes nothing, and
* the CLI surface (``hermes kanban reopen``) reaches the same function.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _done_card(conn, **kw):
    tid = kb.create_task(conn, title=kw.pop("title", "card"), assignee="builder", **kw)
    assert kb.complete_task(conn, tid, result="done")
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == "done"
    return tid


def _status(conn, tid: str) -> str:
    task = kb.get_task(conn, tid)
    assert task is not None
    return task.status


def test_reopen_done_card_lands_ready_and_clears_completion(conn):
    tid = _done_card(conn, title="closed too early")

    ok, err, info = kb.reopen_task(conn, tid, actor="flow", reason="repair")
    assert ok and err is None, (ok, err)
    assert info == {"from_status": "done", "to_status": "ready"}

    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "ready"
    assert task.completed_at is None
    assert task.current_run_id is None

    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "reopened" in kinds
    reopened = [e for e in kb.list_events(conn, tid) if e.kind == "reopened"][0]
    assert reopened.payload["actor"] == "flow"
    assert reopened.payload["reason"] == "repair"
    # The legacy `status` event the dashboard live feed and the sweep read.
    status_events = [e for e in kb.list_events(conn, tid) if e.kind == "status"]
    assert status_events and status_events[-1].payload["status"] == "ready"


def test_reopen_refuses_a_live_card(conn):
    tid = kb.create_task(conn, title="still live", assignee="builder")
    assert _status(conn, tid) == "ready"
    ok, err, info = kb.reopen_task(conn, tid, actor="flow")
    assert not ok and "done" in (err or "") and info == {}
    assert _status(conn, tid) == "ready"

    # ... and a running card, whose resume verb is none of these either.
    kb.claim_task(conn, tid)
    assert _status(conn, tid) == "running"
    ok, err, _ = kb.reopen_task(conn, tid, actor="flow")
    assert not ok and "done" in (err or "")
    assert _status(conn, tid) == "running"


def test_reopen_blocked_landing_is_sticky(conn):
    """The parked policy: `blocked` is what the SDLC repair lands on. If the recompute
    promoted it back to `ready`, the repair would re-dispatch an execution worker onto
    finished work every tick - exactly the churn the policy exists to avoid."""
    tid = _done_card(conn, title="parked")

    ok, err, info = kb.reopen_task(
        conn, tid, actor="flow", reason="r", dest_status="blocked", block_kind="external",
    )
    assert ok and err is None, (ok, err)
    assert info["to_status"] == "blocked"
    assert _status(conn, tid) == "blocked"
    task = kb.get_task(conn, tid)
    assert task is not None and task.block_kind == "external"

    # A later recompute (any dispatch tick) must leave the park intact.
    kb.recompute_ready(conn)
    assert _status(conn, tid) == "blocked"


def test_reopen_regates_to_todo_while_a_parent_is_open(conn):
    parent = kb.create_task(conn, title="open parent", assignee="planner")
    tid = kb.create_task(conn, title="child", assignee="builder", parents=[parent])
    # Direct-SQL close: the parent gate refuses a normal complete, which is exactly the
    # stranded state (a done child under an open parent) a reopen has to re-gate.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))

    ok, _err, info = kb.reopen_task(conn, tid, actor="flow")
    assert ok and info["to_status"] == "todo"
    assert _status(conn, tid) == "todo"

    # Once the parent finishes, the ordinary recompute re-gates it to ready.
    assert kb.complete_task(conn, parent, result="done")
    kb.recompute_ready(conn)
    assert _status(conn, tid) == "ready"


def test_reopen_retracts_closed_descendants(conn):
    parent = _done_card(conn, title="live ancestor")
    child = kb.create_task(conn, title="child", assignee="builder", parents=[parent])
    assert kb.complete_task(conn, child, result="done")

    ok, err, _ = kb.reopen_task(conn, parent, actor="operator")
    assert ok and err is None
    child_task = kb.get_task(conn, child)
    assert child_task is not None and child_task.status == "todo"
    inval = [e for e in kb.list_events(conn, child) if e.kind == "descendant_invalidated"]
    assert len(inval) == 1 and inval[0].payload["ancestor"] == parent


def test_reopen_dry_run_writes_nothing(conn):
    tid = _done_card(conn, title="untouched")
    before = [e.kind for e in kb.list_events(conn, tid)]

    ok, err, info = kb.reopen_task(conn, tid, actor="flow", dry_run=True)
    assert ok and err is None and info["to_status"] == "ready"
    assert _status(conn, tid) == "done"
    assert [e.kind for e in kb.list_events(conn, tid)] == before


def test_reopen_archived_card(conn):
    tid = _done_card(conn, title="archived")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (tid,))
    ok, err, info = kb.reopen_task(conn, tid, actor="flow")
    assert ok and err is None and info == {"from_status": "archived", "to_status": "ready"}
    assert _status(conn, tid) == "ready"


def test_reopen_rejects_an_unknown_landing(conn):
    tid = _done_card(conn, title="bad landing")
    ok, err, _ = kb.reopen_task(conn, tid, actor="flow", dest_status="review")
    assert not ok and "dest_status" in (err or "")
    assert _status(conn, tid) == "done"


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


def _reopen_ns(task_id, *, reason=None, to=None, block_kind=None,
               dry_run=False, as_json=False):
    return argparse.Namespace(task_id=task_id, reason=reason, to=to,
                              block_kind=block_kind, dry_run=dry_run, json=as_json)


def test_cli_reopen_reaches_the_same_function(kanban_home, capsys):
    """The CLI verb is the sanctioned surface the flow drives; it must not be a second
    implementation. `--dry-run` proves the wiring without mutating."""
    from hermes_cli import kanban as kb_cli

    with kbc.connect() as conn:
        tid = _done_card(conn, title="cli card")

    assert kb_cli._cmd_reopen(
        _reopen_ns(tid, reason="repair", to="blocked", block_kind="external")) == 0
    out = capsys.readouterr().out
    assert tid in out
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "blocked"

    # --dry-run validates and writes nothing.
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
    assert kb_cli._cmd_reopen(_reopen_ns(tid, dry_run=True)) == 0
    with kbc.connect() as conn:
        assert _status(conn, tid) == "done"
