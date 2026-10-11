"""``kanban_block`` can express a dependency wait from the worker tool surface.

The kernel already blocks completion while a parent is open (``_parents_satisfied``
— "Hard invariant even for human review approval"), so a worker whose card is
gated by an OPEN parent needs a way to RECORD that wait. The tool used to refuse
a prose-named dependency block with "Re-run with ``waits_on=[...]``" and then
reject ``waits_on`` as an unknown parameter — the remedy named a fix the surface
itself refused.

Two legs are proven here:
  * ``waits_on`` is threaded through the tool (schema + handler) and creates the edge;
  * a card ALREADY linked to its open parent parks with no ``waits_on`` at all —
    the edge it has IS the wait the board's machinery reads.
"""
import json

import pytest


@pytest.fixture
def worker(monkeypatch, tmp_path):
    """A temp HERMES_HOME with a parent and a CLAIMED (running) child the tool owns."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    for var in ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_ID",
                "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        parent_id = kb.create_task(conn, title="parent gate", assignee="test-worker")
        child_id = kb.create_task(conn, title="child work", assignee="test-worker")
        assert kb.claim_task(conn, child_id) is not None
        run_id = kb._current_run_id(conn, child_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", child_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return parent_id, child_id, run_id


def _edges(child_id):
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return [r["parent_id"] for r in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (child_id,))]
    finally:
        conn.close()


def _status(child_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return kb.get_task(conn, child_id).status
    finally:
        conn.close()


def _block_kind(child_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return kb.get_task(conn, child_id).block_kind
    finally:
        conn.close()


def test_block_schema_declares_waits_on():
    """The unknown-parameter gate reads the registered schema, so waits_on lives there."""
    from tools import kanban_tools  # noqa: F401 — import registers the kanban tools
    from tools.registry import registry
    props = registry.get_schema("kanban_block")["parameters"]["properties"]
    assert "waits_on" in props
    assert props["waits_on"]["type"] == "array"
    assert props["waits_on"]["items"] == {"type": "string"}


def test_dependency_block_on_an_already_linked_parent_parks_without_waits_on(worker):
    """The measured gap: an open linked parent is the edge — no waits_on is demanded."""
    parent_id, child_id, run_id = worker
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    conn = kbc.connect()
    try:
        # The worker links its OWN active run first (the documented link-first flow).
        assert kb.link_tasks(conn, parent_id, child_id,
                             expected_child_run_id=run_id) is False
    finally:
        conn.close()
    out = json.loads(kt._handle_block(
        {"task_id": child_id, "kind": "dependency",
         "reason": f"waiting on {parent_id} to land"}))
    assert out.get("ok"), out
    assert out["status"] == "todo"          # a dependency wait, not a human block
    assert out["block_kind"] == "dependency"
    assert _edges(child_id) == [parent_id]


def test_waits_on_passes_through_the_tool_and_creates_the_edge(worker):
    """waits_on is accepted (not 'unknown parameter') and lands as a parent edge."""
    parent_id, child_id, run_id = worker
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_block(
        {"task_id": child_id, "kind": "dependency",
         "reason": f"waiting on {parent_id} to land", "waits_on": [parent_id]}))
    assert out.get("ok"), out
    assert out["status"] == "todo"
    assert out["block_kind"] == "dependency"
    assert _edges(child_id) == [parent_id]


def test_prose_only_dependency_block_is_still_refused(worker):
    """Negative control (i): no edge, no waits_on, a named reason → refused, nothing parked."""
    parent_id, child_id, run_id = worker
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_block(
        {"task_id": child_id, "kind": "dependency",
         "reason": f"waiting on {parent_id} to land"}))
    assert out.get("error"), out
    assert parent_id in out["error"]
    assert "waits_on" in out["error"]
    assert _status(child_id) == "running"
    assert _edges(child_id) == []


def test_waits_on_does_not_license_an_undeclared_prose_ref(worker):
    """Negative control (ii): a declared edge for X cannot hide an unlinked Y in the reason."""
    parent_id, child_id, run_id = worker
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        other = kb.create_task(conn, title="other", assignee="test-worker")
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_block(
        {"task_id": child_id, "kind": "dependency",
         "reason": f"waits on {parent_id} and {other}",
         "waits_on": [parent_id]}))
    assert out.get("error"), out
    assert other in out["error"]
    assert _status(child_id) == "running"
