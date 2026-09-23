"""The belt enqueue path: every block kind queues one row, post-commit.

Contract under test (maintenance-framework design sections 1.1-1.3):

- ``block_task`` fires ``kanban_task_blocked`` AFTER the write txn commits, for
  EVERY kind — the dependency kind included (it used to fire inside the txn).
- A first-party lifecycle observer (``hermes_cli.observability``) appends one
  row to ``belt.db`` and returns: enqueue-only, no board write, no routing.
- The queue coalesces on STATE: ``(task_id, state_fingerprint)`` is unique, so a
  re-fire over an unchanged card does not queue a second disposition.
- A queue write failure can never break the block, and the board DB is never
  left holding a lock the enqueue took.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from hermes_cli import belt_queue
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.plugins import get_plugin_manager

BLOCK_KINDS = ("dependency", "needs_input", "capability", "transient")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The worker that runs this suite pins a live board in the environment; a
    # test must never reach it.
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.init_db()
    return home


@pytest.fixture
def captured_hooks(monkeypatch):
    """Capture the object that reaches a plugin hook on ``kanban_task_blocked``."""
    mgr = get_plugin_manager()
    events: list[tuple[str, dict]] = []
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    mgr._hooks.setdefault("kanban_task_blocked", []).append(
        lambda _h="kanban_task_blocked", **kw: events.append((_h, kw))
    )
    try:
        yield events
    finally:
        mgr._hooks = saved


def _bytes(conn) -> list[dict]:
    """Every queued belt row, oldest first (a separate connection, like the dispatcher)."""
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute("SELECT * FROM belt_queue ORDER BY id")]


def _belt_rows() -> list[dict]:
    path = belt_queue.belt_db_path()
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    try:
        return _bytes(conn)
    finally:
        conn.close()


def _clear_belt() -> None:
    path = belt_queue.belt_db_path()
    if not path.exists():
        return
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DELETE FROM belt_queue")
        conn.commit()
    finally:
        conn.close()


def _block(conn, task_id: str, kind: str, reason: str = "waiting"):
    """Claim then block ``task_id``; the claim is what makes it blockable."""
    kb.claim_task(conn, task_id)
    assert kb.block_task(conn, task_id, reason=reason, kind=kind) is True


@pytest.mark.parametrize("kind", BLOCK_KINDS)
def test_every_block_kind_enqueues_one_row_post_commit(kanban_home, kind):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title=f"t-{kind}", assignee="platform-worker")
        _block(conn, tid, kind)
        rows = _belt_rows()
    finally:
        conn.close()

    assert len(rows) == 1, f"{kind}: expected exactly one belt row, got {rows}"
    row = rows[0]
    assert row["task_id"] == tid
    assert row["block_kind"] == kind
    assert row["domain"] == belt_queue.domain_for_board(row["board"])
    assert row["status"] == "queued"
    assert row["attempts"] == 0
    assert row["run_id"] is None
    assert row["source_status"] == "ready"
    # The row snapshots the COMMITTED state, so the fingerprint the DAG will
    # recompute is the one the queue recorded.
    expected = "todo" if kind == "dependency" else "blocked"
    assert row["state_fingerprint"] == belt_queue.state_fingerprint(
        task_id=tid, status=expected, block_kind=kind,
        block_recurrences=0 if kind == "dependency" else 1,
        last_failure_error=None,
    )
    assert datetime.fromisoformat(row["enqueued_at"]).tzinfo is not None


def test_dependency_block_fires_the_hook_post_commit(kanban_home, captured_hooks):
    """The dependency kind used to fire inside the txn and skip the post-commit call."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="dep", assignee="platform-worker")
        _block(conn, tid, "dependency")
        durable_status = kb.get_task(conn, tid).status
    finally:
        conn.close()

    fired = [event for event in captured_hooks if event[0] == "kanban_task_blocked"]
    assert len(fired) == 1, "a dependency block must fire the block hook exactly once"
    payload = fired[0][1]
    assert payload["task_id"] == tid
    assert payload["block_kind"] == "dependency"
    assert payload["source_status"] == "ready"
    assert durable_status == "todo"
    # The enqueue ran off durable state: the queue row's fingerprint is the
    # post-block state, not the pre-block one.
    row = _belt_rows()[0]
    assert row["state_fingerprint"] == belt_queue.state_fingerprint(
        task_id=tid, status="todo", block_kind="dependency",
        block_recurrences=0, last_failure_error=None,
    )


