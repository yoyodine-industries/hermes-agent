"""Retention must reach the sessions the RUNTIME archived (card t_0ee10a96, L1).

``hermes sessions prune`` built its selection with ``s.archived = 0``, so every session the
idle sweep had already archived (``archived=1``, ``auto_archived=1``) was invisible to it
forever. On a store whose ``sessions.auto_archive_days`` is shorter than its retention window
that made the selection a STRUCTURAL ZERO: prune deleted 0 rows past the window every run
while the store grew (249 sessions / 136,588 rows measured on platform-stl).

The boundary the fix draws: MANUAL archive is operator intent and stays exempt; runtime
BOOKKEEPING (``auto_archived=1``) becomes eligible. Pinning and liveness keep their rows, and
``list`` semantics do not move — only the delete path gains the arm.

These drive the real SessionDB (temp file DB).
"""
import time

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    yield d
    try:
        d.close()
    except Exception:
        pass


def _mk(db, sid, *, age_days=100.0, archived=0, auto_archived=0, pinned=False,
        messages=2, ended=True):
    """An aged session whose SESSION and MESSAGE clocks are both past the window."""
    db.create_session(sid, source="cli")
    for index in range(messages):
        db.append_message(sid, "user", f"payload {index}")
    if pinned:
        db.set_session_pinned(sid, True)
    ts = time.time() - age_days * 86400.0
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET started_at=?, ended_at=?, last_activity_at=?, archived=?,"
            " auto_archived=? WHERE id=?",
            (ts, ts if ended else None, ts, archived, auto_archived, sid),
        )
        # The activity predicate takes the freshest of last_activity_at / latest message /
        # started_at, so the messages have to age with the row or every fixture looks live.
        db._conn.execute("UPDATE messages SET timestamp=? WHERE session_id=?", (ts, sid))
        db._conn.commit()
    return sid


def _message_ids(db, sid):
    with db._lock:
        return {row[0] for row in db._conn.execute(
            "SELECT id FROM messages WHERE session_id=?", (sid,)).fetchall()}


def test_prune_reaches_auto_archived_and_spares_intent_and_liveness(db):
    auto = _mk(db, "20260101_000000_auto01", archived=1, auto_archived=1)
    manual = _mk(db, "20260101_000001_manual", archived=1, auto_archived=0)
    pinned = _mk(db, "20260101_000002_pinned", pinned=True)
    live = _mk(db, "20260101_000003_live01", age_days=0.0, ended=False)

    removed = db.prune_sessions(older_than_days=7, archived=False, include_auto_archived=True)

    assert removed == 1
    # The runtime-archived session and its transcript are gone.
    assert db.get_session(auto) is None
    assert _message_ids(db, auto) == set()
    # Operator intent survives, WITH its messages: prune is session-granular.
    assert db.get_session(manual) is not None
    assert len(_message_ids(db, manual)) == 2
    # A pin is a durable keep flag, and a live session is never a candidate.
    assert db.get_session(pinned) is not None
    assert db.get_session(live) is not None
    assert len(_message_ids(db, live)) == 2


def test_auto_archive_is_invisible_without_the_prune_arm(db):
    """The arm is what makes the population reachable — not a wider age window."""
    auto = _mk(db, "20260101_000010_auto02", archived=1, auto_archived=1)

    assert db.prune_sessions(older_than_days=7, archived=False) == 0
    assert db.get_session(auto) is not None


def test_include_archived_still_means_both_arms(db):
    """``--include-archived`` keeps its meaning: it is the human's explicit opt-in."""
    manual = _mk(db, "20260101_000020_manual2", archived=1, auto_archived=0)

    assert db.prune_sessions(older_than_days=7, archived=None) == 1
    assert db.get_session(manual) is None


def test_unarchived_aged_session_is_still_pruned(db):
    """Negative control: the existing arm must not regress."""
    plain = _mk(db, "20260101_000030_plain1", archived=0, auto_archived=0)

    assert db.prune_sessions(older_than_days=7, archived=False, include_auto_archived=True) == 1
    assert db.get_session(plain) is None


def test_list_semantics_are_unchanged_by_the_prune_arm(db):
    """What a human sees in ``hermes sessions list`` must not gain the archived rows."""
    auto = _mk(db, "20260101_000040_auto03", archived=1, auto_archived=1)
    plain = _mk(db, "20260101_000041_plain2", archived=0, auto_archived=0)

    visible = {row["id"] for row in db.list_sessions_rich(limit=50, offset=0, include_archived=False)}
    assert plain in visible
    assert auto not in visible
