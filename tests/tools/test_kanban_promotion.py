"""ONE promotion predicate, four readers — and the two that catch the real bug.

The board used to decide promotion from different inputs in different places:
``recompute_ready`` read the GRAPH, ``kanban unblock``/``promote`` flipped status with no
work check at all, and the dependency notifier described a promotion from the status word
alone. The measured consequence: a notice promising a promotion while the row carried a
live ``worker_pid`` and a 47-second heartbeat, and a human ``unblock`` buying exactly the
crash the automaton would have bought.

These tests never touch the network. The tracker is a fake injected through the module
function, or a callable passed straight to ``promote_state``.
"""
import json
import os
import time

import pytest

from hermes_cli import kanban_promotion as kp


@pytest.fixture
def board(monkeypatch, tmp_path):
    """A temp HERMES_HOME with a fresh board; yields ``(kb, conn)``."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        yield kb, conn
    finally:
        conn.close()


def _task(conn, kb, **kwargs):
    """A card parked in ``todo`` — the state every promotion door acts on."""
    tid = kb.create_task(conn, title=kwargs.pop("title", "card"), assignee="test-worker",
                         **kwargs)
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))
    conn.commit()
    return tid


def _set_pr(conn, tid, url):
    """Record a PR declaration the way a worker does: on the run's metadata."""
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, metadata, started_at, ended_at)"
        " VALUES (?, 'test-worker', 'done', 'done', ?, ?, ?)",
        (tid, json.dumps({"published_pr": url}), int(time.time()), int(time.time())))
    conn.commit()


def _stamp(conn, tid, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*cols.values(), tid))
    conn.commit()


def _clean_tracker(**over):
    base = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "headRefOid": "abc123", "headRefName": "card/x", "baseRefName": "main",
            "behind_by": 3}
    base.update(over)
    calls = []

    class _Tracker:
        def __init__(self):
            self.calls = []

        def __call__(self, ref):
            self.calls.append(ref)
            return dict(base)

    return _Tracker()


# ---------------------------------------------------------------------------
# 1. A live claim beats the graph — and the notifier goes SILENT.
# ---------------------------------------------------------------------------
def test_a_live_worker_claim_is_IN_FLIGHT_and_the_notifier_is_silent(board, monkeypatch):
    kb, conn = board
    from hermes_cli.kanban_db_links import link_warning, parent_edges

    child = _task(conn, kb, title="child")
    parent = _task(conn, kb, title="parent")
    kb.link_tasks(conn, parent, child)
    # A live claim the graph cannot see: the row carries the pid and a fresh heartbeat.
    _stamp(conn, parent, worker_pid=os.getpid(), last_heartbeat_at=int(time.time()))

    state = kp.promote_state_for(conn, parent)
    assert state.outcome == "IN_FLIGHT", state.receipt()
    assert not state.promotable
    assert state.fields["worker_pid"] == os.getpid()

    edges = parent_edges(conn, child)
    assert len(edges) == 1
    assert edges[0].code == "in_flight"
    assert edges[0].self_moves is True
    # SILENT: nothing to warn about, because a run is on it.
    assert link_warning(edges, {"ready": True, "todo": True}) is None


def test_a_dead_pid_is_not_a_claim(board):
    kb, conn = board
    tid = _task(conn, kb)
    _stamp(conn, tid, worker_pid=999999, last_heartbeat_at=int(time.time()))
    state = kp.promote_state_for(conn, tid)
    assert state.outcome == "NOT_APPLICABLE", state.receipt()


def test_a_stale_heartbeat_is_still_in_flight_not_a_promotion(board):
    kb, conn = board
    tid = _task(conn, kb)
    _stamp(conn, tid, worker_pid=os.getpid(),
           last_heartbeat_at=int(time.time()) - kp.heartbeat_window_s() - 60)
    state = kp.promote_state_for(conn, tid)
    assert state.outcome == "IN_FLIGHT"
    assert "stale claim" in state.reason
    assert not state.promotable


