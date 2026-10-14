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
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"




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


# ---------------------------------------------------------------------------
# dispatcher_hold — a HELD ready card must not read as "stranded"
#
# The dispatcher skips a guarded card on every tick and records a
# ``respawn_guarded`` event carrying its reason; the row itself stays ``ready``.
# ``stranded_in_ready`` used to read that same row and blame the worker pool.
# Measured live 2026-10-13: migrations/t_15763252 sat "ready 12.3h with no
# worker" over 57 skips, and two more cards were held 15h and 35h the same way.
# ---------------------------------------------------------------------------


def test_dispatcher_hold_names_the_reason_instead_of_blaming_the_pool():
    """The held card reports the dispatcher's OWN reason, and the false
    ``stranded_in_ready`` reading of the same row stands down."""
    now = 200_000
    task = _task(status="ready", assignee="platform-coder", claim_lock=None)
    events = [
        _event("created", ts=now - 8 * 3600),
        _event("contract_changed", ts=now - 6 * 3600),
        _event("respawn_guarded", ts=now - 1800, reason="active_pr"),
        _event("respawn_guarded", ts=now - 600, reason="active_pr"),
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d.kind for d in diags] == ["dispatcher_hold"]
    d = diags[0]
    assert d.severity == "warning"
    assert d.data["reason"] == "active_pr"
    assert d.data["held_seconds"] == 1800       # from the FIRST skip, not the last
    assert d.data["skipped_ticks"] == 2
    assert "24h" in d.detail                    # names the window it will lift on
    assert "platform-coder" in d.detail
    assert "stranded_in_ready" not in [x.kind for x in diags]


def test_dispatcher_hold_stays_quiet_inside_the_grace():
    """A hold younger than the grace is ordinary dispatcher spacing, not a
    signal — and it still suppresses the stranded reading of the row."""
    now = 200_000
    task = _task(status="ready", claim_lock=None)
    events = [_event("respawn_guarded", ts=now - 60, reason="rate_limit_cooldown")]
    assert kd.compute_task_diagnostics(task, events, [], now=now) == []


def test_dispatcher_hold_severity_escalates_with_the_age_of_the_hold():
    now = 400_000
    task = _task(status="ready", claim_lock=None)

    def severity(age):
        diags = kd.compute_task_diagnostics(
            task, [_event("respawn_guarded", ts=now - age, reason="active_pr")],
            [], now=now,
        )
        assert [d.kind for d in diags] == ["dispatcher_hold"]
        return diags[0].severity

    assert severity(3600) == "warning"
    assert severity(8 * 3600) == "error"
    assert severity(30 * 3600) == "critical"


def test_a_claim_answers_the_hold_but_a_comment_does_not():
    """A spawn/claim means the guard lifted and a fresh hold starts at the next
    skip; a ``commented`` event must NOT read as a release — a PR-URL comment is
    exactly what ARMS ``active_pr``."""
    now = 200_000
    task = _task(status="ready", claim_lock=None)
    reheld = [
        _event("respawn_guarded", ts=now - 7200, reason="active_pr"),
        _event("claimed", ts=now - 3700),
        _event("respawn_guarded", ts=now - 3600, reason="active_pr"),
    ]
    d = [x for x in kd.compute_task_diagnostics(task, reheld, [], now=now)
         if x.kind == "dispatcher_hold"]
    assert len(d) == 1
    assert d[0].data["held_seconds"] == 3600    # the run restarted at the claim
    assert d[0].data["skipped_ticks"] == 1

    commented = [
        _event("respawn_guarded", ts=now - 7200, reason="active_pr"),
        _event("commented", ts=now - 1200),     # the PR-URL comment itself
    ]
    d = [x for x in kd.compute_task_diagnostics(task, commented, [], now=now)
         if x.kind == "dispatcher_hold"]
    assert len(d) == 1 and d[0].data["held_seconds"] == 7200


def test_only_a_real_handoff_to_another_profile_answers_the_hold():
    now = 200_000
    task = _task(status="ready", claim_lock=None)
    held = [_event("respawn_guarded", ts=now - 7200, reason="active_pr")]

    def kinds(extra):
        return [d.kind for d in kd.compute_task_diagnostics(task, held + extra, [], now=now)]

    # The documented early release: a handoff to a DIFFERENT profile.
    assert kinds([_event("assigned", ts=now - 3600,
                         **{"from": "platform-coder", "assignee": "platform-stl"})]) == []
    # A no-op reassign back to the same lane deliberately does NOT lift it.
    assert kinds([_event("assigned", ts=now - 3600,
                         **{"from": "platform-coder", "assignee": "platform-coder"})]) \
        == ["dispatcher_hold"]
    # An `assigned` with no `from` is not trusted (fail closed).
    assert kinds([_event("assigned", ts=now - 3600, assignee="platform-stl")]) \
        == ["dispatcher_hold"]


def test_stranded_in_ready_still_fires_when_nothing_holds_the_card():
    """The contrast case: no guard event, so the pool-blame reading is still the
    right answer."""
    now = 300_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 45 * 60)]
    assert [d.kind for d in kd.compute_task_diagnostics(task, events, [], now=now)] \
        == ["stranded_in_ready"]


def test_dispatcher_hold_reads_a_real_event_row(kanban_home):
    """The hold survives the real DB shape: ``payload`` is a JSON string on the
    row, and the run must be measured against the event's own timestamp."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="held", assignee="w")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr"})
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (tid,)))
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (tid,)))
        # The guard event is `now`; read it from an hour in the future so the
        # hold is past the grace without touching the row.
        diags = kd.compute_task_diagnostics(row, events, runs, now=int(time.time()) + 3600)
        held = [d for d in diags if d.kind == "dispatcher_hold"]
        assert len(held) == 1
        assert held[0].data["reason"] == "active_pr"
    finally:
        conn.close()
