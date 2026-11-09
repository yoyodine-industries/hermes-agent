"""Retention must reach rows the runtime auto-archived.

The rule (state-db retention ruling, M1.6): the unit is a session aged by
``last_activity_at``; the exemptions are INTENT (pinned, human-archived) and
LIVENESS (never the live session). Auto-archived and hidden rows become
prunable. Before this, ``hermes sessions prune`` passed ``archived=False``, so
every row the runtime had hidden was invisible to retention forever and
``prune`` deleted 0 rows past the window while the store kept growing.
"""

import time

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def _old_ended(db: SessionDB, session_id: str, *, archived: int, auto_archived: int) -> None:
    old = time.time() - 30 * 86400
    db.create_session(session_id, source="cli")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?, message_count = 1, archived = ?, "
        "auto_archived = ? WHERE id = ?",
        (old, old + 60, archived, auto_archived, session_id),
    )
    db._conn.commit()


def _ids(rows):
    return sorted(row["id"] for row in rows)


def test_prune_selection_reaches_an_auto_archived_row(db):
    _old_ended(db, "auto", archived=1, auto_archived=1)
    _old_ended(db, "plain", archived=0, auto_archived=0)

    rows = db.list_prune_candidates(older_than_days=7, archived=False, auto_archived_prunable=True)

    assert _ids(rows) == ["auto", "plain"]


def test_prune_selection_spares_a_human_archived_row(db):
    """A deliberate archive is user intent — retention never deletes it."""
    _old_ended(db, "human", archived=1, auto_archived=0)
    _old_ended(db, "auto", archived=1, auto_archived=1)

    rows = db.list_prune_candidates(older_than_days=7, archived=False, auto_archived_prunable=True)

    assert _ids(rows) == ["auto"]


def test_prune_selection_is_unchanged_without_the_new_flag(db):
    """Without the flag the old selector still holds: archived rows are not candidates."""
    _old_ended(db, "auto", archived=1, auto_archived=1)

    assert db.list_prune_candidates(older_than_days=7, archived=False) == []


def test_prune_deletes_the_auto_archived_row_and_keeps_the_human_archive(db):
    _old_ended(db, "auto", archived=1, auto_archived=1)
    _old_ended(db, "human", archived=1, auto_archived=0)

    deleted = db.prune_sessions(older_than_days=7, archived=False, auto_archived_prunable=True)

    assert deleted == 1
    assert db.get_session("auto") is None
    assert db.get_session("human") is not None
