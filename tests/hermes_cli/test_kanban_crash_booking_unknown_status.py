"""Acceptance pin for card t_00d036cb — the unknown-exit-status -> crash booking, BOTH arms.

The card's defect: the crash text came from the ``kind == "unknown"`` fallback (no entry in
``_recent_worker_exits`` for that pid — it is fed only by ``os.waitpid(-1, WNOHANG)`` inside the
dispatcher, so an UNREAD exit status and a DEAD process were the same input).  Any case where the
status was not collected therefore produced a crash booking, and a provably LIVE worker was
re-queued and re-run (measured: board task_runs 5590/5591, pid 57501 alive at every sample).

These two arms pin the required behaviour in-process:

  arm 1  status never collected + pid ALIVE      -> HELD, never booked crashed/re-queued
  arm 2  status never collected + pid GONE       -> booked crashed (the legitimate reclaim stands)

Run it against the deployed tree (must pass) and against the pre-fix bytes (arm 1 must fail): that
is what makes it a regression pin rather than a restatement of the code.
"""

import os
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


DEAD_PID = 9_999_999  # never a live process on this host


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _running(conn, pid, fingerprint) -> str:
    """A ``running`` card claimed by a host-local worker whose pid/fingerprint we control."""
    tid = kb.create_task(conn, title="job", assignee="worker")
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid)
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_started_at = ?, started_at = ?, claim_expires = ? WHERE id = ?",
            (fingerprint, old, old, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (old, tid),
        )
    return tid


def _crashed_events(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == "crashed"]


def test_arm1_a_live_pid_with_no_exit_status_is_never_booked_crashed(board):
    """A worker that is provably alive is never booked ``crashed``/re-queued.

    The exit status is never collected for this pid (the reap registry holds nothing for it), which
    is exactly the input that used to produce ``pid N not alive``.  The live pid must hold instead.
    """
    conn = board
    # No exit status was ever collected for os.getpid(): the registry is empty of it.
    kbd._recent_worker_exits.pop(int(os.getpid()), None)
    assert kbd._classify_worker_exit(os.getpid()) == ("unknown", None)
    assert kbd._pid_alive(os.getpid()) is True

    tid = _running(conn, os.getpid(), kbd._process_fingerprint(os.getpid()))

    assert kbd.detect_crashed_workers(conn) == [], "a live worker must not be reaped"

    task = kb.get_task(conn, tid)
    assert task.status == "running", "the card keeps its claim"
    assert task.worker_pid == os.getpid(), "the claim is NOT released beside the live worker"
    assert _crashed_events(conn, tid) == [], "no crash may be recorded for a live worker"


def test_arm1b_an_unverifiable_identity_on_a_live_pid_also_holds(board):
    """The same arm for the two deployed fingerprint shapes that can no longer be compared.

    A bare start tick and the empty-witness ``|<start>`` pair both read UNKNOWN, and UNKNOWN must
    fail OPEN: it is not proof of death.
    """
    conn = board
    for fingerprint in ("179041285600", "|179068367989"):
        kbd._recent_worker_exits.pop(int(os.getpid()), None)
        tid = _running(conn, os.getpid(), fingerprint)
        assert kbd._worker_liveness(os.getpid(), fingerprint) == kbd.WORKER_UNKNOWN
        assert kbd.detect_crashed_workers(conn) == []
        assert kb.get_task(conn, tid).status == "running"
        assert _crashed_events(conn, tid) == []


def test_arm2_a_gone_pid_with_no_exit_status_still_books_a_crash(board):
    """The inverse arm: with the pid PROVEN gone the legitimate reclaim still happens.

    Without this arm the fix could be satisfied by never reclaiming anything.
    """
    conn = board
    kbd._recent_worker_exits.pop(DEAD_PID, None)
    assert kbd._classify_worker_exit(DEAD_PID) == ("unknown", None)

    tid = _running(conn, DEAD_PID, kbd._process_fingerprint(os.getpid()))

    assert kbd.detect_crashed_workers(conn) == [tid]
    assert kb.get_task(conn, tid).status == "ready"
    crashed = _crashed_events(conn, tid)
    assert crashed, "the reclaimed attempt is recorded"
    # The booked crash names the pid. ``exit_kind`` is a field the CURE adds, so it is asserted only
    # when present: the arm must discriminate the reclaim decision, not the payload's enrichment.
    assert crashed[-1].payload.get("pid") == DEAD_PID
    if "exit_kind" in crashed[-1].payload:
        assert crashed[-1].payload["exit_kind"] == "unknown"
