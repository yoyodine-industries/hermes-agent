"""The cross-sibling close TOOL surface (``kanban_close_sibling``, card t_fbc22829).

The kernel writer is covered in ``tests/hermes_cli/test_kanban_cross_close.py``. This file
covers the half a worker actually reaches: the tool exists on the worker's schema with the
declared shape, the auditing end is the worker's OWN card (never a caller-supplied id), and
the fences that make a worker mutation safe still hold.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def worker_board(monkeypatch, tmp_path):
    """A claimed caller card (this worker) beside a sibling card to close."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        sibling = kb.create_task(conn, title="the sibling card", assignee="platform-coder")
        caller = kb.create_task(conn, title="the lead's own card", assignee="test-worker")
        kb.claim_task(conn, caller)
        run_id = kb._current_run_id(conn, caller)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", caller)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return sibling, caller, run_id


def _board():
    from hermes_cli import kanban_db_connect as kbc

    return kbc.connect()


def test_the_tool_is_on_the_workers_schema(worker_board):
    """A worker must SEE the door: an unregistered tool is not a door."""
    import tools.kanban_tools  # registration happens on module import
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    assert "kanban_close_sibling" in names


def test_the_declared_shape_is_exactly_the_contract(worker_board):
    """No ``board`` property: the door must not be a second, unpinned board handle."""
    import tools.kanban_tools
    from tools.registry import registry

    schema = registry.get_schema("kanban_close_sibling")
    assert set(schema["parameters"]["properties"]) == {
        "sibling_task_id", "summary", "result",
    }
    assert schema["parameters"]["required"] == ["sibling_task_id"]
    assert "board" not in schema["parameters"]["properties"]


def test_a_worker_closes_a_sibling_and_the_caller_is_audited(worker_board):
    sibling, caller, run_id = worker_board
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_close_sibling({
        "sibling_task_id": sibling, "summary": "absorbed into my branch",
    })
    receipt = json.loads(out)

    assert receipt["ok"] is True
    assert receipt["target"] == sibling
    assert receipt["status"] == "done"
    assert receipt["caller"] == caller, "the worker's own card is the auditing end"

    conn = _board()
    try:
        assert kb.get_task(conn, sibling).status == "done"
        result = kb.get_task(conn, sibling).result
        assert "DELEGATED" in result and caller in result and f"run {run_id}" in result
        comments = kb.list_comments(conn, sibling)
        assert comments[-1].author == "hermes-kanban-close-sibling"
        assert caller in comments[-1].body
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND kind LIKE 'sibling_close%'",
            (caller,))]
        assert kinds == ["sibling_close_recorded"]
    finally:
        conn.close()


def test_the_tool_refuses_the_workers_own_card(worker_board):
    _sibling, caller, _run_id = worker_board
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_close_sibling({"sibling_task_id": caller}))

    assert out["error"].startswith("kanban_close_sibling")
    assert "kanban_complete" in out["error"]
    conn = _board()
    try:
        from hermes_cli import kanban_db as kb

        assert kb.get_task(conn, caller).status == "running", "the caller was not touched"
    finally:
        conn.close()


def test_the_tool_refuses_unknown_parameters(worker_board):
    """The registry guard runs before the handler: a typo cannot half-close."""
    sibling, _caller, _run_id = worker_board
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_close_sibling({
        "sibling_task_id": sibling, "target_task_id": sibling,
    }))

    assert "unknown parameter" in out["error"]


def test_a_delegate_child_cannot_use_the_door(worker_board, monkeypatch):
    """delegate_task children share the parent's process; inherited env is not ownership."""
    sibling, _caller, _run_id = worker_board
    from tools import kanban_tools as kt
    from agent import delegation_context

    monkeypatch.setattr(delegation_context, "is_delegated_child_process_context",
                        lambda default=False: True)
    refused = json.loads(kt._handle_close_sibling({"sibling_task_id": sibling}))

    assert "delegate_task child" in refused["error"]
    from hermes_cli import kanban_db as kb

    conn = _board()
    try:
        assert kb.get_task(conn, sibling).status != "done", "nothing was closed"
    finally:
        conn.close()