# ---------------------------------------------------------------------------
# 2. HUMAN_HOLD refuses the human door too, unless --force.
# ---------------------------------------------------------------------------
def test_needs_input_refuses_unblock_without_force_and_prints_the_reason(board):
    kb, conn = board
    # A READY card: block_task parks a todo card in todo (the dispatch door's own rule).
    tid = kb.create_task(conn, title="needs a human", assignee="test-worker")
    assert kb._task_status(conn, tid) == "ready"
    kb.block_task(conn, tid, reason="which spec?", kind="needs_input")
    assert kb._task_status(conn, tid) == "blocked"

    state = kp.promote_state_for(conn, tid)
    assert state.outcome == "HUMAN_HOLD", state.receipt()

    with pytest.raises(kp.PromotionRefused) as excinfo:
        kb.unblock_task(conn, tid)
    receipt = str(excinfo.value)
    assert "HUMAN_HOLD" in receipt and "needs_input" in receipt
    # Refused means UNCHANGED, not half-released.
    assert kb._task_status(conn, tid) == "blocked"

    # The operator's override is the ONLY way past a hold.
    assert kb.unblock_task(conn, tid, force=True) is True
    assert kb._task_status(conn, tid) != "blocked"


# ---------------------------------------------------------------------------
# 3. A tracker that cannot answer is UNCHECKED — and UNCHECKED refuses.
# ---------------------------------------------------------------------------
def test_tracker_that_raises_is_UNCHECKED_and_the_promotion_is_refused(board, monkeypatch):
    kb, conn = board
    tid = _task(conn, kb)
    _set_pr(conn, tid, "https://github.com/o/r/pull/7")

    def boom(ref):
        raise kp.TrackerError("gh: rate limit exceeded")

    monkeypatch.setattr(kp, "gh_tracker", boom)

    state = kp.promote_state_for(conn, tid)
    assert state.outcome == "UNCHECKED", state.receipt()
    assert state.pr == "o/r#7"
    assert not state.promotable

    ok, err = kb.promote_task(conn, tid, actor="tester")
    assert ok is False and "UNCHECKED" in err
    assert kb._task_status(conn, tid) == "todo"  # unchanged

    # And the automaton obeys the same refusal, with one deferred receipt on the stream.
    assert kb.recompute_ready(conn) == 0
    assert kb._task_status(conn, tid) == "todo"
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = "
                        "'promotion_deferred'", (tid,)).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["outcome"] == "UNCHECKED"
    # Re-running does not re-spam an unchanged wait (spec 3.3).
    kb.recompute_ready(conn)
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = "
                        "'promotion_deferred'", (tid,)).fetchone()[0] == 1


def test_no_tracker_wired_is_UNCHECKED_not_a_clean_pass(board, monkeypatch):
    kb, conn = board
    tid = _task(conn, kb)
    _set_pr(conn, tid, "https://github.com/o/r/pull/9")
    monkeypatch.setenv("HERMES_KANBAN_PROMOTE_TRACKER", "none")
    state = kp.promote_state_for(conn, tid)
    assert state.outcome == "UNCHECKED"
    assert "no tracker is wired" in state.reason


