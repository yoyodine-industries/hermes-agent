"""The header-probe cache is bounded by an idle window (the dashboard's fd release).

``hermes_state_dbfile`` keeps one O_RDONLY probe fd per ``state.db`` for the process lifetime.
For a multiplexed READER (``hermes dashboard``) that is the wrong default: it lists every
profile's store per request and then serves nothing, yet one fd per store keeps it a FOREIGN
holder of all of them, so ``hermes_state_holders`` skips each store's one-time auto_vacuum
rewrite (``vacuum_skipped_holders``) for as long as the process lives.

``hermes_state_registry.release_idle_header_probes`` closes the probes nothing has read for the
idle window, using the same lock discipline as ``close_all_under``: a probe whose inode still has
another descriptor in this process is KEPT (closing any descriptor cancels the process's POSIX
locks), and an entry re-read after the snapshot is kept too.
"""

import os
import sqlite3
import time
from pathlib import Path

import pytest

import hermes_state_dbfile as dbfile
import hermes_state_registry as registry


@pytest.fixture(autouse=True)
def _clean_state(tmp_path):
    registry.close_all()
    registry._generations.clear()
    registry._retired.clear()
    registry._opening.clear()
    registry._tearing_down.clear()
    yield
    registry.close_all()
    with dbfile._HEADER_PROBE_LOCK:
        for key in [k for k in dbfile._HEADER_PROBE_FDS if Path(k).is_relative_to(tmp_path)]:
            fd, _dev, _ino = dbfile._HEADER_PROBE_FDS.pop(key)
            os.close(fd)
        for key in [k for k in dbfile._HEADER_PROBE_LAST_USED if Path(k).is_relative_to(tmp_path)]:
            dbfile._HEADER_PROBE_LAST_USED.pop(key, None)


def _home(tmp_path, name):
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True)
    db_path = home / "state.db"
    registry.release(registry.acquire(db_path))  # creates the schema; final release closes it
    return home, db_path


def _probe(db_path):
    """Cache the probe and return its entry, with a marker that survives a re-open."""
    assert dbfile._pread_db_header(db_path, 16) == b"SQLite format 3\x00"
    fd = dbfile._HEADER_PROBE_FDS[str(db_path)][0]
    os.set_inheritable(fd, True)  # os.open never sets it
    return dbfile._HEADER_PROBE_FDS[str(db_path)]


def _age(db_path, seconds):
    key = str(db_path)
    assert key in dbfile._HEADER_PROBE_FDS
    dbfile._HEADER_PROBE_LAST_USED[key] = time.monotonic() - seconds


def _is_open(fd):
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


@pytest.mark.platforms("posix")
def test_probe_idle_past_the_window_is_closed(tmp_path):
    home, db_path = _home(tmp_path, "idle")
    entry = _probe(db_path)
    _age(db_path, 3600)

    assert registry.release_idle_header_probes(60, root=tmp_path) == 1
    assert str(db_path) not in dbfile._HEADER_PROBE_FDS
    assert str(db_path) not in dbfile._HEADER_PROBE_LAST_USED
    assert not _is_open(entry[0])


@pytest.mark.platforms("posix")
def test_recently_read_probe_is_kept(tmp_path):
    _home(tmp_path, "warm")
    db_path = tmp_path / "profiles" / "warm" / "state.db"
    entry = _probe(db_path)
    _age(db_path, 5)  # inside a 60 s window

    assert registry.release_idle_header_probes(60, root=tmp_path) == 0
    assert dbfile._HEADER_PROBE_FDS.get(str(db_path)) == entry
    assert _is_open(entry[0])
    assert os.get_inheritable(entry[0]), "the probe was closed and re-opened"


@pytest.mark.platforms("posix")
def test_probe_read_again_after_the_snapshot_is_kept(tmp_path):
    """The release re-checks the entry under the probe lock: a read that landed after the
    snapshot (last-used refreshed) must not be closed out from under the reader."""
    _home(tmp_path, "raced")
    db_path = tmp_path / "profiles" / "raced" / "state.db"
    entry = _probe(db_path)
    _age(db_path, 3600)
    stale = dbfile.idle_header_probes(60)[str(db_path)]

    assert dbfile._pread_db_header(db_path, 16) == b"SQLite format 3\x00"  # the reader's read
    assert dbfile.close_header_probe_if_idle(str(db_path), stale, 60) is False
    assert dbfile._HEADER_PROBE_FDS.get(str(db_path)) == entry


