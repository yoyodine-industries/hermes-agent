"""Bounded incremental reclaim replaces the full-VACUUM rewrite (card t_f8db44f6).

The defect: automatic maintenance reclaimed space with an EXCLUSIVE whole-file `VACUUM`, so the
pass that finally ran held the single write lock for the entire multi-GB rewrite. The shape
now: migrate a store ONCE to ``PRAGMA auto_vacuum = INCREMENTAL``, then return space in bounded
``PRAGMA incremental_vacuum`` chunks — each its own transaction, each holding the write lock for
tens of milliseconds — and never rewrite the file whole again.

Every test builds its own scratch store under ``tmp_path``; no live store is touched.
"""

from __future__ import annotations

import inspect
import time

from hermes_state import SessionDB

_ROWS = 600
_BLOB = "z" * 4000


def _grow(db, rows: int = _ROWS) -> None:
    """Add a bulky session and prune it back out, leaving its pages on the freelist."""
    db.create_session("grow", "cli")
    for _ in range(rows):
        db.append_message("grow", "assistant", _BLOB)
    db.end_session("grow", "done")
    db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.prune_sessions(older_than_days=0)
    db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _migrated(tmp_path) -> SessionDB:
    """A scratch store already converted to auto_vacuum=INCREMENTAL."""
    db = SessionDB(db_path=tmp_path / "state.db")
    _grow(db)
    rec = db.reclaim()
    assert rec["migrated"] is True, rec
    assert db._auto_vacuum_mode() == 2, db._auto_vacuum_mode()
    return db


# ── the migration is exactly once ────────────────────────────────────────────

def test_vacuum_is_issued_once_for_the_migration_and_never_again(tmp_path, monkeypatch):
    """The FIRST reclaim converts the file with one VACUUM; every later pass issues none."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        _grow(db)
        calls = []
        real = db.vacuum
        monkeypatch.setattr(db, "vacuum", lambda **kw: (calls.append(kw), real(**kw))[1])

        first = db.reclaim()
        assert first["migrated"] is True, first
        assert first["ran"] is True
        assert first["mode"] == 0 and first["mode_after"] == 2, first
        assert calls == [{"truncate_checkpoint": False}], calls

        _grow(db)
        second = db.reclaim()
        assert second["migrated"] is False, second
        assert second["mode"] == 2, second
        assert len(calls) == 1, f"a second whole-file rewrite was issued: {calls!r}"
        assert calls[0]["truncate_checkpoint"] is False, "automatic path must not TRUNCATE the WAL"
    finally:
        db.close()


def test_store_already_incremental_never_gets_a_vacuum(tmp_path, monkeypatch):
    db = _migrated(tmp_path)
    try:
        calls = []
        monkeypatch.setattr(db, "vacuum", lambda **kw: calls.append(kw))
        _grow(db)
        rec = db.reclaim()
        assert rec["ran"] is True, rec
        assert calls == [], f"an INCREMENTAL store must never be rewritten: {calls!r}"
    finally:
        db.close()


# ── the bounded pass ─────────────────────────────────────────────────────────

def test_chunked_pass_frees_exactly_min_of_freelist_and_pass_pages(tmp_path):
    db = _migrated(tmp_path)
    try:
        _grow(db)
        _ps, freelist = db._freelist_pages()
        assert freelist > 20, freelist

        budget = 10
        rec = db.incremental_vacuum(chunk_pages=4, max_pages=budget, max_seconds=60.0)
        assert rec["ran"] is True
        assert rec["pages"] == min(rec["freelist_before"], budget), (rec, freelist)
        assert rec["pages"] == budget, rec
        assert rec["stopped"] == "pages", rec
        assert rec["max_lock_hold_ms"] > 0.0

        # An unbounded-enough pass drains the rest of the freelist, and no further.
        rec2 = db.incremental_vacuum(chunk_pages=8, max_pages=10 ** 6, max_seconds=120.0)
        assert rec2["stopped"] == "drained", rec2
        assert rec2["freelist_after"] == 0, rec2
        assert rec2["bytes"] == rec2["pages"] * rec2["page_size"], rec2
    finally:
        db.close()


def test_pass_stops_at_the_seconds_budget(tmp_path):
    """A zero-second budget issues no chunk at all and says why."""
    db = _migrated(tmp_path)
    try:
        _grow(db)
        rec = db.incremental_vacuum(chunk_pages=4, max_pages=10 ** 6, max_seconds=0.0)
        assert rec["ran"] is True
        assert rec["stopped"] == "seconds", rec
        assert rec["pages"] == 0, rec
    finally:
        db.close()


def test_incremental_vacuum_is_a_noop_on_a_none_store(tmp_path):
    """Incremental reclaim on an un-migrated store would report a drain that never happened."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        _grow(db)
        rec = db.incremental_vacuum(max_pages=10 ** 6, max_seconds=30.0)
        assert rec["ran"] is False
        assert rec["stopped"] == "not-incremental", rec
        assert rec["pages"] == 0
        assert db._auto_vacuum_mode() == 0
    finally:
        db.close()


