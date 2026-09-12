"""Regression check: ephemeral scaffolding must never become a durable session row.

The kanban stop-guard (agent/turn_stop_gates.py) appends a synthetic assistant
candidate + a synthetic user nudge flagged `_kanban_stop_synthetic`.
`_flush_messages_to_session_db` skips those (agent/session_persistence.py), but the
in-place compaction commit (`SessionDB.archive_and_compact`) inserted the assembled
`compressed` list verbatim -> the scaffolding became durable, and each compaction
generation archived one copy while inserting a fresh ACTIVE one, so the same body
ended up as two simultaneously-active rows in one live view.

Run:
    pytest tests/run_agent/test_ephemeral_scaffolding_never_durable.py -q
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.kanban_stop import build_kanban_stop_nudge
from hermes_state import SessionDB

TASK_ID = "t_0000feed"
SESSION_ID = "ephemeral-scaffolding-fixture"


@pytest.fixture(autouse=True)
def _kanban_worker_env(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", TASK_ID)
    monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(tmp_path / "state.db")
    session_db.create_session(SESSION_ID, "kanban", model="deepseek-v4-flash")
    return session_db


def _count_content(db_path: Path, needle: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT id, role, length(content), active, compacted FROM messages "
            "WHERE content LIKE ? ORDER BY id",
            (f"%{needle}%",),
        ).fetchall()
    finally:
        conn.close()


def assert_single_active_row_per_body(db_path: Path) -> None:
    """The invariant the live view depends on: one ``active=1`` row per (session, role, body)."""
    conn = sqlite3.connect(db_path)
    try:
        dupes = conn.execute(
            "SELECT session_id, role, content, COUNT(*) FROM messages "
            "WHERE active = 1 AND compacted = 0 GROUP BY session_id, role, content "
            "HAVING COUNT(*) > 1"
        ).fetchall()
    finally:
        conn.close()
    assert not dupes, (
        "a body holds more than one active=1 row in one live view: "
        + "; ".join(f"{role} x{n}: {content[:80]!r}" for _, role, content, n in dupes)
    )


def _live_list() -> list[dict]:
    """The live message list exactly as the kanban stop gate leaves it."""
    nudge = build_kanban_stop_nudge(task_id=TASK_ID, messages=[])
    assert nudge, "guard must fire for a kanban worker that stopped without a terminal tool"
    return [
        {"role": "user", "content": f"work kanban task {TASK_ID}"},
        {
            "role": "assistant",
            "content": "I have finished the audit. Summary follows: ...",
            "finish_reason": "kanban_terminal_required",
            "_kanban_stop_synthetic": True,
        },
        {"role": "user", "content": nudge, "_kanban_stop_synthetic": True},
    ]


def test_in_place_compaction_commit_never_persists_scaffolding(db):
    """archive_and_compact is the writer that leaked them; it must filter them out."""
    db.archive_and_compact(SESSION_ID, _live_list(), watermark=None)

    assert _count_content(db.db_path, "You are a Hermes kanban worker") == []
    assert_single_active_row_per_body(db.db_path)


def test_framework_flush_still_skips_scaffolding(db):
    """The framework flush and the compaction commit must agree on the contract."""
    from unittest.mock import MagicMock

    from agent.session_persistence import SessionPersistenceMixin

    live = _live_list()
    stub = MagicMock()
    stub._session_db = db
    stub.session_id = SESSION_ID
    stub._session_messages = live
    stub._db_flush_scan_prefix = None
    stub._flushed_db_message_ids = set()
    stub._db_flush_scan_start = lambda *a, **k: 0
    SessionPersistenceMixin._flush_messages_to_session_db(stub, list(live), None)

    assert _count_content(db.db_path, "You are a Hermes kanban worker") == []


def test_every_ephemeral_flag_is_refused_by_the_durable_insert(db):
    """The DB boundary refuses ALL scaffolding flags, not just the kanban one."""
    from hermes_message_flags import EPHEMERAL_SCAFFOLDING_FLAGS

    for flag in EPHEMERAL_SCAFFOLDING_FLAGS:
        db.archive_and_compact(
            SESSION_ID,
            [{"role": "user", "content": f"scaffolding {flag}", flag: True}],
            watermark=None,
        )
        assert _count_content(db.db_path, f"scaffolding {flag}") == []
    assert_single_active_row_per_body(db.db_path)


def test_flags_have_one_definition():
    """A second copy of the tuple is how the kanban flag got missed; keep one source."""
    from hermes_message_flags import EPHEMERAL_SCAFFOLDING_FLAGS

    from agent import session_persistence, turn_final_response

    assert session_persistence._EPHEMERAL_SCAFFOLDING_FLAGS is EPHEMERAL_SCAFFOLDING_FLAGS
    assert turn_final_response._EPHEMERAL_SCAFFOLDING_FLAGS is EPHEMERAL_SCAFFOLDING_FLAGS