@pytest.mark.platforms("posix")
def test_live_connection_keeps_its_probe(tmp_path):
    """A tracked connection (the gateway, an open chat) owns the inode; the probe must stay."""
    home, db_path = _home(tmp_path, "served")
    entry = _probe(db_path)
    live = registry.acquire(db_path)
    try:
        _age(db_path, 3600)
        assert registry.release_idle_header_probes(60, root=tmp_path) == 0
        assert dbfile._HEADER_PROBE_FDS.get(str(db_path)) == entry
        assert _is_open(entry[0])
    finally:
        registry.release(live)
    registry.close_all()

    _age(db_path, 3600)
    assert registry.release_idle_header_probes(60, root=tmp_path) == 1
    assert not _is_open(entry[0])


@pytest.mark.platforms("posix")
def test_untracked_descriptor_keeps_the_probe(tmp_path):
    home, db_path = _home(tmp_path, "bare")
    entry = _probe(db_path)
    _age(db_path, 3600)
    conn = sqlite3.connect(db_path)
    conn.execute("SELECT 1 FROM sqlite_master").fetchall()
    try:
        assert registry.release_idle_header_probes(60, root=tmp_path) == 0
        assert dbfile._HEADER_PROBE_FDS.get(str(db_path)) == entry
    finally:
        conn.close()
    assert registry.release_idle_header_probes(60, root=tmp_path) == 1
    assert not _is_open(entry[0])


@pytest.mark.platforms("posix")
def test_idle_probes_are_ordered_least_recently_used_first(tmp_path):
    _home(tmp_path, "old")
    _home(tmp_path, "newer")
    old_db = tmp_path / "profiles" / "old" / "state.db"
    newer_db = tmp_path / "profiles" / "newer" / "state.db"
    _probe(old_db)
    _probe(newer_db)
    _age(old_db, 7200)
    _age(newer_db, 120)

    aged = dbfile.idle_header_probes(60)
    assert list(aged) == [str(old_db), str(newer_db)]


@pytest.mark.platforms("posix")
def test_release_scopes_to_the_root_it_is_given(tmp_path):
    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    _home(tmp_path, "inside")
    inside_db = tmp_path / "profiles" / "inside" / "state.db"
    outside_db = other_root / "state.db"
    registry.release(registry.acquire(outside_db))
    inside_entry = _probe(inside_db)
    outside_entry = _probe(outside_db)
    _age(inside_db, 3600)
    _age(outside_db, 3600)

    assert registry.release_idle_header_probes(60, root=tmp_path / "profiles") == 1
    assert not _is_open(inside_entry[0])
    assert dbfile._HEADER_PROBE_FDS.get(str(outside_db)) == outside_entry


@pytest.mark.platforms("posix")
def test_probe_with_no_bookkeeping_counts_as_idle(tmp_path):
    """A probe cached before last-used tracking existed must not be immortal."""
    _home(tmp_path, "legacy")
    db_path = tmp_path / "profiles" / "legacy" / "state.db"
    entry = _probe(db_path)
    dbfile._HEADER_PROBE_LAST_USED.pop(str(db_path), None)

    assert registry.release_idle_header_probes(60, root=tmp_path) == 1
    assert not _is_open(entry[0])


@pytest.mark.platforms("posix")
def test_a_live_read_keeps_a_store_that_a_release_just_skipped(tmp_path):
    """End-to-end shape of the dashboard: a page request re-probes the store, so the next sweep
    (newer than the window) leaves it alone; once requests stop, a later sweep closes it."""
    home, db_path = _home(tmp_path, "page")
    _probe(db_path)
    _age(db_path, 3600)
    # A page request opens the store read-only (its header probe is the first read it does).
    assert dbfile._pread_db_header(db_path, 16) == b"SQLite format 3\x00"
    assert registry.release_idle_header_probes(60, root=tmp_path) == 0
    # No further request: the idle window passes and the probe is released.
    _age(db_path, 61)
    assert registry.release_idle_header_probes(60, root=tmp_path) == 1
    assert str(db_path) not in dbfile._HEADER_PROBE_FDS
