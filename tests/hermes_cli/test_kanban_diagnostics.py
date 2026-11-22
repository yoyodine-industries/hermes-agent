"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_running_with_open_parents_fires_only_while_running():
    """A running card whose parent is not terminal is flagged; the same graph
    on a ready/todo card (the gate is holding it) and a done parent are not."""
    graph = {"parents": [{"id": "t_parent", "title": "p", "status": "todo"}], "children": []}
    diags = kd.compute_task_diagnostics(_task(status="running", started_at=100), [], [], graph=graph)
    assert [d.kind for d in diags] == ["running_with_open_parents"]
    assert diags[0].data["open_parents"] == [{"id": "t_parent", "status": "todo"}]
    assert "hermes kanban unlink t_parent t_demo00" in diags[0].actions[0].payload["command"]
    assert kd.compute_task_diagnostics(_task(status="todo"), [], [], graph=graph) == []
    done_graph = {"parents": [{"id": "t_parent", "title": "p", "status": "done"}], "children": []}
    assert kd.compute_task_diagnostics(_task(status="running"), [], [], graph=done_graph) == []


def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48








# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# The trigger is a failed START: a run row with a failed-start outcome inside
# the evidence window, an assignee with no live profile, or a lane that claimed
# nothing while the dispatcher is demonstrably running. Queue depth is CONTEXT.
# On a net-sink board (producers outrun the drain) every ready card is old, so
# age alone must never raise a warning.
# ---------------------------------------------------------------------------

_NOW = int(time.time())


def _ready_card(conn, *, title, assignee, age_seconds):
    """A ready card that has been waiting ``age_seconds`` (no created event, so
    the rule's ready-clock falls back to ``created_at``)."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    conn.execute("DELETE FROM task_events WHERE task_id = ?", (tid,))
    conn.execute(
        "UPDATE tasks SET status = 'ready', created_at = ?, started_at = NULL WHERE id = ?",
        (_NOW - age_seconds, tid),
    )
    conn.commit()
    return tid


def _run_row(conn, task_id, *, profile, outcome, started_offset, error=None):
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, profile, outcome or "running", _NOW - started_offset,
         None if outcome is None else _NOW - started_offset + 30, outcome, error),
    )
    conn.commit()


def _stranded_diags(conn, task_id, *, known_profiles=None, window=86400):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    events = list(conn.execute("SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)))
    runs = list(conn.execute("SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,)))
    ctx = kd.build_lane_context(
        conn, now=_NOW, window_seconds=window, known_profiles=set(known_profiles or ()))
    diags = kd.compute_task_diagnostics(
        row, events, runs, now=_NOW, config={"_lane_context": ctx})
    return [d for d in diags if d.kind == "stranded_in_ready"]


def test_deep_ready_queue_with_recent_claims_is_backlog_not_stranded(kanban_home):
    """Acceptance (a): a lane with a deep ready queue AND recent successful
    claims produces no stranded_in_ready warning — age is backlog, not failure."""
    conn = kbc.connect()
    try:
        for i in range(30):
            _ready_card(conn, title=f"q{i}", assignee="alpha", age_seconds=6 * 86400 - i * 60)
        oldest = _ready_card(conn, title="oldest", assignee="alpha", age_seconds=6 * 86400)
        for i in range(3):
            _run_row(conn, oldest, profile="alpha", outcome="completed", started_offset=300 + i * 60)
        assert _stranded_diags(conn, oldest, known_profiles={"alpha"}) == []
    finally:
        conn.close()


def test_lane_with_no_claims_while_dispatcher_runs_fires_once(kanban_home):
    """Acceptance (b): ready work + zero claims in the window + a running
    dispatcher (another lane keeps claiming) = ONE finding naming the lane."""
    conn = kbc.connect()
    try:
        beta_old = _ready_card(conn, title="beta-old", assignee="beta", age_seconds=3 * 86400)
        beta_new = _ready_card(conn, title="beta-new", assignee="beta", age_seconds=3600)
        alpha = _ready_card(conn, title="alpha", assignee="alpha", age_seconds=7200)
        # The dispatcher is alive: it claimed alpha two minutes ago.
        _run_row(conn, alpha, profile="alpha", outcome="completed", started_offset=120)

        stranded = _stranded_diags(conn, beta_old, known_profiles={"alpha", "beta"})
        assert len(stranded) == 1
        d = stranded[0]
        assert d.severity == "critical"
        assert d.data["trigger"] == "no_claim"
        assert "beta" in d.title and "beta" in d.data["assignee"]
        assert "no claim" in d.detail
        # Depth rides along as context and words itself as such.
        assert "Depth is context" in d.detail
        assert d.data["lane_ready_depth"] == 2

        # One finding per lane: the rest of that lane's queue stays quiet.
        assert _stranded_diags(conn, beta_new, known_profiles={"alpha", "beta"}) == []
    finally:
        conn.close()


def test_assignee_without_live_profile_still_fires(kanban_home):
    """Acceptance (c): the rule's original purpose — a typo'd / deleted profile
    can never be claimed, so it fires even though the queue is not the cause.
    The finding names the dispatcher's own skip reason."""
    conn = kbc.connect()
    try:
        ghost = _ready_card(conn, title="ghost", assignee="ghost", age_seconds=2 * 3600)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (ghost, "skipped_nonspawnable", '{"assignee": "ghost"}', _NOW - 1800),
        )
        conn.commit()
        stranded = _stranded_diags(conn, ghost, known_profiles={"alpha"})
        assert len(stranded) == 1
        assert stranded[0].severity == "critical"
        assert stranded[0].data["trigger"] == "profile_missing"
        assert "ghost" in stranded[0].title
        assert "skipped_nonspawnable" in stranded[0].detail
    finally:
        conn.close()


