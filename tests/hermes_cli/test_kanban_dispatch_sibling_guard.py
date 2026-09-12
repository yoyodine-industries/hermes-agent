"""Respawn guard: a card whose previous worker process is still alive.

Regression tests for the sibling-worker race. The board's claim lock, the
``consecutive_failures`` counter and both ``worker_pid`` columns are cleared on
block / reclaim / timeout, but the worker PROCESS is not signalled —
``kanban_block`` is normally called by the worker itself, mid-turn. Without the
``sibling_live`` guard every dispatch input says "nobody is running this" and
the dispatcher spawns a second worker over the first: two agents burning tokens
on one card and racing the same files.

The live-sibling evidence is the ``spawned`` event (``_set_worker_pid``), the
only spawn record that survives the null-outs. These tests drive the real
helpers end to end with a real live child process — process existence is not
mocked away — and each one fails on the pre-fix tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _live_kbd():
    """The dispatcher module the CLI reads *at call time*.

    ``test_kanban_cli_dispatch_passthrough.py`` purges every ``hermes_cli*``
    module from ``sys.modules`` to scope ``HERMES_HOME`` and never restores
    them. In a whole-directory run the CLI then imports a *fresh* dispatcher
    module while a module-level ``import ... as kbd`` still points at the
    stranded copy: patching that copy is a silent no-op and the CLI runs the
    real dispatcher. Resolve the seam per call instead of at collection.
    """
    import importlib

    return importlib.import_module("hermes_cli.kanban_db_dispatch")


@pytest.fixture
def sibling_module_purge():
    """Reproduce the sibling file's ``sys.modules`` purge, then restore it.

    Deterministic stand-in for the ordering hazard: once
    ``test_kanban_cli_dispatch_passthrough.py`` has run, every ``hermes_cli*``
    module is gone from ``sys.modules`` mid-suite. Restoring the mapping in
    teardown keeps this fixture from becoming a polluter in its own right.
    """
    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if name.startswith(("hermes_cli", "hermes_state")) or name == "hermes_constants"
    }
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        sys.modules.update(saved)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def live_child():
    """A real, live process unrelated to the test process — stands in for the
    worker that is still running after it lost the card."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Make sure the pid is really alive before the test asserts on it.
        deadline = time.time() + 10
        while time.time() < deadline and not kbd._pid_alive(proc.pid):
            time.sleep(0.05)
        yield proc.pid
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _end_run(conn, task_id: str, outcome: str) -> None:
    """Close the task's current run with ``outcome`` (claim already released)."""
    task = kb.get_task(conn, task_id)
    assert task is not None
    run_id = task.current_run_id
    conn.execute(
        "UPDATE task_runs SET outcome=?, status=?, ended_at=? WHERE id=?",
        (outcome, outcome, int(time.time()), run_id),
    )
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
        "claim_expires=NULL, worker_pid=NULL WHERE id=?",
        (task_id,),
    )
    conn.commit()


def _requeued_card(conn, pid: int, outcome: str = "crashed") -> str:
    """A card that was spawned, lost its claim (block/reclaim/timeout) and went
    back to ``ready`` — the exact state the dispatcher sees as "run me"."""
    tid = kb.create_task(conn, title="sibling", assignee="alice")
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid)
    _end_run(conn, tid, outcome)
    return tid


# ---------------------------------------------------------------------------
# The durable spawn record
# ---------------------------------------------------------------------------


def test_spawn_event_records_pid_and_create_time(kanban_home, live_child):
    """``_set_worker_pid`` must leave a record that identifies a live worker.

    The pid alone is not enough: pids are reused, and the ``worker_pid`` columns
    are NULLed the moment the card is re-queued. The create-time is what lets
    the guard tell this worker from a later process on the same pid.
    """
    psutil = pytest.importorskip("psutil")

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spawn-record", assignee="alice")
        kb.claim_task(conn, tid)
        kbd._set_worker_pid(conn, tid, live_child)
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='spawned' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (tid,),
        ).fetchone()

    payload = kb._json_dict(row["payload"])
    assert payload["pid"] == live_child
    assert payload["create_time"] == pytest.approx(
        psutil.Process(live_child).create_time(), abs=1.0
    )


# ---------------------------------------------------------------------------
# check_respawn_guard — the deferral itself
# ---------------------------------------------------------------------------


def test_guard_defers_while_previous_worker_is_alive(kanban_home, live_child):
    """A re-queued card with a live worker must be deferred, not re-spawned."""
    with kbc.connect() as conn:
        tid = _requeued_card(conn, live_child)

        assert kbd.check_respawn_guard(conn, tid) == "sibling_live"


