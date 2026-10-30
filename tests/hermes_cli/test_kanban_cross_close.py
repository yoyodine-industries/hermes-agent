"""The cross-sibling close: a lane lead closes its SIBLING card through the kernel.

Card ``t_fbc22829``. The shape under test is the one the board's completion clause
already owns: :func:`hermes_cli.kanban_cross_close.close_sibling` never writes a status
column -- it calls ``complete_task`` -- so the parent gate, the acceptance receipt and the
landing-carrier clause still refuse a close they would refuse anyway.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ACTOR = "hermes-kanban-close-sibling"


@pytest.fixture
def board(monkeypatch, tmp_path):
    """A private board (temp HERMES_HOME), no tenant/board pins inherited."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-cross-close")
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT",
                "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        yield kb, conn
    finally:
        conn.close()


def _card(kb, conn, title="a card", **kw):
    return kb.create_task(conn, title=title, assignee="platform-coder", **kw)


def _caller(kb, conn, title="the lead card"):
    """A caller card with a claimed run, so its audit events carry a run id."""
    caller = _card(kb, conn, title)
    kb.claim_task(conn, caller)
    return caller, kb._current_run_id(conn, caller)


def _events(conn, task_id, kind):
    return [json.loads(r["payload"] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind))]


# --------------------------------------------------------------------------- unit surface


def test_the_target_is_completed_through_the_kernel(board):
    kb, conn = board
    from hermes_cli.kanban_cross_close import close_sibling

    sibling = _card(kb, conn, "sibling work")
    caller, run_id = _caller(kb, conn)

    receipt = close_sibling(conn, sibling, caller_task_id=caller, caller_run_id=run_id,
                            summary="absorbed into the lead's branch")

    assert receipt["target"] == sibling
    assert receipt["status"] == "done"
    assert receipt["caller"] == caller
    assert kb.get_task(conn, sibling).status == "done"
    run = kb.latest_run(conn, sibling)
    assert run.ended_at is not None and run.outcome == "completed"


def test_the_record_says_the_close_was_delegated_and_names_the_caller(board):
    """A sibling's history must never read as if its own worker closed it."""
    kb, conn = board
    from hermes_cli.kanban_cross_close import close_sibling

    sibling = _card(kb, conn, "sibling work")
    caller, run_id = _caller(kb, conn)

    close_sibling(conn, sibling, caller_task_id=caller, caller_run_id=run_id,
                  summary="absorbed into the lead's branch")

    result = kb.get_task(conn, sibling).result
    assert "DELEGATED" in result
    assert caller in result
    assert f"run {run_id}" in result

    run = kb.latest_run(conn, sibling)
    assert "DELEGATED" in (run.summary or "")
    assert run.metadata["delegated_close_from"] == caller
    assert run.metadata["delegated_close_run_id"] == run_id


def test_one_comment_on_the_sibling_names_the_caller(board):
    kb, conn = board
    from hermes_cli.kanban_cross_close import ACTOR, close_sibling

    sibling = _card(kb, conn, "sibling work")
    caller, run_id = _caller(kb, conn)

    close_sibling(conn, sibling, caller_task_id=caller, caller_run_id=run_id,
                  summary="absorbed into the lead's branch")

    comments = kb.list_comments(conn, sibling)
    assert comments, "the sibling must carry the audit comment"
    last = comments[-1]
    assert last.author == ACTOR
    assert caller in last.body
    assert "absorbed into the lead's branch" in last.body


def test_both_cards_carry_the_audit_event(board):
    kb, conn = board
    from hermes_cli.kanban_cross_close import close_sibling

    sibling = _card(kb, conn, "sibling work")
    caller, run_id = _caller(kb, conn)

    close_sibling(conn, sibling, caller_task_id=caller, caller_run_id=run_id)

    on_target = _events(conn, sibling, "sibling_close_recorded")
    on_caller = _events(conn, caller, "sibling_close_recorded")
    assert len(on_target) == 1 and len(on_caller) == 1
    assert on_target[0]["caller_task_id"] == caller
    assert on_caller[0]["target_task_id"] == sibling


# ------------------------------------------------------------------------------- refusals


