"""The landing-verdict TOOL surface (``kanban_record_landing_verdict``, card t_765136ce).

The kernel writer is covered in ``tests/hermes_cli/test_kanban_landing_verdict.py``. This file
covers the half that actually unblocks a worker: the tool exists on the worker's schema, and a
worker calling it -- with its own ``HERMES_KANBAN_TASK`` as the auditing caller -- writes the
verdict on the DECLARING card and gets a receipt back.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def worker_board(monkeypatch, tmp_path):
    """A worker session (caller card claimed) next to a closed DECLARING card."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        pr = "https://github.com/yoyodine-industries/yaan-hermes-config/pull/63"
        declaring = kb.create_task(conn, title="the declaring card", assignee="platform-coder")
        kb._synthesize_ended_run(
            conn, declaring, outcome="completed", summary="closed; this PR is the deliverable",
            metadata={"published_pr": pr},
        )
        caller = kb.create_task(conn, title="[land] carrier", assignee="test-worker")
        kb.claim_task(conn, caller)
        run_id = kb._current_run_id(conn, caller)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", caller)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return declaring, caller


REASON = "superseded by yoyodine-industries/yaan-hermes-config#64, which lands the same rule"


def test_the_tool_is_on_the_workers_schema(worker_board):
    """A worker must SEE the door: an unregistered tool is not a door."""
    import tools.kanban_tools  # noqa: F401 -- registration happens on module import
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    assert "kanban_record_landing_verdict" in names


def test_a_worker_records_the_verdict_and_the_caller_is_audited(worker_board):
    declaring, caller = worker_board
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    out = kt._handle_record_landing_verdict({
        "target_task_id": declaring, "verdict": "landing_not_owed", "reason": REASON,
    })
    receipt = json.loads(out)

    assert receipt["ok"] is True
    assert receipt["target"] == declaring
    assert receipt["caller"] == caller, "the worker's own card is the second audit end"
    assert receipt["changed"] is True

    conn = kbc.connect()
    try:
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (declaring,)
        ).fetchone()
        assert json.loads(row["metadata"])["landing_not_owed"] == REASON
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND kind LIKE 'landing_verdict%'", (caller,)
        )]
        assert kinds == ["landing_verdict_recorded"]
    finally:
        conn.close()


def test_the_tool_refuses_a_reason_the_clause_would_refuse(worker_board):
    declaring, _caller = worker_board
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_record_landing_verdict({
        "target_task_id": declaring, "verdict": "landing_not_owed",
        "reason": "this is not owed any more, it was superseded last week",
    }))

    assert out["error"].startswith("kanban_record_landing_verdict")
    assert "landing_not_owed" in out["error"]


def test_the_tool_refuses_unknown_parameters(worker_board):
    """The registry guard runs before the handler: a typo cannot half-write."""
    declaring, _caller = worker_board
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_record_landing_verdict({
        "target_task_id": declaring, "verdict": "landing_not_owed", "reason": REASON,
        "target_task": declaring,
    }))

    assert "unknown parameter" in out["error"]
