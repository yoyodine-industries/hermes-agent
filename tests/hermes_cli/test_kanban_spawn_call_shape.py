"""The dispatcher's spawn-call shape is decided by BINDING, never by calling.

Measured on the ops board 2026-09-23: seven cards in one tick were recorded
``spawn_failed: boom() takes 1 positional argument but 2 were given`` and were
spawned normally twelve seconds later. ``boom`` names nothing in the spawn path —
the name came from the *caller's own* ``spawn_fn`` — because ``_call_spawn_fn``
decided the call shape by CALLING it: it invoked the callable with two positional
arguments, caught the resulting ``TypeError``, invoked it a second time, and let
that second ``TypeError`` escape into the card's ``task_runs.error``. Two
consequences, both asserted below:

* a spawn callable is side-effecting (it starts a worker), so deciding its arity
  by calling it invokes it twice;
* the recorded error described the callable's arity, not the callable, which is
  why the card read as a defect in a function that exists nowhere.

A callable the dispatcher cannot call must be refused BEFORE it is invoked, and
the refusal must name it and the shape it failed. Silently adapting (calling
``boom(task)``) would be worse than refusing: a one-argument spawn callable
cannot place the worker's workspace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

# ``_call_spawn_fn`` only forwards ``task`` to the callable, so the call-shape
# tests need any object at all; the real Task is built by the dispatcher.
_A_TASK: Any = SimpleNamespace(id="t_x")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _latest_run(conn, task_id: str):
    return conn.execute(
        "SELECT outcome, error FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# The call shape is decided without invoking the callable
# ---------------------------------------------------------------------------


def test_one_argument_spawn_fn_is_refused_without_being_invoked():
    """The measured incident shape: ``def boom(task)`` handed to the dispatcher."""
    calls: list = []

    def boom(task):
        calls.append(task)
        return 4242

    with pytest.raises(TypeError) as excinfo:
        kbd._call_spawn_fn(boom, _A_TASK, "/tmp/ws", None)

    assert calls == [], (
        "the arity is decided by binding the signature: a wrong-arity callable must "
        "never be invoked (the old shim invoked it twice)"
    )
    message = str(excinfo.value)
    assert "boom" in message, "the refusal must name the callable the dispatcher cannot call"
    assert "workspace" in message, "the refusal must state the shape the dispatcher calls with"


def test_spawn_that_raises_inside_is_invoked_once_and_its_own_error_survives():
    """A failure INSIDE a correctly-shaped spawn must not be re-invoked or masked."""
    calls: list = []

    def boom(task, workspace, board=None):
        calls.append(task)
        raise TypeError("worker argv exploded")

    with pytest.raises(TypeError, match="worker argv exploded"):
        kbd._call_spawn_fn(boom, _A_TASK, "/tmp/ws", None)

    assert len(calls) == 1, "a spawn is side-effecting; it is never re-invoked to probe arity"


def test_uninspectable_spawn_fn_keeps_the_historical_two_argument_shape():
    """An un-introspectable callable is still called (board dropped), never refused."""
    calls: list = []

    class Boom:
        @property
        def __signature__(self):
            raise ValueError("no signature here")

        def __call__(self, task, workspace):
            calls.append((task, workspace))
            return 7

    assert kbd._call_spawn_fn(Boom(), "t_x", "/tmp/ws", None) == 7
    assert calls == [("t_x", "/tmp/ws")]


# ---------------------------------------------------------------------------
# The dispatcher's call shape, end to end, through a real tick
# ---------------------------------------------------------------------------


def test_two_argument_spawn_fn_still_spawns_through_dispatch_once(conn, all_assignees_spawnable):
    """Back-compat contract: the older ``(task, workspace)`` stub still spawns."""
    task_id = kb.create_task(conn, title="t", assignee="w")
    seen: list = []

    def spawn(task, workspace):
        seen.append((task.id, workspace))
        return 4242

    result = kbd.dispatch_once(conn, spawn_fn=spawn, max_in_progress=4)

    assert [tid for tid, _ws in seen] == [task_id]
    assert [tid for tid, _assignee, _ws in result.spawned] == [task_id]


def test_one_argument_spawn_fn_fails_its_card_without_killing_the_tick(
    conn, all_assignees_spawnable,
):
    """The tick survives a spawn callable it cannot call, and says why.

    The card is the unit of failure: ``dispatch_once`` returns normally (the
    refusal is caught per card), the card carries an actionable ``spawn_failed``
    error, and no worker is reported spawned. The old shim recorded the bare arity
    message ``boom() takes 1 positional argument but 2 were given`` — naming
    nothing the operator can act on.
    """
    task_id = kb.create_task(conn, title="t", assignee="w")

    def boom(task):
        raise AssertionError("a spawn_fn the dispatcher cannot call must never be invoked")

    result = kbd.dispatch_once(conn, spawn_fn=boom, max_in_progress=4)

    assert result.spawned == []
    row = _latest_run(conn, task_id)
    assert row["outcome"] == "spawn_failed"
    assert "boom" in row["error"], "the recorded error must name the offending callable"
    assert "workspace" in row["error"], "the recorded error must state the shape it needs"
