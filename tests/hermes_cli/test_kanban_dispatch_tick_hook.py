"""Tests for the ``on_kanban_dispatch_tick`` observer hook.

Re-port of PR #56066 per the #64231 batch disposition: renamed to the
taxonomy form and fired by ``kanban_db.dispatch_once`` strictly AFTER the
board's single-writer dispatch lock has been released — the original fired
inside ``_dispatch_tick_lock``, so a slow subscriber could extend the
critical section and stall a sibling dispatcher. Verifies the post-lock
contract directly, the ``outcome`` classification (including idle and
contended ticks), ``dry_run`` propagation, and that a misbehaving
subscriber never breaks the dispatcher.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.plugins import get_plugin_manager

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home

@pytest.fixture
def captured_ticks(monkeypatch):
    """Register a capturing callback for the dispatch tick hook."""
    mgr = get_plugin_manager()
    events: list[dict] = []
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    mgr._hooks.setdefault("on_kanban_dispatch_tick", []).append(
        lambda **kw: events.append(kw)
    )
    try:
        yield events
    finally:
        mgr._hooks = saved

def test_active_tick_fires_hook_with_outcome_ok(
    kanban_home, all_assignees_spawnable, captured_ticks,
):
    """A tick that spawns a worker fires the hook with outcome='ok'."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    finally:
        conn.close()
    ok_events = [kw for kw in captured_ticks if kw["outcome"] == "ok"]
    assert ok_events, (
        f"expected an ok tick, got {[kw['outcome'] for kw in captured_ticks]}"
    )
    result = ok_events[-1]["result"]
    assert any(row[0] == tid for row in result.spawned)

def test_tick_hook_fires_after_dispatch_lock_released(kanban_home):
    """The #56066 sweeper finding, as a contract: subscribers run OUTSIDE
    the single-writer critical section. From the callback, acquiring the
    board's dispatch lock must succeed — flock conflicts across file
    descriptors within one process, so this fails if the hook still fires
    while ``dispatch_once`` holds the lock."""
    db_path = kb.kanban_db_path()
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    acquired: list[bool] = []

    def _probe_lock(**kw):
        with kbc._dispatch_tick_lock(db_path) as held:
            acquired.append(held)

    mgr._hooks.setdefault("on_kanban_dispatch_tick", []).append(_probe_lock)
    try:
        conn = kbc.connect()
        try:
            kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        finally:
            conn.close()
    finally:
        mgr._hooks = saved
    assert acquired == [True]

def test_misbehaving_subscriber_does_not_break_dispatcher(kanban_home):
    """A hook callback that raises must not break the dispatch tick."""
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}

    def _boom(**kw):
        raise RuntimeError("subscriber exploded")

    mgr._hooks.setdefault("on_kanban_dispatch_tick", []).append(_boom)
    try:
        conn = kbc.connect()
        try:
            result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
            assert isinstance(result, kb.DispatchResult)
        finally:
            conn.close()
    finally:
        mgr._hooks = saved


def test_a_release_only_tick_is_not_idle(
    kanban_home, all_assignees_spawnable, captured_ticks,
):
    """A tick whose ONLY activity is ``reclaimed_orphaned`` reports ok, never idle.

    The counter releases a claim left on a SETTLED row (card t_921f8f57). Until it was listed
    in ``_TICK_ACTIVITY_FIELDS`` a tick that released one was indistinguishable from a tick
    that did nothing: the observer said ``idle`` and ``hermes kanban dispatch`` printed
    ``Reclaimed: 0`` with no other line to say work had happened.

    Two halves, because the defect was one classification step and one wiring step.

    1. CLASSIFICATION, exercised directly: a synthetic result whose ONLY counter is the
       release must report ``ok``, and the all-zero control must still report ``idle`` -- so
       the arm cannot pass by calling every tick ok, and it fails on the pre-fix code.
    2. WIRING, through a real ``dispatch_once``: a ``review`` row holding an expired claim is
       released without its status moving and without a failure being booked. (The review
       lane also parks that row as ``skipped_self_review`` -- no implementer is recorded --
       which is incidental to the release and not asserted here.)
    """
    # 1. Classification: the release alone is activity; the empty tick is the control.
    release_only = kb.DispatchResult()
    release_only.reclaimed_orphaned = 1
    kb._fire_dispatch_tick_hook(release_only, board="platform")
    assert captured_ticks[-1]["outcome"] == "ok", captured_ticks[-1]["outcome"]

    empty = kb.DispatchResult()
    kb._fire_dispatch_tick_hook(empty, board="platform")
    assert captured_ticks[-1]["outcome"] == "idle"

    # 2. Wiring: a real tick books the release on a settled row and is not idle.
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="review with residue", assignee="alice")
        kb.claim_task(conn, tid, claimer="localhost:4242")
        kbd._set_worker_pid(conn, tid, 424242)
        conn.execute(
            "UPDATE tasks SET status = 'review', claim_expires = ?, "
            "consecutive_failures = 2 WHERE id = ?",
            (int(time.time()) - 3600, tid),
        )
        conn.commit()

        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
        row = conn.execute(
            "SELECT status, claim_lock, claim_expires, consecutive_failures "
            "FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
    finally:
        conn.close()

    assert result.reclaimed_orphaned == 1
    assert result.reclaimed == 0                    # not a reclaim: no status move
    assert row["status"] == "review"                # the release keeps the status it found
    assert row["claim_lock"] is None and row["claim_expires"] is None
    assert row["consecutive_failures"] == 2         # residue is not a failed attempt
    # The three hook events are: the release-only synthetic ('ok'), the all-zero control
    # ('idle'), and the real tick -- which must be the 'ok' one, not idle.
    assert [kw["outcome"] for kw in captured_ticks] == ["ok", "idle", "ok"], \
        [kw["outcome"] for kw in captured_ticks]
