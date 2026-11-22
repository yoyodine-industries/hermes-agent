"""The lane that OWNS a parked card can TYPE it: kanban_block reaches the kernel's
classify-in-place branch (card t_84f64ea9).

``kanban_db.block_task``'s classify-in-place branch types a ``blocked`` row whose
``block_kind`` is NULL and whose run is over -- its one condition is
``expected_run_id is None``. ``tools/kanban_tools._handle_block`` asserted this
worker's own run id unconditionally, so the owning lane was refused verbatim
"the live row is status=blocked; no run owns it" and ``block_kind IS NULL`` was a
one-way door: the unblocker ladder then had no vocabulary for the park (it went
``unclassifiable`` -> resolver card).

The tests below pin the door open where no run can own the row, and pin it SHUT
everywhere else (a kind-less call, a live claim, an already-typed park).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def parked_worker_env(monkeypatch, tmp_path):
    """A dispatched worker whose OWN card the failure breaker parked untyped.

    Reproduces ``kanban_db_dispatch._record_task_failure``'s trip: status
    ``blocked``, ``block_kind`` NULL, run closed (``current_run_id`` NULL), while
    the worker session still holds the run id it was spawned with.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="breaker-parked", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'blocked', current_run_id = NULL, "
                "block_kind = NULL WHERE id = ?", (tid,))
        parked = kb.get_task(conn, tid)
        assert (parked.status, parked.block_kind, parked.current_run_id) == ("blocked", None, None)
        assert run_id is not None
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def _board_row(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return kb.get_task(conn, tid), conn
    except Exception:  # pragma: no cover - connection must be closed by caller
        conn.close()
        raise


def _as_dict(out):
    """Handlers return either a JSON string (tool_error) or a dict (_ok_landed)."""
    return out if isinstance(out, dict) else json.loads(out)


def test_owning_lane_types_a_parked_card_in_place(parked_worker_env):
    """The replay: a card parked with no kind, touched by the lane that owns it,
    is TYPED in place -- no status flap, no synthetic run, one audit event."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = parked_worker_env
    out = kt._handle_block({"reason": "standing notice: no worker action owed",
                            "kind": "needs_input"})
    d = _as_dict(out)
    assert "error" not in d, d
    assert d["block_kind"] == "needs_input"

    conn = kbc.connect()
    try:
        after = kb.get_task(conn, tid)
        assert (after.status, after.block_kind) == ("blocked", "needs_input")
        assert after.current_run_id is None
        events = kb.list_events(conn, tid)
        classified = [e for e in events if e.kind == "blocked"]
        payload = classified[-1].payload
        payload = payload if isinstance(payload, dict) else json.loads(payload)
        assert payload.get("classified_in_place") is True
        assert len(kb.list_runs(conn, tid)) == 1          # the run that crashed; none minted
    finally:
        conn.close()


def test_kindless_call_on_a_parked_card_is_still_refused(parked_worker_env):
    """Without the kind there is nothing to classify with: refuse, write nothing."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = parked_worker_env
    d = json.loads(kt._handle_block({"reason": "just parking it, no kind"}))
    assert "error" in d, d
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, tid).block_kind is None
    finally:
        conn.close()


def test_cannot_type_another_workers_live_park(parked_worker_env, monkeypatch):
    """A ``blocked`` row that STILL carries a current run is not classifiable by a
    stale worker: the run assertion is kept and the kernel refuses."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = parked_worker_env
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET current_run_id = 424242 WHERE id = ?", (tid,))
    finally:
        conn.close()

    d = json.loads(kt._handle_block({"reason": "mine to type", "kind": "needs_input"}))
    assert "error" in d, d
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, tid).block_kind is None
    finally:
        conn.close()


def test_a_typed_park_is_never_retyped(parked_worker_env):
    """The in-place branch is a door for an UNTYPED park only -- a typed one is
    refused, so nothing can flap the kind under a live resolver."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = parked_worker_env
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET block_kind = 'capability' WHERE id = ?", (tid,))
    finally:
        conn.close()

    d = json.loads(kt._handle_block({"reason": "retype it", "kind": "needs_input"}))
    assert "error" in d, d
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, tid).block_kind == "capability"
    finally:
        conn.close()
