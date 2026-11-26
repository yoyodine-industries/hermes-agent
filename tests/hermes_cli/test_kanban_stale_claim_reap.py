"""A claim with no liveness evidence is REAPED, not left holding a dispatch slot.

Card t_d7105215 (2026-11-25). MEASURED on board ``platform``: 2,187 ready cards for
``platform-coder`` and six ``running`` ones, three of them holding claims over a day and a
half old with no heartbeat; nothing reaped them, and new ready work (including a card
promoted to the top of its band) could not be claimed. One ``hermes kanban reclaim``
freed a slot and a waiting card moved ``ready -> running`` within 60 seconds.

The reclaim paths between them did not cover the shape: ``release_stale_claims`` selected
only ``claim_expires IS NOT NULL AND claim_expires < now``, so a claim with NO lease was
invisible to it; ``detect_crashed_workers`` needs a recorded pid that is PROVEN dead;
``reconcile_orphaned_running`` defers while the recorded pid reads alive-or-unprovable. A
running card whose worker stopped heartbeating therefore held its slot forever whenever
the recorded pid was still occupied — by a recycled number, or by a wedged process.

These tests pin the line the kernel already states for a live-but-silent worker
(``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS``, the #29747 backstop) and extend it to the
claim that carries no lease at all:

* conclusive silence (a heartbeat older than the ceiling, or none at all and a claim older
  than it) ... REAP, and the ``reclaimed`` event names the evidence (``reason``);
* a live worker still heartbeating ... KEEP, and a claim missing its lease gets one back
  (``claim_lease_restored``) instead of stalling or being killed;
* a ready card ... ``promote --priority`` raises it (the ordering lever must work on the
  state a starving card is actually in).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]


def _absent_pid(base: int = 3_700_000) -> int:
    import psutil
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


def _alive(pid: int) -> bool:
    import psutil
    try:
        return psutil.Process(pid).is_running()
    except psutil.Error:
        return False


class _Board:
    """A scratch board store: tasks, runs, and the events a claim leaves behind."""

    def __init__(self, root: Path):
        self.path = root / "kanban.db"
        kbc.init_db(self.path)
        self.conn = kbc.connect(self.path)

    def close(self) -> None:
        self.conn.close()

    def running(
        self,
        tid: str = "t_hold",
        *,
        lease: str = "missing",          # missing | expired | fresh
        worker_pid=None,
        worker_fingerprint=None,
        heartbeat_age=None,              # seconds ago, None = never
        run_age_seconds: int = 43 * 3600,
        lock_host=None,
    ) -> int:
        """A ``running`` card whose claim carries the evidence the caller names."""
        pid = _absent_pid() if worker_pid is None else worker_pid
        lock = f"{lock_host or HOST}:{os.getpid() if worker_pid is None else 4242}"
        expires = {"missing": None, "expired": NOW - 600, "fresh": NOW + 600}[lease]
        started = NOW - run_age_seconds
        run_id = self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'default', 'running', ?, ?, ?, ?)",
            (tid, lock, expires, worker_pid, started),
        ).lastrowid
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, claim_expires, "
            "worker_pid, worker_started_at, last_heartbeat_at, current_run_id, started_at, priority) "
            "VALUES (?, ?, 'default', 'running', ?, ?, ?, ?, ?, ?, ?, ?, 5)",
            (tid, tid, NOW - 86400, lock, expires, worker_pid, worker_fingerprint,
             (NOW - heartbeat_age) if heartbeat_age is not None else None,
             int(run_id), started),
        )
        self.event(tid, int(run_id), "claimed", {"lock": lock, "expires": expires}, started)
        self.event(tid, int(run_id), "spawned",
                   {"pid": worker_pid, "started_at": worker_fingerprint} if worker_pid else {},
                   started)
        self.conn.commit()
        return int(run_id)

    def event(self, tid: str, run: int, kind: str, payload: dict, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (tid, run, kind, json.dumps(payload), ts))

    def ready(self, tid: str, *, priority: int = 5) -> None:
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, priority) "
            "VALUES (?, ?, 'default', 'ready', ?, ?)", (tid, tid, NOW - 86400, priority))
        self.conn.commit()

    def state(self, tid: str) -> dict:
        return dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())

    def events(self, tid: str, kind: str | None = None) -> list:
        sql = "SELECT kind, payload FROM task_events WHERE task_id = ?"
        params: tuple = (tid,)
        if kind:
            sql += " AND kind = ?"
            params += (kind,)
        return [dict(kind=k, **json.loads(p)) for k, p in self.conn.execute(sql + " ORDER BY id", params)]


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    scratch = _Board(tmp_path)
    yield scratch
    scratch.close()


@pytest.fixture
def worker():
    """A REAL live child process standing in for a worker, with its spawn fingerprint.

    The reclaim paths signal a live worker, so the test must never point them at pytest's own
    pid: this child is the thing that gets killed, exactly as a wedged worker would be.
    """
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield proc.pid, kbd._process_fingerprint(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


# ---- the hole: a claim with no lease and no heartbeat is invisible to the reaper ----------

def test_a_claim_with_no_lease_behind_a_silent_live_pid_is_reaped(board, worker):
    """The incident shape: a recorded pid still occupied, no heartbeat for hours, no lease."""
    pid, _ = worker
    board.running(
        "t_hold", lease="missing", worker_pid=pid,
        worker_fingerprint=kbd.UNVERIFIED_WORKER_FINGERPRINT,  # identity never captured
        heartbeat_age=5 * 3600,
    )
    assert kb.release_stale_claims(board.conn) == 1
    row = board.state("t_hold")
    assert (row["status"], row["claim_lock"], row["worker_pid"], row["current_run_id"]) == (
        "ready", None, None, None)
    reap = board.events("t_hold", "reclaimed")
    assert len(reap) == 1
    assert reap[0]["reason"] == "claim_without_lease"
    assert reap[0]["heartbeat_age_seconds"] >= 5 * 3600
    assert reap[0]["heartbeat_stale"] is True
    # An unprovable pid is never signalled; the claim is released on the silence alone, and the
    # process (a stranger, as far as this host can prove) is left running.
    assert reap[0]["termination_attempted"] is False
    assert reap[0]["released_despite_live_pid"] is True
    assert _alive(pid)  # the stranger's process was left alone


def test_a_claim_with_no_lease_and_no_heartbeat_ever_is_reaped_once_the_claim_is_old(board):
    board.running("t_hold", lease="missing", heartbeat_age=None, run_age_seconds=3 * 3600)
    assert kb.release_stale_claims(board.conn) == 1
    reap = board.events("t_hold", "reclaimed")[0]
    assert reap["reason"] == "claim_without_lease_and_heartbeat"
    assert reap["claim_age_seconds"] >= 3 * 3600


def test_a_wedged_live_worker_past_the_heartbeat_ceiling_is_reaped_with_a_fresh_lease(board, worker):
    """The #29747 backstop is not reachable today when the lease was extended before it wedged."""
    pid, fingerprint = worker
    board.running("t_hold", lease="fresh", worker_pid=pid,
                  worker_fingerprint=fingerprint, heartbeat_age=2 * 3600)
    assert kb.release_stale_claims(board.conn) == 1
    reap = board.events("t_hold", "reclaimed")[0]
    assert reap["reason"] == "heartbeat_silent"
    assert board.state("t_hold")["status"] == "ready"


