"""Regression tests: the retry budget is durable across re-queues.

Measured read-only on the live board stores (2026-09-20, per-board audit of
attempts spent against each card's ceiling): 18 cards had already spent MORE
attempts than their ceiling — 13 on ops, 3 on research, 1 on financially, 1 on
yoyodine-web-services, the worst at 12 attempts against a ceiling of 2 — and six
of the ops cards were sitting ``ready`` with ``consecutive_failures=0``, i.e.
queued for yet another attempt instead of parked at the ceiling. The dispatcher
did book every one of those attempts as a protocol violation — the ledger and the
board agreed on the count — but the trip it eventually fired was undone in the
same tick: the violation budget trips at ``streak >= limit`` while the shared
``streak >= limit`` while the shared counter only reached
``consecutive_failures=1``, so ``recompute_ready`` read ``1 < limit`` and
promoted the card straight back into the queue. The loop was
blocked -> promoted -> respawned, and the card never escalated to a human.

Two invariants:

* a card cannot spend more attempts than its effective retry ceiling, however
  often it is re-queued: the budget is read off the append-only run ledger, not
  off a counter that the re-queue paths (an operator ``unblock``, a
  reassignment) deliberately reset;
* reaching the ceiling is a *documented, final* park: status ``blocked``, a
  typed ``block_kind`` so the disposition sweep escalates the card instead of
  draining an undocumented blocker back into the queue, and ``recompute_ready``
  leaves it alone.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB and no crash grace."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _set_ceiling(conn, tid: str, ceiling: int) -> None:
    conn.execute("UPDATE tasks SET max_retries = ? WHERE id = ?", (ceiling, tid))
    conn.commit()


def _spend_attempt(conn, tid: str) -> None:
    """Spend one attempt exactly the way a dispatcher tick does.

    Claim -> spawn a worker that exits cleanly WITHOUT a terminal kanban call ->
    reap it. ``_record_worker_exit`` with status 0 is the rc=0 the host really
    observes; reaping then books the crash and runs the breaker accounting.
    """
    assert kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:probe")
    dead = subprocess.Popen(["true"])
    dead.wait()
    kbd._set_worker_pid(conn, tid, dead.pid)
    # Rewind past the launch grace window, like a long-running worker.
    conn.execute("UPDATE tasks SET started_at = started_at - 9999 WHERE id = ?", (tid,))
    conn.execute("UPDATE task_runs SET started_at = started_at - 9999 WHERE task_id = ?", (tid,))
    conn.commit()
    kbd._record_worker_exit(dead.pid, 0)
    kbd.detect_crashed_workers(conn)


def _tick(conn, times: int = 3) -> None:
    """The dispatcher's own re-queue pass — must not resurrect a spent card."""
    for _ in range(times):
        assert kb.recompute_ready(conn) == 0


def test_ceiling_trip_is_recorded_and_survives_the_dispatcher_tick(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rc0 without a terminal call")
        _set_ceiling(conn, tid, 2)

        _spend_attempt(conn, tid)

        # D2: the attempt is recorded against the card — rc, duration and the
        # budget it was working against — not discarded with the reaped pid.
        run = conn.execute(
            "SELECT outcome, error, metadata FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        meta = json.loads(run["metadata"])
        assert run["outcome"] == "crashed"
        assert meta["protocol_violation"] is True
        assert meta["exit_code"] == 0
        assert "elapsed_seconds" in meta
        assert kbd._attempts_without_disposition(conn, tid) == 1
        assert kb.get_task(conn, tid).status != "blocked"

        _spend_attempt(conn, tid)

        # The ceiling: blocked, with the counter on the card and a block kind the
        # disposition sweep can escalate (an untyped park reads as undocumented
        # and gets drained).
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.consecutive_failures >= 2
        assert task.block_kind == "needs_input"

        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'gave_up' "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        payload = json.loads(event["payload"])
        assert payload["attempts_without_disposition"] == 2
        assert payload["effective_limit"] == 2

        # …and it holds. On the unfixed dispatcher the trip lands at
        # consecutive_failures=1, this tick promotes the card, and the loop
        # resumes with a 3rd, 4th, nth attempt.
        _tick(conn)
        assert kb.get_task(conn, tid).status == "blocked"


def test_budget_is_spent_once_across_a_deliberate_re_queue(kanban_home: Path) -> None:
    """``unblock_task`` resets ``consecutive_failures`` on purpose — a human
    restarting the cycle. It must not hand the card an unbounded budget."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="re-queued past its ceiling")
        _set_ceiling(conn, tid, 2)
        _spend_attempt(conn, tid)
        _spend_attempt(conn, tid)
        assert kb.get_task(conn, tid).status == "blocked"
        _tick(conn)
        assert kb.get_task(conn, tid).status == "blocked"

        assert kb.unblock_task(conn, tid) is True
        row = conn.execute(
            "SELECT consecutive_failures FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["consecutive_failures"] == 0, "the row counter is meant to restart"

        _spend_attempt(conn, tid)

        # The ledger, not the row counter, decides: the third attempt re-trips
        # against the cumulative count instead of starting a fresh budget.
        _tick(conn)
        assert kb.get_task(conn, tid).status == "blocked"
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'gave_up' "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        payload = json.loads(event["payload"])
        assert payload["attempts_without_disposition"] == 3
        assert payload["effective_limit"] == 2
