"""The reaper's verdict ledger and the cap on the row (card t_48194194).

Three defects, one card, and the tests below pin all three:

1. **Every examined claim writes exactly one verdict.** The KEEP side already existed
   (``reclaim_deferred``) and the TOOK side already existed (``reclaimed`` / ``crashed`` /
   ``timed_out``); what did not exist was the guarantee that a claim the reaper LOOKED AT and then
   declined to act on writes anything at all. "The reaper never looked" and "the reaper looked and
   could not act" were the same event: nothing. Two silent paths are covered here -- an unreadable
   process identity (``held_unverifiable``) and a guard hold (``identity_shape_incomparable``) --
   plus a lost claim CAS (``cas_missed``).

2. **A row a limit killed carries the limit.** The runtime sweep is gated on
   ``max_runtime_seconds``; the run row is inserted at claim time, BEFORE a defaulted cap exists,
   so 52% of clamp rows on the live board carry ``max_runtime_seconds IS NULL`` and the predicate
   ``dur >= max_runtime_seconds`` silently drops half the population it claims to count.

3. **The two limiters stay separate.** ``elapsed Ns > limit Ns`` is the runtime cap;
   ``Iteration budget exhausted`` is the worker's own turn budget. A duration band counts both as
   one population, which is the artefact class this card exists to end: the kind is a FIELD
   (``limit_kind``) and the prose prefix is DOCUMENTED in one place, so no reader has to invent a
   LIKE pattern.

Hermetic: every board lives under a per-test ``HERMES_HOME``.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from gateway import status as gw_status
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_reclaim_ledger as ledger

DEAD_PID = 9_999_999
# A pre-witness fingerprint: its identity can no longer be compared, so the liveness read is
# UNKNOWN (never DEAD) and the claim must hold. Same shape the live board carries.
UNCOMPARABLE_FINGERPRINT = "|1790412856"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _claimed_running(conn, *, pid=None, started_at=None, max_runtime=None,
                     started_ago=3600) -> str:
    tid = kb.create_task(conn, title="job", assignee="worker", max_runtime_seconds=max_runtime)
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid if pid is not None else os.getpid())
    old = int(time.time()) - started_ago
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_started_at = ?, started_at = ?, claim_expires = ? WHERE id = ?",
            (started_at, old, old, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (old, tid),
        )
    return tid


def _events(conn, task_id, kind=None):
    rows = conn.execute(
        "SELECT kind, payload, run_id FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    out = []
    for row in rows:
        if kind is not None and row["kind"] != kind:
            continue
        out.append((row["kind"], json.loads(row["payload"] or "{}"), row["run_id"]))
    return out


def _open_run(conn, task_id):
    return conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# (2) The cap that killed the row is ON the row
# ---------------------------------------------------------------------------

def test_a_defaulted_cap_lands_on_the_open_run_row_at_claim(board):
    """The root cause of the 52%: the cap was stamped on ``tasks`` AFTER the run was inserted.

    Nothing in the sweep was wrong -- the run row simply never learned its own budget, so
    ``dur >= max_runtime_seconds`` was NULL-dropped for every defaulted card.
    """
    conn = board
    tid = _claimed_running(conn, started_at=None, max_runtime=None)

    written = kbd._stamp_default_max_runtime_seconds(conn, tid, limit=7200)
    assert written == 7200

    assert kb.get_task(conn, tid).max_runtime_seconds == 7200
    run = _open_run(conn, tid)
    assert run["max_runtime_seconds"] == 7200, (
        "the run row must carry the cap it is working against, not just the card"
    )
    # The event names the run it stamped, so the two records join without a timestamp guess.
    ev = _events(conn, tid, "runtime_cap_defaulted")
    assert ev and ev[-1][2] == run["id"]


def test_a_runtime_clamp_row_carries_the_cap_that_killed_it_and_the_limit_kind(board):
    """A row a limit killed carries the limit, so the clamp predicate is valid on the whole row."""
    conn = board
    tid = _claimed_running(conn, pid=DEAD_PID, started_at=None, max_runtime=7200,
                           started_ago=7300)
    killed = []
    assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append(pid)) == [tid]

    run = _open_run(conn, tid)
    assert run["status"] == "timed_out"
    assert run["error"].startswith("elapsed ")
    assert run["max_runtime_seconds"] == 7200
    assert int(run["ended_at"]) - int(run["started_at"]) >= int(run["max_runtime_seconds"]), (
        "the clamp predicate must hold for the row the cap killed -- that is the whole point"
    )

    _kind, payload, run_id = _events(conn, tid, "timed_out")[-1]
    assert run_id == run["id"]
    assert payload["limit_kind"] == kb.LIMIT_KIND_RUNTIME_CAP
    assert payload["verdict"] == "reclaimed"
    assert payload["gate"] == "elapsed_over_limit"
    assert payload["limit_seconds"] == 7200


# ---------------------------------------------------------------------------
# (3) The two limiters are distinguished by FIELD, with the prose prefix documented once
# ---------------------------------------------------------------------------

def test_the_two_limiters_separate_by_prefix_never_by_band():
    f = kb.limit_kind_for_error
    assert f("elapsed 7300s > limit 7200s") == kb.LIMIT_KIND_RUNTIME_CAP
    assert f("Iteration budget exhausted (200/200) — task could not complete") == (
        kb.LIMIT_KIND_ITERATION_BUDGET
    )
    assert f("boom") is None
    assert f(None) is None
    # A worker quoting the other limiter in its own output must NOT be reclassed by substring.
    assert f("Traceback: saw 'Iteration budget exhausted' once") is None


# ---------------------------------------------------------------------------
# (1) One verdict row per examined claim
# ---------------------------------------------------------------------------

def test_every_examined_claim_gets_exactly_one_verdict(board):
    """``count(verdicts) == count(candidates)`` over one sweep, per gate.

    The sweep examines four claims: one provably alive (NOT a candidate -- a verdict row for it
    every tick would be an event storm), one dead (reclaimed), one whose identity cannot be read
    (held), one dead-but-heartbeating (R2 veto).
    """
    conn = board
    alive = _claimed_running(conn, pid=os.getpid(), started_at=None)
    dead = _claimed_running(conn, pid=DEAD_PID, started_at=None)
    unknown = _claimed_running(conn, pid=os.getpid(), started_at=UNCOMPARABLE_FINGERPRINT)
    vetoed = _claimed_running(conn, pid=DEAD_PID, started_at=None)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
                     (int(time.time()), vetoed))

    sweep = kbd._reclaim_dead_workers(conn)

    assert sweep.assessment is not None
    assert not sweep.assessment.refusal_needed, "one death cannot trip the sweep guard"
    assert sweep.candidates == 3, "the alive worker is not a candidate"
    assert len(sweep.verdicts) == sweep.candidates, (
        f"a claim was examined without a verdict: {sweep.verdicts}"
    )
    by_task: dict[str, tuple] = {}
    for entry in sweep.verdicts:
        assert entry[0] not in by_task, f"two verdicts for one examined claim: {entry}"
        by_task[entry[0]] = entry
    assert alive not in by_task

    # ``dead.kind`` is the classifier's own word for the death ("unknown" when the worker left no
    # exit code); the GATE is what this card pins: proof-of-death is the only gate that releases.
    assert by_task[dead][1:3] == ("reclaimed", "dead_pid")
    assert by_task[dead][3]
    assert by_task[unknown][1:] == (
        "reclaim_deferred", "pid_liveness", "held_unverifiable",
    )
    assert by_task[vetoed][1:] == ("reclaim_deferred", "heartbeat_veto", "heartbeat_fresh")

    # The ledger is on the board, not just in the return value: each verdict is a real event row
    # carrying its side, gate and reason.
    for tid, (_, kind, gate, reason) in by_task.items():
        hits = [e for e in _events(conn, tid) if e[1].get("verdict")]
        assert len(hits) == 1, f"{tid} wrote {len(hits)} verdict rows"
        assert hits[0][1]["verdict"] == kind
        assert hits[0][1]["gate"] == gate
        assert hits[0][1]["reason"] == reason

    assert kb.get_task(conn, dead).status != "running"
    assert kb.get_task(conn, unknown).status == "running"
    assert kb.get_task(conn, vetoed).status == "running"


def test_a_lost_claim_cas_is_a_verdict_too(board, monkeypatch):
    """Decided to reclaim, then the claim moved under us: that is a KEEP, and it is written."""
    conn = board
    tid = _claimed_running(conn, pid=DEAD_PID, started_at=None)

    real_update = conn.execute

    class _CasMisses:
        """Pass every statement through EXCEPT the reclaim's owning UPDATE, which reports 0 rows."""

        def __init__(self, inner):
            self._inner = inner
            self.rowcount = 0

        def __call__(self, sql, *args, **kwargs):
            cursor = self._inner(sql, *args, **kwargs)
            if "UPDATE tasks SET status = ?" in sql and "claim_lock IS ?" in sql:
                return self
            return cursor

        def __getattr__(self, name):
            return getattr(self._inner, name)

    conn.execute = _CasMisses(real_update)
    try:
        sweep = kbd._reclaim_dead_workers(conn)
    finally:
        conn.execute = real_update

    assert sweep.candidates == 1
    assert sweep.verdicts == [(tid, "reclaim_deferred", "claim_cas", "cas_missed")]