# ---- the other side of the line: a live, heartbeating worker keeps its claim ---------------

def test_a_claim_missing_its_lease_under_a_live_heartbeating_worker_gets_its_lease_back(board, worker):
    pid, fingerprint = worker
    board.running("t_hold", lease="missing", worker_pid=pid,
                  worker_fingerprint=fingerprint, heartbeat_age=10)
    assert kb.release_stale_claims(board.conn) == 0
    row = board.state("t_hold")
    assert row["status"] == "running" and row["claim_expires"] > NOW
    assert [e["reason"] for e in board.events("t_hold", "claim_lease_restored")] == [
        "claim carried a live worker with no lease"]


def test_a_claim_with_a_fresh_heartbeat_is_not_reaped_even_if_the_pid_is_gone(board):
    """A worker can heartbeat from a NEW pid while the recorded one is gone: silence, not death, is
    the signal, so a fresh heartbeat always wins the tick — and the missing lease is REPAIRED."""
    board.running("t_hold", lease="missing", heartbeat_age=30, run_age_seconds=600)
    assert kb.release_stale_claims(board.conn) == 0
    row = board.state("t_hold")
    assert row["status"] == "running" and row["claim_expires"] > NOW


def test_an_expired_lease_under_a_live_heartbeating_worker_is_still_extended(board, worker):
    pid, fingerprint = worker
    board.running("t_hold", lease="expired", worker_pid=pid,
                  worker_fingerprint=fingerprint, heartbeat_age=30)
    assert kb.release_stale_claims(board.conn) == 0
    row = board.state("t_hold")
    assert row["status"] == "running" and row["claim_expires"] > NOW


# ---- promote: the ordering lever must reach a READY card -----------------------------------

def test_promote_raises_a_ready_cards_priority(board):
    board.ready("t_starve", priority=890003)
    ok, err = kb.promote_task(
        board.conn, "t_starve", actor="test", priority=890010)
    assert (ok, err) == (True, None)
    assert board.state("t_starve")["priority"] == 890010
    body = board.events("t_starve", "promoted_manual")[0]
    assert body["already_ready"] is True and body["priority"] == 890010


def test_promote_on_a_ready_card_without_a_priority_is_an_idempotent_success(board):
    board.ready("t_starve", priority=890003)
    assert kb.promote_task(board.conn, "t_starve", actor="test") == (True, None)
    assert board.state("t_starve")["priority"] == 890003


def test_promote_still_moves_a_todo_card_and_applies_the_priority(board):
    board.ready("t_todo", priority=500)
    board.conn.execute("UPDATE tasks SET status = 'todo' WHERE id = 't_todo'")
    board.conn.commit()
    ok, err = kb.promote_task(board.conn, "t_todo", actor="test", priority=890500)
    assert (ok, err) == (True, None)
    row = board.state("t_todo")
    assert row["status"] == "ready" and row["priority"] == 890500


def test_promote_still_refuses_a_RUNNING_card(board):
    board.running("t_live", lease="fresh")
    ok, err = kb.promote_task(board.conn, "t_live", actor="test", priority=890500)
    assert ok is False and "promote only applies" in err


def test_the_promote_verb_ACCEPTS_a_priority_flag():
    """The lever has to exist on the COMMAND, not only in the kernel: `promote --priority N`."""
    import argparse

    from hermes_cli import kanban_parser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    kanban_parser.build_parser(sub)
    args = parser.parse_args(["kanban", "promote", "t_starve", "--priority", "890010"])
    assert args.priority == 890010
    assert "t_starve" in (getattr(args, "task_id", None) or "")