def test_the_caller_may_not_close_its_own_card(board):
    """A self-close must ride kanban_complete's run-ownership proof, not this door."""
    kb, conn = board
    from hermes_cli.kanban_cross_close import CrossCloseRefused, close_sibling

    caller, run_id = _caller(kb, conn)

    with pytest.raises(CrossCloseRefused) as excinfo:
        close_sibling(conn, caller, caller_task_id=caller, caller_run_id=run_id)

    assert "kanban_complete" in str(excinfo.value)
    assert kb.get_task(conn, caller).status == "running", "the caller card was not touched"


def test_an_unknown_target_is_refused(board):
    kb, conn = board
    from hermes_cli.kanban_cross_close import CrossCloseRefused, close_sibling

    caller, run_id = _caller(kb, conn)

    with pytest.raises(CrossCloseRefused) as excinfo:
        close_sibling(conn, "t_0000000000", caller_task_id=caller, caller_run_id=run_id)

    assert "does not exist" in str(excinfo.value)


def test_an_archived_target_is_refused(board):
    kb, conn = board
    from hermes_cli.kanban_cross_close import CrossCloseRefused, close_sibling

    sibling = _card(kb, conn, "already archived")
    kb.archive_task(conn, sibling, reason="superseded")
    caller, run_id = _caller(kb, conn)

    with pytest.raises(CrossCloseRefused) as excinfo:
        close_sibling(conn, sibling, caller_task_id=caller, caller_run_id=run_id)

    assert "archived" in str(excinfo.value)
    assert kb.get_task(conn, sibling).status == "archived"


def test_an_already_done_target_is_refused(board):
    kb, conn = board
    from hermes_cli.kanban_cross_close import CrossCloseRefused, close_sibling

    sibling = _card(kb, conn, "already closed")
    kb.complete_task(conn, sibling, result="closed by its own worker")
    before = kb.get_task(conn, sibling).result
    caller, run_id = _caller(kb, conn)

    with pytest.raises(CrossCloseRefused) as excinfo:
        close_sibling(conn, sibling, caller_task_id=caller, caller_run_id=run_id,
                      summary="a second close")

    assert "already done" in str(excinfo.value)
    assert kb.get_task(conn, sibling).result == before, "nothing was rewritten"


def test_a_live_run_is_refused_by_the_claim_fence(board):
    """The claim fence decides liveness; the door reports it and writes nothing."""
    import os

    kb, conn = board
    from hermes_cli.kanban_cross_close import CrossCloseRefused, close_sibling

    sibling = _card(kb, conn, "another lane is on it")
    assert kb.claim_task(conn, sibling) is not None
    # A claim only protects a run when it records a LIVE worker process (the fence's own
    # liveness authority). This test process is that worker.
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), sibling))
    conn.commit()
    caller, run_id = _caller(kb, conn)

    with pytest.raises(CrossCloseRefused) as excinfo:
        close_sibling(conn, sibling, caller_task_id=caller, caller_run_id=run_id)

    message = str(excinfo.value)
    assert "live claim" in message
    assert kb.get_task(conn, sibling).status == "running", "the live worker still owns it"
    assert kb.list_comments(conn, sibling) == [], "no audit comment on a refused close"


def test_a_completion_gate_refusal_propagates_and_leaves_everything_alone(board, monkeypatch):
    """The clause is the kernel's: a cause it returns is a cause the door reports."""
    kb, conn = board
    from hermes_cli import kanban_cross_close as cc

    monkeypatch.setattr(
        kb, "complete_task",
        lambda *a, **kw: kb.CompletionRefusal(
            "landing_carrier_missing",
            "declares an unmerged PR with no carrier and no recorded verdict"),
    )
    sibling = _card(kb, conn, "declares an unlanded PR")
    child = _card(kb, conn, "its child", parents=[sibling])
    caller, run_id = _caller(kb, conn)

    with pytest.raises(cc.CrossCloseRefused) as excinfo:
        cc.close_sibling(conn, sibling, caller_task_id=caller, caller_run_id=run_id)

    assert "landing_carrier_missing" in str(excinfo.value)
    assert kb.get_task(conn, sibling).status != "done", "the target was not closed"
    assert kb.list_comments(conn, sibling) == []
    assert _events(conn, sibling, "sibling_close_recorded") == []
    assert kb.get_task(conn, child).status == "todo", "no child was promoted"
    # The refusal is recorded on the caller, so a lead does not retry it blind.
    refused = _events(conn, caller, "sibling_close_refused")
    assert len(refused) == 1 and refused[0]["target_task_id"] == sibling