# ---------------------------------------------------------------------------
# The reader: both axes, never one
# ---------------------------------------------------------------------------

def _finished_run(conn, *, error, cap, dur, tid=None):
    tid = tid or kb.create_task(conn, title="c", assignee="worker")
    kb.claim_task(conn, tid)
    run_id = kb._current_run_id(conn, tid)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET status='timed_out', started_at=?, ended_at=?, error=?, "
            "max_runtime_seconds=? WHERE id = ?",
            (now - dur, now, error, cap, run_id),
        )
        conn.execute("UPDATE tasks SET status='ready', current_run_id = NULL WHERE id = ?", (tid,))
    return tid, run_id


def test_the_ledger_reads_both_sides_and_reports_cap_coverage(board):
    """The graded table: took, kept, and the clamp population WITH its cap coverage.

    A clamp count without its coverage is a number whose predicate nobody stated -- so the reader
    reports both, and counts the iteration-budget population separately instead of summing it in.
    """
    conn = board
    _t1, r1 = _finished_run(conn, error="elapsed 7300s > limit 7200s", cap=7200, dur=7300)
    _t2, r2 = _finished_run(conn, error="elapsed 7400s > limit 7200s", cap=None, dur=7400)
    _t3, r3 = _finished_run(conn, error="Iteration budget exhausted (200/200) — x", cap=None,
                            dur=7300)

    with kb.write_txn(conn):
        kb._append_event(conn, "t1", "crashed",
                         {"verdict": "reclaimed", "gate": "dead_pid", "reason": "dead_worker"},
                         run_id=r1)
        kb._append_event(conn, "t2", "reclaim_deferred",
                         {"verdict": "reclaim_deferred", "gate": "pid_liveness",
                          "reason": "held_unverifiable"}, run_id=r2)
        # A legacy row: no verdict field, side named by kind. The reader must still count it.
        kb._append_event(conn, "t3", "reclaimed", {"stale_lock": "x"}, run_id=r3)

    report = ledger.signature(conn)
    clamp = report["clamp"]
    assert clamp["clamps"] == 2, "only the runtime cap is a clamp"
    assert clamp["with_cap"] == 1
    assert clamp["without_cap"] == 1
    assert clamp["cap_share"] == 0.5
    assert clamp["predicate_matches"] == 1, "the predicate can only see the row that carries a cap"
    assert clamp["by_limit_kind"] == {kb.LIMIT_KIND_RUNTIME_CAP: 2,
                                     kb.LIMIT_KIND_ITERATION_BUDGET: 1}

    took = {(r["gate"], r["reason"]): r["runs"] for r in report["took"]}
    kept = {(r["gate"], r["reason"]): r["runs"] for r in report["kept"]}
    assert took == {("dead_pid", "dead_worker"): 1, ("legacy", ""): 1}
    assert kept == {("pid_liveness", "held_unverifiable"): 1}
    assert report["took_rows"] == 2
    assert report["kept_rows"] == 1
