"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent import delegation_context
from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    # HERMES_DELEGATED_CHILD_CONTEXT is set in any delegate child process; left in
    # place, ``is_delegated_child_process_context()`` is True and every nudge is
    # suppressed, so a pytest run launched from a worker would fail the tests that
    # assert the guard fires.
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_STOP_NUDGE",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


@pytest.mark.parametrize("tool_name", ["kanban_request_review", "kanban_request_changes"])
def test_no_nudge_after_lane_handoff(clear_kanban_env, tool_name):
    """A lane handoff closes the dispatcher run, so it is terminal, not a violation."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_lane")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": tool_name, "tool_call_id": "1", "content": "handed off"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_nudge_suppressed_only_when_another_lane_owns_the_card(clear_kanban_env, tmp_path):
    """The false 'protocol violation' case: run 142 ended, the dispatcher handed the
    card to run 145, and the still-alive 142 session was nagged about 145's card."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    board_path = tmp_path / "kanban.db"
    conn = kbc.connect(db_path=board_path)
    try:
        task_id = kb.create_task(conn, title="lane handoff on the review step")
        conn.execute(
            "UPDATE tasks SET status = 'running', current_run_id = 145 WHERE id = ?",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", task_id)
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(board_path))
    narration_only = [
        {"role": "user", "content": "review this card"},
        {"role": "assistant", "content": "Summary of what I found, handing back next."},
    ]

    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "142")
    assert build_kanban_stop_nudge(messages=narration_only) is None

    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "145")
    assert build_kanban_stop_nudge(messages=narration_only) is not None

    # Acceptance's other half: a card with no live run at all is still the guard's
    # business, whoever is asking.
    board = kbc.connect(db_path=board_path)
    try:
        board.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,))
        board.commit()
    finally:
        board.close()
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "142")
    assert build_kanban_stop_nudge(messages=narration_only) is not None


def test_no_nudge_outside_dispatcher_owned_context(clear_kanban_env):
    """A cron run / delegate child inherits HERMES_KANBAN_* but does not own the card."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_someone_elses_card")
    narration_only = [
        {"role": "user", "content": "cron job fired inside a worker process"},
        {"role": "assistant", "content": "Just narrating."},
    ]
    with delegation_context.non_dispatcher_owned_context():
        assert kanban_stop_nudge_enabled() is False
        assert build_kanban_stop_nudge(messages=narration_only) is None






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