def test_refire_over_unchanged_state_does_not_queue_twice(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="platform-worker")
        _block(conn, tid, "capability")
        assert len(_belt_rows()) == 1
        # A re-fire (observer replay, dispatcher retry hint) over the same state
        # coalesces rather than queueing a second disposition.
        belt_queue.enqueue_block(task_id=tid, board=kb.get_current_board(), reason="waiting")
        assert len(_belt_rows()) == 1

        # A NEW cause is a new state, and does queue.
        conn.execute("UPDATE tasks SET last_failure_error = ? WHERE id = ?", ("402 payment required", tid))
        conn.commit()
        belt_queue.enqueue_block(task_id=tid, board=kb.get_current_board(), reason="waiting")
        rows = _belt_rows()
    finally:
        conn.close()

    assert len(rows) == 2
    assert rows[1]["state_fingerprint"] != rows[0]["state_fingerprint"]
    assert rows[1]["last_failure_error"] == "402 payment required"


def test_queue_failure_never_breaks_the_block(kanban_home, monkeypatch):
    """The observer is a notification, not part of the transition's success."""
    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(belt_queue, "_insert", _boom)
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="platform-worker")
        _block(conn, tid, "transient")
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()
    assert _belt_rows() == []


def test_board_stays_writable_after_the_enqueue(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="platform-worker")
        _block(conn, tid, "needs_input")
        # No lock survives the enqueue: the board writer still works on the same
        # connection that raised the block.
        kb.create_task(conn, title="after", assignee="platform-worker")
        conn.commit()
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
    finally:
        conn.close()


def test_observability_dispatch_reaches_the_belt_queue(kanban_home, monkeypatch):
    """The block hook is consumed by a first-party projection, not by a plugin."""
    from hermes_cli import observability
    from hermes_cli.lifecycle import has_hook

    assert has_hook("kanban_task_blocked") is True
    assert observability.handles_hook("kanban_task_blocked") is True
    assert observability.handles_hook("on_session_finalize") is False

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="platform-worker")
        _block(conn, tid, "capability")
    finally:
        conn.close()
    board = kb.get_current_board()

    # Clear what the live hook path queued, then drive the same projection
    # through the dispatch table with a broken sibling ahead of it.
    _clear_belt()
    monkeypatch.setattr(observability, "_PROJECTIONS", ("no_such_projection", "belt"))
    observability.observe_lifecycle(
        "kanban_task_blocked", task_id=tid, board=board, reason="waiting",
    )
    rows = _belt_rows()
    assert len(rows) == 1
    assert rows[0]["task_id"] == tid
    assert observability.handles_hook("kanban_task_blocked") is True


def test_domain_lookup_defaults_and_registry_override(kanban_home):
    assert belt_queue.domain_for_board("ops") == "platform"
    assert belt_queue.domain_for_board("research") == "research"
    assert belt_queue.domain_for_board("financially") == "financially"
    assert belt_queue.domain_for_board("some-unknown-board") == "platform"
    assert belt_queue.domain_for_board(None) == "platform"

    registry = belt_queue.domain_registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps({**belt_queue.DEFAULT_BOARD_DOMAINS, "engines": "platform"}),
        encoding="utf-8",
    )
    assert belt_queue.domain_for_board("ENGINES") == "platform"

    registry.write_text(json.dumps({"ops": "../../etc/passwd"}), encoding="utf-8")
    assert belt_queue.domain_for_board("ops") == "platform"
