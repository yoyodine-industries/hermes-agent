"""Regression tests: an unspawnable card reaches its ceiling ONCE and stays parked.

Measured read-only on the ops board 2026-09-16..19: one card whose
``workspace_path`` pointed at a path that is not a git repo root was claimed ->
``spawn_failed`` -> re-queued by the disposition sweep's drain-leaks leg six
times. That re-queue is ``hermes kanban unblock``, which deliberately resets
``consecutive_failures`` for a fresh start — so the counter that had just tripped
the breaker was cleared and the identical, unspawnable spawn ran again, burning a
worker slot every cycle. No retry can clear it: the cause is a fact about what
the card (or its board) RECORDS.

Two invariants:

* a spawn failure no retry can clear is spent on the FIRST attempt — the card
  lands ``blocked`` with the counter AT its effective limit, a typed
  ``block_kind`` (so the disposition sweep escalates it instead of draining it)
  and the ``permanent_spawn_cause`` on the failure the breaker fired;
* the re-queue paths refuse it: ``unblock_task`` — the verb the drain-leaks leg
  drives — writes nothing, while an operator's explicit ``force=True`` still can.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with a fresh kanban DB and no crash grace."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _must_not_spawn(*_args, **_kwargs):
    raise AssertionError("an unspawnable workspace must not reach a spawn")


def _status(conn, tid: str) -> str:
    task = kb.get_task(conn, tid)
    assert task is not None
    return task.status


def _newest_failure_payload(conn, tid: str) -> dict:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('spawn_failed', 'gave_up') ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    return json.loads(row["payload"]) if row is not None and row["payload"] else {}


def test_unspawnable_workspace_parks_once_and_the_requeue_refuses_it(
    kanban_home: Path, tmp_path: Path,
) -> None:
    with kbc.connect() as conn:
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        tid = kb.create_task(
            conn, title="worktree path is not a git repo root",
            assignee="default", workspace_kind="worktree",
            workspace_path=str(not_a_repo),
        )

        # One tick. The spawn fn is the canary: the workspace has to fail BEFORE a
        # worker starts, so a passing run is one that burned no worker slot.
        kbd.dispatch_once(conn, spawn_fn=_must_not_spawn)

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        payload = _newest_failure_payload(conn, tid)
        assert payload["trigger_outcome"] == "spawn_failed"
        assert payload["permanent_spawn_cause"] == "workspace_path"
        # Reached on THIS first attempt, and pinned AT the limit: a counter below
        # it is exactly what ``recompute_ready`` auto-recovers.
        assert task.consecutive_failures >= payload["effective_limit"]
        # The park is documented (a NULL block_kind is the cohort the leak sweep
        # drains) and it names the path a human has to repair.
        assert task.block_kind == "needs_input"
        assert str(not_a_repo) in (task.last_failure_error or "")

        for _ in range(3):
            kb.recompute_ready(conn)
        assert _status(conn, tid) == "blocked"

        # The drain-leaks re-queue, exactly as the ops sweep runs it.
        assert kb.unblock_task(conn, tid) is False
        assert _status(conn, tid) == "blocked"
        assert kb.spawn_failure_cause(conn, tid) == "workspace_path"

        # …and the operator override, once the cause has been repaired, is not a
        # dead end.
        assert kb.unblock_task(conn, tid, force=True) is True
        assert _status(conn, tid) == "ready"


def test_an_unmapped_workspace_error_keeps_its_retry_budget(
    kanban_home: Path, tmp_path: Path,
) -> None:
    """The classification is a FAMILY, not "any workspace error".

    An error the dispatcher does not know to be structural keeps the pre-existing
    behaviour: one attempt spent, the card re-queued, its re-queue paths open.
    """
    occupied = tmp_path / "a-file"
    occupied.write_text("not a directory\n")
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="dir workspace path is a file", assignee="default",
            workspace_kind="dir", workspace_path=str(occupied),
        )
        kbd.dispatch_once(conn, spawn_fn=_must_not_spawn)

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        payload = _newest_failure_payload(conn, tid)
        assert "permanent_spawn_cause" not in payload
        assert kb.spawn_failure_cause(conn, tid) is None

        # …so the re-queue paths are still open for it.
        assert kb.block_task(conn, tid, reason="transient probe", kind="transient") is True
        assert kb.unblock_task(conn, tid) is True