def test_failed_start_on_the_card_fires_with_its_outcome(kanban_home):
    """A claim that raised (spawn_failed inside the window) is a failed START
    and fires, even though the lane itself is otherwise healthy."""
    conn = kbc.connect()
    try:
        card = _ready_card(conn, title="crashed", assignee="alpha", age_seconds=3 * 3600)
        _run_row(conn, card, profile="alpha", outcome="spawn_failed",
                 started_offset=1800, error="[Errno 24] Too many open files")
        stranded = _stranded_diags(conn, card, known_profiles={"alpha"})
        assert len(stranded) == 1
        d = stranded[0]
        assert d.severity == "warning"
        assert d.data["trigger"] == "failed_start"
        assert d.data["last_start_outcome"] == "spawn_failed"
        assert "Too many open files" in d.detail
        assert "not queue depth" in d.detail
    finally:
        conn.close()


def test_stale_failed_start_outside_the_window_is_backlog(kanban_home):
    """The platform board's 5,869 cards whose last start failed 8 days ago are
    backlog once the lane has claimed since — an old failure is not evidence the
    lane is broken now."""
    conn = kbc.connect()
    try:
        card = _ready_card(conn, title="old-failure", assignee="alpha", age_seconds=9 * 86400)
        _run_row(conn, card, profile="alpha", outcome="spawn_failed", started_offset=8 * 86400)
        _run_row(conn, card, profile="alpha", outcome="completed", started_offset=600)
        # completed run is the latest -> no failed start at all
        assert _stranded_diags(conn, card, known_profiles={"alpha"}) == []
        # ...and even with only the old failure on record it is outside the window
        conn.execute("DELETE FROM task_runs WHERE outcome = 'completed'")
        conn.commit()
        assert _stranded_diags(conn, card, known_profiles={"alpha"}) == []
    finally:
        conn.close()


def test_no_lane_context_does_not_flag_depth(kanban_home):
    """Standalone callers with no evidence channel get silence, never the old
    age-only 'no worker' claim."""
    diags = kd.compute_task_diagnostics(
        _task(status="ready", assignee="demo"), [_event("created", ts=_NOW - 45 * 60)], [],
        now=_NOW)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True