def test_guard_releases_once_worker_is_gone(kanban_home):
    """Deferral is not a permanent block: a dead worker releases the card."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=10)
    dead_pid = proc.pid

    with kbc.connect() as conn:
        tid = _requeued_card(conn, dead_pid)

        assert kbd.check_respawn_guard(conn, tid) != "sibling_live"


def test_guard_outranks_review_lane(kanban_home, live_child):
    """The review lane returns early for every other reason — a live worker
    still wins: a review worker racing a live worker is the same bug."""
    with kbc.connect() as conn:
        tid = _requeued_card(conn, live_child)
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()

        assert kbd.check_respawn_guard(conn, tid, lane="review") == "sibling_live"


def test_guard_ignores_completed_run(kanban_home, live_child):
    """A completed run is a durable handoff — re-running that card is a
    deliberate operator action, not a race, so the guard must not claim it."""
    with kbc.connect() as conn:
        tid = _requeued_card(conn, live_child, outcome="completed")

        assert kbd.check_respawn_guard(conn, tid) != "sibling_live"


def test_guard_expires_stale_spawn_record(kanban_home, live_child, monkeypatch):
    """A leaked long-lived pid must not park the card forever: past the window
    the spawn record is treated as stale and the card is released."""
    with kbc.connect() as conn:
        tid = _requeued_card(conn, live_child)
        spawn = conn.execute(
            "SELECT id, created_at FROM task_events WHERE task_id=? AND kind='spawned' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (tid,),
        ).fetchone()

        # Same live pid, same record — only the clock moves past the window.
        monkeypatch.setattr(
            kbd.time, "time",
            lambda: spawn["created_at"] + kbd._SIBLING_LIVE_WINDOW_SECONDS + 1,
        )
        assert kbd.check_respawn_guard(conn, tid) != "sibling_live"

        # ...and inside the window it still defers (control for the line above).
        monkeypatch.setattr(kbd.time, "time", lambda: spawn["created_at"] + 60)
        assert kbd.check_respawn_guard(conn, tid) == "sibling_live"


def test_guard_ignores_reused_pid(kanban_home, live_child):
    """The same pid number is not the same worker. A process that started
    later must not inherit the deferral."""
    pytest.importorskip("psutil")

    with kbc.connect() as conn:
        tid = _requeued_card(conn, live_child)
        # Rewrite the spawn record's create-time: same pid, different process.
        conn.execute(
            "UPDATE task_events SET payload=? WHERE task_id=? AND kind='spawned'",
            ('{"pid": %d, "create_time": 1.0}' % live_child, tid),
        )
        conn.commit()

        assert kbd.check_respawn_guard(conn, tid) != "sibling_live"


def test_guard_ignores_own_process(kanban_home):
    """The dispatcher's own pid is never a sibling worker."""
    with kbc.connect() as conn:
        tid = _requeued_card(conn, os.getpid())

        assert kbd.check_respawn_guard(conn, tid) != "sibling_live"


# ---------------------------------------------------------------------------
# dispatch_once — the race that actually costs money
# ---------------------------------------------------------------------------


def test_dispatch_does_not_spawn_over_live_worker(
    kanban_home, live_child, all_assignees_spawnable,
):
    """End to end: a ready card with a live worker is never spawned again."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        tid = _requeued_card(conn, live_child)
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert spawns == []
    assert res.spawned == []
    assert (tid, "sibling_live") in res.respawn_guarded


def test_dispatch_spawns_once_live_worker_is_gone(
    kanban_home, all_assignees_spawnable,
):
    """Control: the guard defers only live workers — a normal re-queue still
    spawns on the same tick."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=10)

    with kbc.connect() as conn:
        tid = _requeued_card(conn, proc.pid)
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert spawns == [tid]
    assert res.respawn_guarded == []


# ---------------------------------------------------------------------------
# Operator surface — a guarded card must say WHY it did not spawn
# ---------------------------------------------------------------------------


def test_cli_dispatch_prints_guard_deferral(kanban_home, monkeypatch, capsys):
    """``hermes kanban dispatch`` must name a guarded card and its reason.

    Without this the deferral is invisible: the operator sees
    ``Spawned: 0`` for a card that is ready and assigned, with nothing to
    explain the hold — the classic "board looks stuck" report.
    """
    import argparse

    from hermes_cli import kanban as kanban_cli

    live = _live_kbd()
    monkeypatch.setattr(
        live,
        "dispatch_once",
        lambda conn, **kw: live.DispatchResult(
            respawn_guarded=[("t_abc", "sibling_live")],
        ),
    )

    rc = kanban_cli._cmd_dispatch(
        argparse.Namespace(dry_run=False, max=None, failure_limit=2, json=False)
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "t_abc" in out
    assert "sibling_live" in out


def test_cli_dispatch_json_carries_guard_reason(kanban_home, monkeypatch, capsys):
    """``--json`` consumers (dashboards, cron wrappers) get the reason too."""
    import argparse
    import json

    from hermes_cli import kanban as kanban_cli

    live = _live_kbd()
    monkeypatch.setattr(
        live,
        "dispatch_once",
        lambda conn, **kw: live.DispatchResult(
            respawn_guarded=[("t_abc", "sibling_live")],
        ),
    )

    kanban_cli._cmd_dispatch(
        argparse.Namespace(dry_run=False, max=None, failure_limit=2, json=True)
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["respawn_guarded"] == [
        {"task_id": "t_abc", "reason": "sibling_live"},
    ]


def test_cli_dispatch_guard_reason_survives_sibling_module_purge(
    kanban_home, monkeypatch, capsys, sibling_module_purge,
):
    """The CLI guard surface must survive a sibling file's ``sys.modules`` purge.

    Both CLI tests above patch ``kanban_db_dispatch.dispatch_once``. When an
    earlier file drops ``hermes_cli*`` from ``sys.modules`` — which
    ``test_kanban_cli_dispatch_passthrough.py`` does on every run — a
    collection-time ``import ... as kbd`` is stranded and the patch becomes a
    no-op: the CLI prints the real dispatcher's zero-summary and the operator
    never learns why a ready card was held. The seam is a call-time lookup, so
    the test must resolve it the same way.
    """
    import argparse
    import importlib

    kanban_cli = importlib.import_module("hermes_cli.kanban")
    kbd_live = _live_kbd()
    assert kbd_live is importlib.import_module("hermes_cli.kanban_db_dispatch")

    monkeypatch.setattr(
        kbd_live,
        "dispatch_once",
        lambda conn, **kw: kbd_live.DispatchResult(
            respawn_guarded=[("t_abc", "sibling_live")],
        ),
    )

    rc = kanban_cli._cmd_dispatch(
        argparse.Namespace(dry_run=False, max=None, failure_limit=2, json=False)
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "t_abc" in out
    assert "sibling_live" in out
