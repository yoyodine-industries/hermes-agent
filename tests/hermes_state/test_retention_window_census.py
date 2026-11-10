"""The retention census must BE the runtime's selection, not a second opinion on it
(card t_0ee10a96, rework round 2).

The dashboard's ``hermes_sessions`` class used to re-type the prune predicate in its own SQL.
On 2026-11-09 that mirror reported 260 sessions / 56,914 messages eligible on the live fleet
while the runtime's own ``_prune_where`` selected **0** in every store: all 260 were
``end_reason='startup_orphan_reap'`` rows whose ``ended_at`` sat inside the window — the
runtime's deliberate carve-out (a reaped orphan ages from the sweep, not from its old
activity, or the next prune deletes it before recovery). A carrier that plans a number the
runtime will not act on turns a green run into a false red, one deploy after it was a false
green.

So the runtime answers, in one call, both halves of the class record: what the prune SELECTS
(``selected_*``) and what the window still HOLDS (``past_window_*``). These tests drive the
real :class:`SessionDB` and the real predicate.
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


def _mk(db, sid, *, activity_days=100.0, ended_days=None, messages=2, archived=0,
        auto_archived=0, pinned=False, end_reason=None, message_age_days=None):
    """A session whose ACTIVITY and whose MESSAGE clocks are set independently.

    ``activity_days`` moves ``last_activity_at``/``started_at``; ``message_age_days`` (when
    given) moves the messages somewhere else, so a fixture can separate "old transcript" from
    "recently used" — the distinction the activity basis exists to make.
    """
    db.create_session(sid, source="cli")
    for index in range(messages):
        db.append_message(sid, "user", f"payload {index}")
    if pinned:
        db.set_session_pinned(sid, True)
    activity = time.time() - activity_days * 86400.0
    ended = (None if ended_days is None
             else time.time() - ended_days * 86400.0)
    message_ts = (activity if message_age_days is None
                  else time.time() - message_age_days * 86400.0)
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET started_at=?, ended_at=?, end_reason=?, last_activity_at=?,"
            " archived=?, auto_archived=? WHERE id=?",
            (activity, ended, end_reason, activity, archived, auto_archived, sid),
        )
        db._conn.execute("UPDATE messages SET timestamp=? WHERE session_id=?", (message_ts, sid))
        db._conn.commit()
    return sid


def _ids(census):
    return set(census["selected_ids"])


def test_orphan_reaped_rows_are_not_selected_even_when_aged(db):
    """The runtime's carve-out, measured: a reaped orphan ages from the sweep, not its activity.

    This is the exact population that made the dashboard's mirror wrong (260 sessions fleet
    wide): old activity, ``ended_at`` inside the window, ``end_reason='startup_orphan_reap'``.
    """
    orphan = _mk(db, "20260101_000000_orphan", activity_days=30, ended_days=0.5,
                 end_reason="startup_orphan_reap")

    census = db.retention_window_census(
        older_than_days=7, archived=False, include_auto_archived=True)

    assert _ids(census) == set()
    assert census["selected_sessions"] == 0
    assert census["selected_messages"] == 0
    # ...but it IS past the window: it is KEPT by rule, not absent. A record that could not
    # tell those two facts apart is what let the class report "ok" over 186,872 rows.
    assert census["past_window_sessions"] == 1
    assert census["past_window_messages"] == 2
    assert db.get_session(orphan) is not None


def test_the_same_row_with_an_old_ended_at_is_selected_and_pruned(db):
    """Past the carve-out's own edge (``ended_at`` older than the window) the row is a candidate."""
    orphan = _mk(db, "20260101_000010_orphan2", activity_days=30, ended_days=8,
                 end_reason="startup_orphan_reap")

    census = db.retention_window_census(
        older_than_days=7, archived=False, include_auto_archived=True)

    assert _ids(census) == {orphan}
    assert census["selected_sessions"] == 1
    assert census["selected_messages"] == 2
    removed = db.prune_sessions(older_than_days=7, archived=False, include_auto_archived=True)
    assert removed == census["selected_sessions"]
    assert db.get_session(orphan) is None


def test_a_recently_used_session_with_an_old_transcript_is_not_eligible(db):
    """The activity basis is the freshest of ``last_activity_at`` / latest message / started.

    A session resumed yesterday whose transcript starts five weeks back is NOT old: reading
    ``MAX(messages.timestamp)`` alone (the mirror's bug) would delete a live conversation.
    """
    resumed = _mk(db, "20260101_000020_resumed", activity_days=0.5, ended_days=0.5,
                  messages=3, message_age_days=35)

    census = db.retention_window_census(
        older_than_days=7, archived=False, include_auto_archived=True)

    assert _ids(census) == set()
    assert census["past_window_sessions"] == 0
    assert census["past_window_messages"] == 0
    assert db.prune_sessions(older_than_days=7, archived=False, include_auto_archived=True) == 0
    assert db.get_session(resumed) is not None


def test_census_keeps_intent_and_liveness_and_reports_them_as_kept(db):
    """Pins and HUMAN archives are kept by rule; runtime bookkeeping is not."""
    # Ended past the window, so each is a real candidate before the eligibility rule runs —
    # an OPEN session is never a prune subject at all, and a fixture that confused the two
    # would prove nothing about intent or liveness.
    kept_manual = _mk(db, "20260101_000030_manual", ended_days=9, archived=1, auto_archived=0)
    kept_pinned = _mk(db, "20260101_000031_pinned", ended_days=9, pinned=True)
    reachable = _mk(db, "20260101_000032_auto", ended_days=9, archived=1, auto_archived=1)

    census = db.retention_window_census(
        older_than_days=7, archived=False, include_auto_archived=True)

    assert _ids(census) == {reachable}
    assert census["past_window_sessions"] == 3
    assert census["past_window_messages"] == 6
    # The KEPT half of the record is what a per-lane cap must not be mischarged against.
    assert census["past_window_sessions"] - census["selected_sessions"] == 2


def test_census_matches_the_prune_it_describes_on_a_mixed_store(db):
    """Identity, not a spot value: census count == rows the prune actually removes."""
    _mk(db, "20260101_000040_a", activity_days=40, ended_days=40)
    _mk(db, "20260101_000041_b", activity_days=9, ended_days=9, archived=1, auto_archived=1)
    _mk(db, "20260101_000042_c", activity_days=12, ended_days=12, pinned=True)
    _mk(db, "20260101_000043_d", activity_days=30, ended_days=0.2,
        end_reason="startup_orphan_reap")
    _mk(db, "20260101_000044_e", activity_days=1, ended_days=1)

    census = db.retention_window_census(
        older_than_days=7, archived=False, include_auto_archived=True)
    expected = _ids(census)
    removed = db.prune_sessions(older_than_days=7, archived=False, include_auto_archived=True)

    assert len(expected) == 2
    assert removed == census["selected_sessions"] == 2
    for sid in expected:
        assert db.get_session(sid) is None
    assert db.get_session("20260101_000042_c") is not None   # pin survives
    assert db.get_session("20260101_000043_d") is not None   # orphan fence survives