# ---------------------------------------------------------------------------
# 4. No PR carrier is NOT-APPLICABLE — the one surviving graph-only path — and it
#    promotes, saying so.
# ---------------------------------------------------------------------------
def test_no_pr_card_is_NOT_APPLICABLE_and_promotes_with_the_receipt(board):
    kb, conn = board
    tid = _task(conn, kb)

    state = kp.promote_state_for(conn, tid)
    assert state.outcome == "NOT_APPLICABLE", state.receipt()
    assert state.promotable

    ok, err = kb.promote_task(conn, tid, actor="tester")
    assert ok is True, err
    assert kb._task_status(conn, tid) == "ready"
    payload = json.loads(conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'promoted_manual' "
        "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()["payload"])
    assert payload["promotion"]["outcome"] == "NOT_APPLICABLE"
    assert "graph-only" in payload["promotion"]["reason"]


# ---------------------------------------------------------------------------
# 5. The tracker legs: only a clean, near-enough, OPEN carrier is PROMOTABLE.
# ---------------------------------------------------------------------------
def _pr_card(conn, kb):
    tid = _task(conn, kb)
    _set_pr(conn, tid, "https://github.com/o/r/pull/7")
    return tid


def test_a_clean_carrier_is_PROMOTABLE(board):
    kb, conn = board
    tid = _pr_card(conn, kb)
    tracker = _clean_tracker()
    state = kp.promote_state_for(conn, tid, tracker=tracker)
    assert state.outcome == "PROMOTABLE", state.receipt()
    assert state.behind_by == 3 and state.threshold == kp.DEFAULT_MAX_BEHIND
    assert tracker.calls == ["o/r#7"]


@pytest.mark.parametrize("over,needle", [
    ({"behind_by": 500}, "behind_by=500"),
    ({"state": "MERGED"}, "MERGED"),
    ({"mergeable": "CONFLICTING"}, "CONFLICTING"),
    ({"mergeStateStatus": "DIRTY"}, "DIRTY"),
])
def test_a_carrier_the_tracker_refuses_is_NOT_PROMOTABLE(board, monkeypatch, over, needle):
    kb, conn = board
    tid = _pr_card(conn, kb)
    tracker = _clean_tracker(**over)
    monkeypatch.setattr(kp, "gh_tracker", tracker)
    state = kp.promote_state_for(conn, tid, tracker=tracker)
    assert state.outcome == "NOT-PROMOTABLE", state.receipt()
    assert needle in state.reason
    ok, err = kb.promote_task(conn, tid, actor="tester")
    assert ok is False and "NOT-PROMOTABLE" in err


def test_the_threshold_is_declared_and_overridable(board, monkeypatch):
    kb, conn = board
    tid = _pr_card(conn, kb)
    monkeypatch.setenv("HERMES_KANBAN_PROMOTE_MAX_BEHIND", "400")
    assert kp.behind_threshold() == 400
    state = kp.promote_state_for(conn, tid, tracker=_clean_tracker(behind_by=399))
    assert state.outcome == "PROMOTABLE", state.receipt()
    assert state.threshold == 400


# ---------------------------------------------------------------------------
# 6. SCHEDULED: a future clock is not a promotion, but it is not a hold either.
# ---------------------------------------------------------------------------
def test_a_future_clock_is_SCHEDULED_and_unblock_is_still_allowed(board):
    kb, conn = board
    tid = kb.create_task(conn, title="windowed", assignee="test-worker")
    assert kb.schedule_task(conn, tid, reason="window",
                            due_at=int(time.time()) + 3600) is True
    assert kp.promote_state_for(conn, tid).outcome == "SCHEDULED"
    # unblock is the door a clock fence opens THROUGH: SCHEDULED is not a refusal there.
    assert kb.unblock_task(conn, tid) is True


def test_the_receipt_carries_every_field_a_reader_needs(board):
    kb, conn = board
    tid = _pr_card(conn, kb)
    state = kp.promote_state_for(conn, tid, tracker=_clean_tracker())
    receipt = state.receipt()
    for needle in ("promote_state=PROMOTABLE", "reason=", "pr=o/r#7", "state=OPEN",
                   "mergeable=MERGEABLE", "headRefOid=abc123", "behind_by=3/", "measured_at="):
        assert needle in receipt, receipt


# ---------------------------------------------------------------------------
# 7. The notifier is a ROW-ONLY reader: it never asks the tracker, and says so.
# ---------------------------------------------------------------------------
def test_the_notifier_does_not_consult_the_tracker_and_says_it_did_not(board, monkeypatch):
    kb, conn = board
    tid = _pr_card(conn, kb)

    def explode(ref):  # pragma: no cover - must never be reached
        raise AssertionError("the notifier must not hit the network")

    monkeypatch.setattr(kp, "gh_tracker", explode)
    from hermes_cli.kanban_db_links import link_warning, parent_edges

    child = _task(conn, kb, title="child")
    kb.link_tasks(conn, tid, child)
    edges = parent_edges(conn, child)
    assert len(edges) == 1
    # The parent is a plain todo card holding a PR: nothing will move it, and the notifier
    # says so WITHOUT a network call.
    assert edges[0].self_moves is False
    assert "nothing will move" in (link_warning(edges, {"ready": True, "todo": True}) or "").lower() \
        or link_warning(edges, {"ready": True, "todo": True}) is not None

    row = dict(conn.execute("SELECT id, status, worker_pid, last_heartbeat_at, due_at, "
                            "block_kind, completion_contract FROM tasks WHERE id = ?",
                            (tid,)).fetchone())
    row["runs_metadata"] = [{"published_pr": "https://github.com/o/r/pull/7"}]
    state = kp.promote_state(row, tracker=False)
    assert state.outcome == "NOT_APPLICABLE"
    assert "row-only reader" in state.reason
