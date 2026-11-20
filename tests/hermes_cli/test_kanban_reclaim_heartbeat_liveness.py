"""The heartbeat is the liveness test; the pid is corroboration (card t_28747981, R1).

Measured on the live fleet 2026-11-10 (platform-stl): 47,942 task_runs, 1,716 reclaimed, and
1,709 of those reclaims (99.6%) had no ``worker_pid`` — because a pid is almost never recorded
at all (0.4% platform, 0.1% financially, 0.1% research). ``release_stale_claims`` extended a
claim only when ``host_local AND worker_pid AND not-dead AND not heartbeat_stale``, so the
one run shape the fleet actually produces — a heartbeat, no pid — failed the conjunction and
was reclaimed on the spot: 138 platform runs died with a heartbeat under 300 s old.

The contract these tests pin: a recorded heartbeat inside ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS``
extends the claim whether or not a pid exists; a live pid still extends a claim that never
heartbeated (a worker inside one long tool-free call cannot heartbeat); the one-hour max-stale
backstop is unchanged, so a heartbeat past it is reclaimed even beside a live process.

The first test is the card's own: it fails on the base revision.
"""

import os
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _expired_claim(conn, *, heartbeat_age=None, pid=None, started_at=None) -> str:
    """A ``running`` card whose claim expired a TTL ago, with the evidence we choose.

    ``heartbeat_age=None`` means the run NEVER recorded a heartbeat (the column is NULL, which
    is what 36% of platform runs and 30% of financially runs look like).
    """
    tid = kb.create_task(conn, title="job", assignee="worker")
    kb.claim_task(conn, tid)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ?, worker_pid = ?, "
            "worker_started_at = ? WHERE id = ?",
            (now - 3600,
             None if heartbeat_age is None else now - heartbeat_age,
             pid, started_at, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)", (now - 3600, tid),
        )
    return tid


def _extension_events(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == "claim_extended"]


def test_a_fresh_heartbeat_extends_an_expired_claim_with_no_pid(board):
    """The card's failing-today test: heartbeat 60 s old, worker_pid NULL, TTL expired."""
    conn = board
    tid = _expired_claim(conn, heartbeat_age=60)
    assert conn.execute("SELECT worker_pid FROM tasks WHERE id = ?", (tid,)
                        ).fetchone()["worker_pid"] is None

    assert kb.release_stale_claims(conn) == 0, (
        "a claim with a heartbeat 60 s old was reclaimed: the sweep is testing a pid the row "
        "does not carry instead of the heartbeat it does"
    )
    task = kb.get_task(conn, tid)
    assert task.status == "running"
    assert task.claim_expires is not None and task.claim_expires > int(time.time())

    events = _extension_events(conn, tid)
    assert events, "the extension must be recorded, or the next reader cannot tell it happened"
    payload = events[-1].payload
    assert payload["reason"] == "heartbeat_fresh"
    assert payload["worker_pid"] is None, "a NULL pid must be recorded as absent, not crash"
    assert 55 <= payload["heartbeat_age_s"] <= 65


def test_a_fresh_heartbeat_wins_over_a_pid_that_is_gone(board):
    """Heartbeat-first, not pid-first: a recorded dead pid cannot veto a fresh heartbeat."""
    conn = board
    tid = _expired_claim(conn, heartbeat_age=120, pid=999_999)

    assert kb.release_stale_claims(conn) == 0
    assert kb.get_task(conn, tid).status == "running"
    assert _extension_events(conn, tid)[-1].payload["reason"] == "heartbeat_fresh"


def test_a_live_pid_still_extends_a_claim_that_never_heartbeated(board):
    """The corroboration branch survives: a worker inside one long tool-free call cannot heartbeat."""
    conn = board
    tid = _expired_claim(conn, heartbeat_age=None, pid=os.getpid(),
                         started_at=kbd._process_fingerprint(os.getpid()))

    assert kb.release_stale_claims(conn) == 0
    assert kb.get_task(conn, tid).status == "running"
    payload = _extension_events(conn, tid)[-1].payload
    assert payload["reason"] == "pid_alive"
    assert payload["heartbeat_age_s"] is None


def test_the_hour_backstop_still_reclaims_a_stale_heartbeat(board):
    """Unchanged by R1: past ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` means wedged."""
    conn = board
    tid = _expired_claim(
        conn, heartbeat_age=kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS + 100,
        # A pid that is gone: the point under test is the heartbeat bound, and a live pid here
        # would make the sweep signal this very interpreter (its "survived termination" branch).
        pid=999_999,
    )

    assert kb.release_stale_claims(conn) == 1, (
        "a heartbeat past the max-stale bound must still be reclaimed: this is the wedged "
        "backstop, and R1 does not weaken it"
    )
    assert kb.get_task(conn, tid).status != "running"


def test_an_expired_claim_with_no_heartbeat_and_no_pid_is_still_reclaimed(board):
    """R1 does not turn absence of evidence into liveness: nothing recorded, nothing to hold."""
    conn = board
    tid = _expired_claim(conn, heartbeat_age=None, pid=None)

    assert kb.release_stale_claims(conn) == 1
    assert kb.get_task(conn, tid).status != "running"
