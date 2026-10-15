"""Message-level retention: the declared window applied to archived ROWS of declared sources.

Session retention keys on ``ended_at IS NOT NULL`` *and* idleness, so a session that is written
to forever (``api_server``'s canonical Bot Chat: 176,834 rows, 62% of the store over 43 days,
only 50 of them ``active``) can never be reached by it — its compaction archive is unbounded.
:meth:`SessionMaintenanceMixin.prune_archived_messages` deletes only rows no reader can see:
``active = 0`` (every model-facing read applies ``AND active = 1``), ``compacted = 1``,
not a compaction summary, inside the declared window, and owned by a session of a declared
source.  These tests pin every term of that predicate, the replay-set invariance (the whole
point of the ``active = 0`` term), and the maintenance wiring that runs the step.
"""

import hashlib
import json
import time

import pytest

from hermes_state import SessionDB

DAY = 86400.0


@pytest.fixture
def db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


def _archive(db: SessionDB, message_id: int, *, summary: bool = False, rewind: bool = False) -> None:
    """Mark one stored row the way the store really marks it.

    ``summary`` = a compaction summary marker (display history, kept); ``rewind`` = an Undo/Rewind
    row (``active = 0, compacted = 0``, deliberately OUT of this rule's scope).
    """
    db._conn.execute(
        "UPDATE messages SET active = 0, compacted = ?, _compressed_summary = ? WHERE id = ?",
        (0 if rewind else 1, 1 if summary else 0, message_id))
    db._conn.commit()


def _ids(db: SessionDB, session_id: str) -> set:
    return {row[0] for row in
            db._conn.execute("SELECT id FROM messages WHERE session_id = ?", (session_id,))}


def _fts_match(db: SessionDB, table: str, term: str) -> int:
    return int(db._conn.execute(
        f"SELECT count(*) FROM {table} WHERE {table} MATCH ?", (term,)).fetchone()[0])


def _replay_set(db: SessionDB, session_id: str) -> str:
    """The model-facing replay for this session, hashed — the artifact the rule must not change."""
    rows = db.get_messages_as_conversation(session_id)
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _seed(db: SessionDB, session_id: str, source: str, *, old: float = 6 * DAY, tag: str = "row") -> dict:
    """One session carrying every row shape the predicate distinguishes; returns their ids.

    *tag* prefixes each row's content with a token that is unique to this session, so an FTS5
    probe can tell which session's rows survived the delete without a hyphenated query term.
    """
    now = time.time()
    db.create_session(session_id, source=source)
    live_id = db.append_message(session_id, role="user", content=f"{tag}liveword",
                                timestamp=now - DAY)
    archived = db.append_message(session_id, role="assistant", content=f"{tag}archword",
                                 timestamp=now - old)
    inside = db.append_message(session_id, role="assistant", content=f"{tag}insideword",
                               timestamp=now - 60)
    summary = db.append_message(session_id, role="assistant", content=f"{tag}summaryword",
                                timestamp=now - old, _compressed_summary=True)
    rewind = db.append_message(session_id, role="assistant", content=f"{tag}rewindword",
                               timestamp=now - old)
    _archive(db, archived)
    _archive(db, inside)
    _archive(db, summary, summary=True)
    _archive(db, rewind, rewind=True)
    return {"live": live_id, "archived": archived, "inside": inside,
            "summary": summary, "rewind": rewind}


def test_predicate_deletes_only_archived_in_window_rows_of_declared_sources(db):
    api = _seed(db, "api", "api_server", tag="apix")
    cli = _seed(db, "cli_sess", "cli", tag="clix")  # another source: same shapes, untouched

    deleted = db.prune_archived_messages(older_than_days=5)

    # Only the api_server session's out-of-window compaction archive goes.
    assert deleted == 1
    assert api["archived"] not in _ids(db, "api")
    # active = 1 rows are never touched — this term IS the safety invariant.
    assert api["live"] in _ids(db, "api")
    # Inside the declared window: kept (retention is a window, not a purge).
    assert api["inside"] in _ids(db, "api")
    # A compaction SUMMARY is display history, and a rewind row (active=0, compacted=0) is
    # deliberately out of scope: widening to rewind rows is deferred, not silently taken.
    assert api["summary"] in _ids(db, "api")
    assert api["rewind"] in _ids(db, "api")
    # Source scoping: an undeclared source with the identical shapes is entirely untouched.
    assert _ids(db, "cli_sess") == set(cli.values())
    # Both FTS5 indexes drop the deleted row (the delete triggers own them).
    assert _fts_match(db, "messages_fts", "apixarchword") == 0
    assert _fts_match(db, "messages_fts_trigram", "apixarchword") == 0
    assert _fts_match(db, "messages_fts", "clixarchword") == 1
    # Idempotent: a re-run finds nothing left to delete.
    assert db.prune_archived_messages(older_than_days=5) == 0


