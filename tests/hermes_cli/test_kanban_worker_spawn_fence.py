"""A dispatcher-owned worker spawn must NOT carry the delegated-child fence marker (t_1623ec8c).

WHY
---
``HERMES_DELEGATED_CHILD_CONTEXT`` is the write fence: ``kanban_path_is_fenced`` refuses every board
mutation from any process carrying it, and that refusal is what ``write_txn`` raises as
``PermissionError: delegate_task child contexts cannot mutate Kanban tasks or boards``.

Every Hermes-owned spawn boundary MANUFACTURES the marker for a DESCENDANT
(``agent.delegation_context.delegated_child_subprocess_env`` -> ``scrub_kanban_env``): the worker's
terminal session, its ``execute_code`` sandbox and any subprocess an in-process ``delegate_task``
child launches are all fenced, deliberately and correctly.

The danger is the OTHER direction. ``_default_spawn`` builds the worker env from
``build_subprocess_env``, which snapshots the DISPATCHER's ``os.environ`` — and that snapshot is
re-fenced whenever the dispatcher carries the marker itself (a dispatcher launched from a worker's
shell) or carries worker identity (``HERMES_KANBAN_TASK``, a dispatcher that IS a worker). The
dispatcher's OWN spawn is the scope grant, so the worker must land on the board with ``HERMES_KANBAN_*``
identity and NO marker; the strip is one ``env.pop`` line and is silent when it regresses — the worker
then cannot close its own card (``kanban_complete``, ``kanban_request_review``, ``kanban_comment``
and the auto-heartbeat bridge all refuse, logging at DEBUG only).

This suite pins both halves of the boundary: the spawned worker is unfenced, its descendants are not.
"""
import os

import pytest

from hermes_cli import kanban_db_dispatch
from hermes_cli.kanban_db import Task

MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"

#: Ambient pins a real dispatched worker shell exports; cleared so the suite never reads the live board.
_AMBIENT_PINS = ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                 "HERMES_KANBAN_WORKSPACES_ROOT")


class _StopSpawn(Exception):
    """Abort ``_default_spawn`` after the env is built so no worker process is created."""


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A hermetic HERMES_HOME so profile/board resolution never touches the live install."""
    home = tmp_path / "fakehome" / ".hermes"
    (home / "profiles" / "platform-coder").mkdir(parents=True)
    (home / "profiles" / "platform-coder" / "config.yaml").write_text("toolsets: [kanban]\n",
                                                                     encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (*_AMBIENT_PINS, MARKER):
        monkeypatch.delenv(var, raising=False)
    return home


def _captured_worker_env(monkeypatch, tmp_path):
    """Run ``_default_spawn`` far enough to capture the worker env, never spawning anything."""
    from tools import process_registry

    captured: list[dict] = []

    def _capture(env):
        captured.append(dict(env))
        raise _StopSpawn

    monkeypatch.setattr(process_registry, "systemd_user_bus_env", _capture)
    task = Task(
        id="t_fence", title="t", body=None, assignee="platform-coder", status="claimed",
        priority=0, created_by=None, created_at=0, started_at=None, completed_at=None,
        workspace_kind="dir", workspace_path=None, claim_lock=None, claim_expires=None,
        tenant=None)
    with pytest.raises(_StopSpawn):
        kanban_db_dispatch._default_spawn(task, str(tmp_path / "ws"))
    assert captured, "_default_spawn never built a worker env"
    return captured[0]


@pytest.mark.parametrize("dispatcher_state", ["clean", "fenced", "is-a-worker"])
def test_dispatcher_owned_spawn_never_carries_the_fence_marker(
        fake_home, tmp_path, monkeypatch, dispatcher_state):
    """The worker lands with worker identity and no fence, whatever the dispatcher's own state."""
    if dispatcher_state == "fenced":
        # A dispatcher launched from a worker's shell (or any fenced parent).
        monkeypatch.setenv(MARKER, str(fake_home))
    elif dispatcher_state == "is-a-worker":
        # A worker running the dispatcher: build_subprocess_env re-fences the snapshot for it.
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_dispatcher")

    env = _captured_worker_env(monkeypatch, tmp_path)

    assert env.get("HERMES_KANBAN_TASK") == "t_fence", (
        f"the worker did not receive its own task identity: {env.get('HERMES_KANBAN_TASK')!r}")
    assert not env.get(MARKER), (
        f"dispatcher_state={dispatcher_state}: the worker inherited the delegated-child fence, so "
        "every board mutation from its own session is refused (kanban_complete / heartbeat)")


def test_the_workers_own_board_is_admitted_and_its_descendants_are_fenced(
        fake_home, tmp_path, monkeypatch):
    """Both halves at once: worker verdict False, descendant verdict True on the same store."""
    from agent.delegation_context import delegated_child_subprocess_env, kanban_path_is_fenced

    worker_env = _captured_worker_env(monkeypatch, tmp_path)
    board_db = worker_env["HERMES_KANBAN_DB"]

    # 1. The worker's own identity: prefix-free env, so its mutations are admitted.
    monkeypatch.setenv("HERMES_KANBAN_TASK", worker_env["HERMES_KANBAN_TASK"])
    monkeypatch.setenv("HERMES_KANBAN_DB", board_db)
    assert kanban_path_is_fenced(board_db) is False, (
        "the dispatcher's own worker was fenced against its own board")

    # 2. A DESCENDANT built from that env stays fenced and loses the worker identity.
    descendant = delegated_child_subprocess_env(worker_env)
    assert descendant.get(MARKER), "a descendant of the worker lost the fence"
    assert not descendant.get("HERMES_KANBAN_TASK"), (
        "a descendant inherited worker identity (it would be treated as the worker)")
    monkeypatch.setenv(MARKER, descendant[MARKER])
    assert kanban_path_is_fenced(board_db) is True, (
        "a descendant of the worker could mutate the board: the fence was relaxed")
    assert os.environ.get("HERMES_KANBAN_TASK"), "sanity: worker identity was pinned for this read"
