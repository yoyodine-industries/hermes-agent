"""Convergence guard: a card whose completion contract can never be satisfied.

The respawn guard's ``active_pr`` reason holds a ready card every tick while a
GitHub PR URL sits in a recent comment. For a card whose worker opened a PR but
whose completion contract can never pass (e.g. a branch-rules API 403 on a
private repo), that hold never clears: the dispatcher writes one
``respawn_guarded`` event per tick, forever, and no worker is ever re-spawned —
measured at 310+ ``active_pr`` holds on one live card before this guard existed.

The convergence guard counts CONSECUTIVE guard holds of a *stuck* reason
(``active_pr``) and, at ``_NONCONVERGENCE_LIMIT``, parks the card ``blocked`` /
``transient`` with a named blocker instead of silently spinning. Transient holds
(``sibling_live``, ``rate_limit_cooldown``, ``recent_success``) clear on their
own and must never be parked by this guard.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

# Matches ``kanban_db_dispatch._NONCONVERGENCE_LIMIT``. Kept as a literal so the
# test's red-on-base is behavioural (the card is never parked) rather than a
# missing-constant AttributeError — the test asserts the *outcome*, not the knob.
_LIMIT = 3


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def live_child():
    """A real, live process standing in for a still-running worker."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not kbd._pid_alive(proc.pid):
            time.sleep(0.05)
        yield proc.pid
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _requeued_card(conn, pid: int) -> str:
    """A card that was spawned, lost its claim and went back to ``ready`` with a
    live worker process — the ``sibling_live`` hold state."""
    tid = kb.create_task(conn, title="sibling", assignee="alice")
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid)
    task = kb.get_task(conn, tid)
    assert task is not None
    run_id = task.current_run_id
    conn.execute(
        "UPDATE task_runs SET outcome=?, status=?, ended_at=? WHERE id=?",
        ("crashed", "crashed", int(time.time()), run_id),
    )
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
        "claim_expires=NULL, worker_pid=NULL WHERE id=?",
        (tid,),
    )
    conn.commit()
    return tid


def test_convergence_guard_parks_card_held_by_active_pr(
    kanban_home, all_assignees_spawnable,
):
    """A card held by ``active_pr`` N ticks in a row is parked, not re-spun.

    Reproduces the measured loop: the worker opened a PR (a PR URL comment
    exists) and the completion contract cannot pass, so the respawn guard holds
    the card every tick. After ``_NONCONVERGENCE_LIMIT`` holds the card must be
    ``blocked``/``transient`` with the reason recorded.
    """
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unsatisfiable-contract", assignee="alice")
        kb.add_comment(conn, tid, "alice", "Opened PR: https://github.com/org/repo/pull/42")

        res = None
        for _ in range(_LIMIT):
            res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    # The guard held it every tick — never spawned.
    assert spawns == []

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "transient"
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        reason = kb._json_dict(row["payload"]).get("reason", "")
        assert "convergence guard" in reason
        assert "active_pr" in reason

    # Reported on the tick that parked it.
    assert (tid, "active_pr") in res.non_converging


def test_convergence_guard_ignores_sibling_live_hold(
    kanban_home, all_assignees_spawnable, live_child,
):
    """A card held by a transient reason (``sibling_live``) is never parked.

    ``sibling_live`` clears when the worker process exits, so holding it for N
    ticks must NOT trip the convergence guard — only a *stuck* reason
    (``active_pr``) converges to a park.
    """
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        tid = _requeued_card(conn, live_child)

        res = None
        for _ in range(_LIMIT + 1):
            res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert spawns == []
    assert res.respawn_guarded and all(
        reason == "sibling_live" for _tid, reason in res.respawn_guarded
    )
    assert res.non_converging == []

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.block_kind is None


def test_converging_card_spawns_and_is_not_parked(
    kanban_home, all_assignees_spawnable,
):
    """Control: a normal ready card with no PR URL spawns and is never parked."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="normal", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert spawns == [tid]
    assert res.non_converging == []
    assert res.respawn_guarded == []

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status != "blocked"


def test_cli_dispatch_reports_non_converging_card(kanban_home, monkeypatch, capsys):
    """``hermes kanban dispatch`` names a parked card and its reason.

    The report line is the operator's signal that a card will never finish on
    its own — without it the park is invisible and the board still looks stuck.
    """
    import argparse

    from hermes_cli import kanban as kanban_cli

    monkeypatch.setattr(
        kbd,
        "dispatch_once",
        lambda conn, **kw: kbd.DispatchResult(
            non_converging=[("t_abc", "active_pr")],
        ),
    )

    rc = kanban_cli._cmd_dispatch(
        argparse.Namespace(dry_run=False, max=None, failure_limit=2, json=False)
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "Non-converging" in out
    assert "t_abc" in out
    assert "active_pr" in out


def test_cli_dispatch_json_carries_non_converging(kanban_home, monkeypatch, capsys):
    """``--json`` consumers get the parked card and reason too."""
    import argparse
    import json

    from hermes_cli import kanban as kanban_cli

    monkeypatch.setattr(
        kbd,
        "dispatch_once",
        lambda conn, **kw: kbd.DispatchResult(
            non_converging=[("t_abc", "active_pr")],
        ),
    )

    kanban_cli._cmd_dispatch(
        argparse.Namespace(dry_run=False, max=None, failure_limit=2, json=True)
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["non_converging"] == [
        {"task_id": "t_abc", "reason": "active_pr"},
    ]
