"""Tests for kanban lifecycle plugin hooks.

Verifies that claim/complete/block transitions fire the
kanban_task_claimed / kanban_task_completed / kanban_task_blocked plugin
hooks AFTER the board DB change is committed, with the documented kwargs,
and that a misbehaving hook callback never breaks the transition.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.plugins import VALID_HOOKS, get_plugin_manager


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def captured_hooks(monkeypatch):
    """Register capturing callbacks for the three kanban lifecycle hooks.

    Patches the plugin manager's _hooks dict directly (the same registry
    invoke_hook reads) and restores it afterward.
    """
    mgr = get_plugin_manager()
    events: list[tuple[str, dict]] = []
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    for hook in ("kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked"):
        mgr._hooks.setdefault(hook, []).append(
            lambda _h=hook, **kw: events.append((_h, kw))
        )
    try:
        yield events
    finally:
        mgr._hooks = saved




def test_claim_fires_hook(kanban_home, captured_hooks):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
    finally:
        conn.close()
    fired = [e for e in captured_hooks if e[0] == "kanban_task_claimed"]
    assert len(fired) == 1
    kw = fired[0][1]
    assert kw["task_id"] == tid
    assert kw["assignee"] == "worker"
    assert "profile_name" in kw
    assert kw["run_id"] is not None




def test_misbehaving_hook_does_not_break_transition(kanban_home, monkeypatch):
    """A hook callback that raises must not break the board transition."""
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}

    def _boom(**kw):
        raise RuntimeError("plugin exploded")

    mgr._hooks.setdefault("kanban_task_completed", []).append(_boom)
    try:
        conn = kbc.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            kb.claim_task(conn, tid)
            # Despite the raising hook, completion succeeds and persists.
            assert kb.complete_task(conn, tid, summary="ok") is True
            assert kb.get_task(conn, tid).status == "done"
        finally:
            conn.close()
    finally:
        mgr._hooks = saved


# --- the blocked-card trigger is the SINGLE store function (card t_64412a4a) --------
# The escalation of a blocked card is fired from ``kanban_db.block_task``, which is
# the ONE store function every caller routes through. Wiring it into a caller instead
# (the CLI, or the kanban tool) would leave the other path blocking a card and
# registering nothing - the backdoor this change exists to close. These cases fail if
# that coverage is ever lost, by driving BOTH entry points and counting the fires.


def _running_card(title: str) -> str:
    """A claimed card, so a block is an admitted running->blocked transition."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=title, assignee="worker")
        assert kb.claim_task(conn, tid) is not None
        return tid


def test_block_hook_carries_the_block_kind(kanban_home, captured_hooks):
    """A consumer must be able to decide from the payload alone - no board re-read."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="needs a decision", kind="needs_input") is True
    finally:
        conn.close()

    fired = [e for e in captured_hooks if e[0] == "kanban_task_blocked"]
    assert len(fired) == 1
    kw = fired[0][1]
    assert kw["task_id"] == tid
    assert kw["assignee"] == "worker"
    assert kw["block_kind"] == "needs_input"
    assert kw["reason"] == "needs a decision"
    assert "board" in kw


def test_both_block_entry_points_fire_exactly_one_hook(kanban_home, captured_hooks):
    """The CLI and the kanban tool each block through ``block_task``: one fire apiece."""
    import argparse

    from hermes_cli import kanban as kanban_cli
    from tools import kanban_tools

    # (a) the CLI entry point: `hermes kanban block <id> --kind needs_input <reason>`
    cli_id = _running_card("cli-blocked")
    args = argparse.Namespace(task_id=cli_id, ids=None,
                              reason=["needs", "an", "operator", "decision"],
                              kind="needs_input")
    assert kanban_cli._cmd_block(args) == 0

    # (b) the kanban TOOL entry point a dispatched worker calls
    tool_id = _running_card("tool-blocked")
    kanban_tools._handle_block({"task_id": tool_id, "reason": "needs a capability",
                                "kind": "capability"})

    fired = [e for e in captured_hooks if e[0] == "kanban_task_blocked"]
    by_task: dict = {}
    for _event, kw in fired:
        by_task.setdefault(kw["task_id"], []).append(kw)

    assert set(by_task) == {cli_id, tool_id}, "both entry points must reach the store function"
    assert len(by_task[cli_id]) == 1, "the CLI path fires exactly once"
    assert len(by_task[tool_id]) == 1, "the tool path fires exactly once"
    assert by_task[cli_id][0]["block_kind"] == "needs_input"
    assert by_task[tool_id][0]["block_kind"] == "capability"
