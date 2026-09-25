"""The worker-facing surface of the close-path deliverable gate.

``complete_task`` refuses a handoff whose declared deliverable nothing outside the
producing tree can retrieve (the ``t_b579f394`` shape: a scratch-tree commit and
artifacts under a profile scratch dir). The worker's own tool call
(``tools.kanban_tools._handle_complete``) must say the card was NOT mutated and
what to fix — otherwise the model reads the tool error as terminal and strands the
run instead of pushing the commit and retrying (#22923).
"""
import json
from pathlib import Path

import pytest


@pytest.fixture
def running_card_with_scratch_handoff(monkeypatch, tmp_path):
    """A claimed (running) card whose handoff cites a commit no checkout holds."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_completion_gate as gate
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    # The gate searches the fleet's checkouts by default; a test owns its search set.
    monkeypatch.setattr(gate, "_FLEET_REPO_ROOTS", ())
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="train handoff", assignee="test-worker")
        assert kb.claim_task(conn, tid) is not None
        run_id = kb._current_run_id(conn, tid)
        scratch = home / "profiles" / "test-worker" / "cache" / "scratch" / tid
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "dag-diff.txt").write_text("diff", encoding="utf-8")
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid, scratch / "dag-diff.txt"


def test_complete_reports_unretrievable_deliverable_and_stays_retryable(
    running_card_with_scratch_handoff,
):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    tid, artifact = running_card_with_scratch_handoff
    out = json.loads(kt._handle_complete({
        "task_id": tid,
        "summary": "done: committed on card/train-handoff @ 018511a",
        "metadata": {"commit": "018511a", "artifacts": [str(artifact)]},
    }))

    assert out.get("error")
    assert "018511a" in out["error"], "the refusal must name the unresolvable revision"
    assert str(artifact) in out["error"], "and the artifact nothing outside the tree holds"
    assert "still in-flight" in out["error"], "the worker must know the card was not mutated"
    assert "retry" in out["error"].lower()

    conn = kbc.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
        kinds = [event.kind for event in kb.list_events(conn, tid)]
    finally:
        conn.close()
    assert "completion_blocked_unretrievable_deliverable" in kinds