def test_the_real_landing_clause_refuses_a_declared_unlanded_pr(board):
    """The deployed clause, not a stub: a declared PR with no landing leg is refused."""
    pytest.importorskip("hermes_cli.kanban_landing_carrier")
    kb, conn = board
    from hermes_cli.kanban_cross_close import CrossCloseRefused, close_sibling

    sibling = _card(kb, conn, "closed green on a declared PR")
    kb._synthesize_ended_run(
        conn, sibling, outcome="review_requested", summary="review me",
        metadata={"published_pr": "https://github.com/yoyodine-industries/yaan-hermes-config/pull/9999"},
    )
    caller, run_id = _caller(kb, conn)

    with pytest.raises(CrossCloseRefused) as excinfo:
        close_sibling(conn, sibling, caller_task_id=caller, caller_run_id=run_id)

    assert "landing" in str(excinfo.value).lower()
    assert kb.get_task(conn, sibling).status != "done"


# -------------------------------------------------------------------------------- children


def test_incomplete_children_are_re_promoted_and_a_gated_child_does_not_move(board):
    kb, conn = board
    from hermes_cli.kanban_cross_close import close_sibling

    sibling = _card(kb, conn, "parent about to be closed")
    child_todo = _card(kb, conn, "child in todo", parents=[sibling])
    child_blocked = _card(kb, conn, "child blocked on the parent")
    # Block while it is still `ready` (block_task only fires from running/ready), THEN link:
    # a ready -> todo demotion on link would leave the card unblockable.
    assert kb.block_task(conn, child_blocked, reason="needs the parent",
                         kind="capability") is True
    kb.link_tasks(conn, parent_id=sibling, child_id=child_blocked)
    assert kb.get_task(conn, child_blocked).status == "blocked"

    other_parent = _card(kb, conn, "an unfinished other parent")
    gated = _card(kb, conn, "gated by another parent", parents=[sibling, other_parent])
    caller, run_id = _caller(kb, conn)

    assert kb.get_task(conn, child_todo).status == "todo"
    receipt = close_sibling(conn, sibling, caller_task_id=caller, caller_run_id=run_id)

    by_id = {c["task_id"]: c for c in receipt["children"]}
    assert kb.get_task(conn, child_todo).status == "ready"
    assert by_id[child_todo]["to"] == "ready"
    assert by_id[child_todo]["action"] in ("promoted", "released")
    assert kb.get_task(conn, child_blocked).status == "ready", "an explicit block is released"
    assert by_id[child_blocked]["action"] in ("unblocked", "released")

    assert kb.get_task(conn, gated).status == "todo", "another unfinished parent holds it"
    assert by_id[gated]["action"] == "gated"
    assert other_parent in by_id[gated]["reason"]


def test_a_closed_siblings_children_are_in_the_audit_trail(board):
    kb, conn = board
    from hermes_cli.kanban_cross_close import close_sibling

    sibling = _card(kb, conn, "parent")
    child = _card(kb, conn, "child", parents=[sibling])
    caller, run_id = _caller(kb, conn)

    close_sibling(conn, sibling, caller_task_id=caller, caller_run_id=run_id)

    payload = _events(conn, sibling, "sibling_close_recorded")[0]
    assert payload["children"][0]["task_id"] == child
    assert payload["children"][0]["to"] == "ready"


# ------------------------------------------------------------------------------ importability


def test_the_module_has_no_import_cycle():
    """``kanban_tools`` imports it at module scope, so it must not import kanban_db then."""
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, hermes_cli.kanban_cross_close as m;"
         "assert callable(m.close_sibling);"
         "bad = [n for n in sys.modules if n in ('hermes_cli.kanban_db', 'tools.kanban_tools')];"
         "print('loaded:' + ','.join(bad))"],
        cwd=str(root), capture_output=True, text=True,
        env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "loaded:", proc.stdout