def test_migration_is_admitted_by_the_freelist_ratio(tmp_path):
    """Below the ratio a rewrite costs more than it returns; the store is left alone."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("tiny", "cli")
        db.append_message("tiny", "assistant", "z" * 200)
        db.end_session("tiny", "done")
        db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.prune_sessions(older_than_days=0)
        rec = db.reclaim()
        assert rec["ran"] is False, rec
        assert rec["stopped"] == "freelist-ratio", rec
        assert rec["mode"] == 0
    finally:
        db.close()


# ── the automatic path records the numbers the reports carry ─────────────────

def test_auto_maintenance_records_reclaimed_bytes_and_lock_hold(tmp_path, monkeypatch):
    # Isolate the holder probe: it walks the REAL hermes home, which tests/home_io_guard.py
    # refuses from a checkout inside that home — an environmental refusal, not this behaviour.
    import hermes_state_holders as holders
    monkeypatch.setattr(holders, "foreign_state_db_holders", lambda *_a, **_k: [])
    monkeypatch.setattr(holders, "in_process_state_db_holders", lambda *_a, **_k: [])
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        _grow(db)
        monkeypatch.setattr(db, "prune_sessions", lambda **_kwargs: 1)
        monkeypatch.setattr(db, "_freelist_ratio", lambda: 0.9)

        result = db.maybe_auto_prune_and_vacuum(min_interval_hours=0)

        assert result["vacuumed"] is True, result
        assert result["reclaim_mode"] == "migrate", result
        assert result["reclaimed_bytes"] > 0, result
        assert isinstance(result["max_lock_hold_ms"], float) and result["max_lock_hold_ms"] > 0.0, result
        assert db.get_meta("last_vacuum") is not None

        # Second pass: INCREMENTAL, so no migration and no long hold.
        db.set_meta("last_auto_prune", "0")
        db.set_meta("last_vacuum", "0")
        _grow(db)
        again = db.maybe_auto_prune_and_vacuum(min_interval_hours=0)
        assert again["vacuumed"] is True, again
        assert again["reclaim_mode"] == "incremental", again
        assert again["freelist_ratio"] is None, again
    finally:
        db.close()


def test_min_vacuum_interval_days_defaults_to_one():
    sig = inspect.signature(SessionDB.maybe_auto_prune_and_vacuum)
    assert sig.parameters["min_vacuum_interval_days"].default == 1
    from hermes_cli.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["sessions"]["min_vacuum_interval_days"] == 1
    assert DEFAULT_CONFIG["sessions"]["vacuum_chunk_pages"] == 512
    assert DEFAULT_CONFIG["sessions"]["vacuum_max_pages_per_pass"] == 4096
    assert DEFAULT_CONFIG["sessions"]["vacuum_max_seconds_per_pass"] == 5.0