def test_replay_set_is_byte_identical_across_the_delete(db):
    seeded = _seed(db, "api", "api_server")
    before = _replay_set(db, "api")

    deleted = db.prune_archived_messages(older_than_days=5)

    assert deleted == 1
    # The invariant, executed: the model's replay of this session cannot change, because every
    # model-facing read applies ``AND active = 1`` and every deleted row is ``active = 0``.
    assert _replay_set(db, "api") == before
    assert seeded["live"] in _ids(db, "api")


def test_fail_closed_on_empty_sources_and_negative_window(db):
    api = _seed(db, "api", "api_server")

    # An empty source tuple is fail-closed: the rule reaches nothing at all.
    assert db.prune_archived_messages(older_than_days=5, sources=()) == 0
    # A negative window would build a FUTURE cutoff and match the whole archive; refused.
    assert db.prune_archived_messages(older_than_days=-1) == 0
    assert db.prune_archived_messages(older_than_days=None) == 0
    assert api["archived"] in _ids(db, "api")
    # The window is the only difference: at 0 days both archived rows are out of window while
    # the live row still is not (``active = 0`` holds at every window).
    assert db.prune_archived_messages(older_than_days=0) == 2
    assert api["live"] in _ids(db, "api")


def test_renews_the_watchdog_lease_and_commits_once_per_chunk(db, monkeypatch):
    _seed(db, "api", "api_server")
    db.append_message("api", role="assistant", content="second archived",
                      timestamp=time.time() - 6 * DAY)
    _archive(db, max(_ids(db, "api")))

    renewals: list = []
    monkeypatch.setattr("hermes_state_maintenance.report_startup_progress",
                        lambda *a, **kw: renewals.append(kw.get("phase")))

    deleted = db.prune_archived_messages(older_than_days=5, chunk_rows=1)

    assert deleted == 2
    # One renewal per chunk + the one that discovers the store is drained = 3 transactions,
    # i.e. the write lock is released between chunks instead of held for the whole delete.
    assert renewals == ["state_db_auto_prune_archive"] * 3


def test_maintenance_runs_the_retention_step_and_records_its_count(db, tmp_path, monkeypatch):
    _seed(db, "api", "api_server")
    monkeypatch.setattr(db, "vacuum", lambda *a, **kw: None)

    result = db.maybe_auto_prune_and_vacuum(
        retention_days=5, min_interval_hours=0, vacuum=True, min_vacuum_interval_days=0)

    assert result["pruned_archived_messages"] == 1
    record = json.loads(db.get_meta("last_state_db_maintenance"))
    assert record["pruned_archived_messages"] == 1
    assert record["retention_days"] == 5
    lines = [json.loads(line) for line in
             (tmp_path / "logs" / "state-db-maintenance.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert lines[-1]["pruned_archived_messages"] == 1


def test_retention_rows_alone_admit_the_vacuum(db, monkeypatch):
    """Without the new count in the admission, rows freed by this step are never vacuumed."""
    _seed(db, "api", "api_server")
    db.set_meta("last_vacuum", str(time.time()))  # due, not throttled
    calls: list = []
    monkeypatch.setattr(db, "vacuum", lambda *a, **kw: calls.append(1))
    monkeypatch.setattr(db, "prune_sessions", lambda **kw: 0)  # no session pruned
    monkeypatch.setattr(db, "reap_orphan_messages", lambda: 0)  # no orphan reaped
    monkeypatch.setattr(db, "_freelist_ratio", lambda: 1.0)
    monkeypatch.setattr(db, "optimize_fts", lambda: 0)
    # No other process may hold the store, so the reclaim admission is decided by the counts.
    monkeypatch.setattr("hermes_state_holders.foreign_state_db_holders", lambda path: [])
    monkeypatch.setattr("hermes_state_holders.in_process_state_db_holders",
                        lambda path, exclude=None: [])

    result = db.maybe_auto_prune_and_vacuum(
        retention_days=5, min_interval_hours=0, vacuum=True, min_vacuum_interval_days=0)

    assert result["pruned_archived_messages"] == 1
    assert calls == [1] and result["vacuumed"] is True
