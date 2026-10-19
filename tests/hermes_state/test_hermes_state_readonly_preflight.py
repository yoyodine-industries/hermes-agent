"""Tests for the read-only DB preflight (port of Kilo-Org/kilocode#12508).

A stray read-only ``state.db`` / ``-wal`` / ``-shm`` (sudo run, restored
backup, copied dotfiles) used to surface as an opaque
``sqlite3.OperationalError: attempt to write a readonly database`` raised
from deep inside ``_init_schema`` — naming no file and no fix.

``preflight_db_writability`` now runs before the first connection:

- files inside the Hermes home tree are repaired with ``chmod u+rw``
  (the safe scope — chmod fails on files the user doesn't own);
- anything else fails fast with an error naming the exact file and the
  exact ``chmod`` command;
- WAL sidecars are never deleted, so committed frames survive repair.

Only paths that must exist are judged: the directory and the main DB
file. ``-wal``/``-shm``/``-journal`` are SQLite's own bookkeeping — a
connection unlinks them at checkpoint, so *absent is the normal state*.
The preflight never tests, never reports and never repairs a sidecar:
reading one as a permission fault invented "state.db is not writable"
on healthy stores that had simply checkpointed a microsecond earlier,
and pointed operators at deleting the ``-wal`` that held their commits.
"""

import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

import hermes_state
from hermes_state import SessionDB, preflight_db_writability

pytestmark = [
    pytest.mark.platforms("posix"),  # POSIX chmod semantics
    pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses file permission checks",
    ),
]


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so the repair scope covers tmp DBs."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


def _make_wal_db(path: Path) -> None:
    """Create a WAL-mode DB with committed-but-uncheckpointed frames."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # Keep a READ-ONLY second connection open so neither close can
    # checkpoint: the writer skips checkpoint-on-close because another
    # connection exists, and the ro holder cannot checkpoint at all.
    # The committed row therefore lives only in the -wal file.
    holder = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    holder.execute("SELECT 1").fetchone()
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()
    holder.close()
    assert path.with_name(path.name + "-wal").is_file(), (
        "fixture precondition: -wal sidecar must survive with pending frames"
    )


class TestRepairScope:
    def test_repairs_readonly_db_inside_home(self, hermes_home):
        db = hermes_home / "state.db"
        _make_db(db)
        os.chmod(db, 0o444)

        preflight_db_writability(db, db_label="state.db")

        assert os.access(db, os.W_OK)

    def test_readonly_sidecars_are_not_policed(self, hermes_home):
        """A sidecar's mode is SQLite's business: repaired main DB, untouched -wal."""
        db = hermes_home / "state.db"
        _make_wal_db(db)
        wal = db.with_name(db.name + "-wal")
        assert wal.is_file(), "fixture must leave a -wal behind"
        os.chmod(db, 0o444)
        os.chmod(wal, 0o444)

        preflight_db_writability(db, db_label="state.db")

        assert os.access(db, os.W_OK)
        # Never chmod'd by the preflight — and never inspected as a fault.
        assert not os.access(wal, os.W_OK)


    def test_repairs_readonly_parent_directory(self, hermes_home):
        sub = hermes_home / "kanban"
        sub.mkdir()
        db = sub / "kanban.db"
        _make_db(db)
        os.chmod(sub, 0o555)
        try:
            preflight_db_writability(db, db_label="kanban.db")
            assert os.access(sub, os.W_OK)
        finally:
            os.chmod(sub, 0o755)


class TestRefusalOutsideScope:
    def test_actionable_error_names_file_and_chmod(self, hermes_home, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        db = outside / "custom.db"
        _make_db(db)
        os.chmod(db, 0o444)
        try:
            with pytest.raises(sqlite3.OperationalError) as exc_info:
                preflight_db_writability(db, db_label="custom.db")
            msg = str(exc_info.value)
            assert str(db) in msg
            # Must NOT have silently chmod'd a file outside the home tree.
            assert not os.access(db, os.W_OK)
        finally:
            os.chmod(db, 0o644)

    def test_readonly_wal_outside_home_does_not_refuse(self, hermes_home, tmp_path):
        """A read-only sidecar is not a store-level permission fault."""
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        db = outside / "custom.db"
        _make_wal_db(db)
        wal = db.with_name(db.name + "-wal")
        os.chmod(wal, 0o444)
        try:
            # The main DB and its directory are writable: nothing to refuse. SQLite
            # reports what it can actually open; the preflight forms no opinion.
            preflight_db_writability(db, db_label="custom.db")
        finally:
            os.chmod(wal, 0o644)


class TestSidecarsAreNotPoliced:
    """Absence of a -wal/-shm is SQLite checkpointing, never a permission fault."""

    def test_checkpointed_away_sidecars_pass_and_the_store_writes(self, hermes_home):
        db = hermes_home / "board.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()  # last close checkpoints and unlinks -wal/-shm
        wal = db.with_name(db.name + "-wal")
        shm = db.with_name(db.name + "-shm")
        assert not wal.exists() and not shm.exists(), (
            "fixture precondition: a clean close must leave no sidecars"
        )

        preflight_db_writability(db, db_label="board.db")

        conn = sqlite3.connect(str(db))
        try:
            conn.execute("INSERT INTO t VALUES (2)")
            conn.commit()
            assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 2
        finally:
            conn.close()

    def test_sidecar_unlinked_mid_check_is_absence_not_a_permission_fault(self, hermes_home, monkeypatch):
        """The production race: the sidecar is gone by the time anyone looks at it."""
        db = hermes_home / "kanban.db"
        _make_wal_db(db)
        sidecars = [db.with_name(db.name + suf) for suf in ("-wal", "-shm")]
        assert any(p.is_file() for p in sidecars), "fixture precondition: a sidecar must exist"

        real_access = os.access
        checkpointed = []

        def access_while_sqlite_checkpoints(path, mode, **kwargs):
            # A concurrent connection checkpoints: the preflight's existence
            # snapshot is already stale and the sidecars are unlinked.
            if not checkpointed:
                checkpointed.append(True)
                for sidecar in sidecars:
                    sidecar.unlink(missing_ok=True)
            return real_access(path, mode, **kwargs)

        monkeypatch.setattr(os, "access", access_while_sqlite_checkpoints)
        preflight_db_writability(db, db_label="kanban.db")  # must NOT raise
        monkeypatch.undo()

        conn = sqlite3.connect(str(db))
        try:
            conn.execute("INSERT INTO t VALUES (7)")
            conn.commit()
            # The store opens and writes: the vanished sidecar was absence, not a fault.
            assert conn.execute("SELECT count(*) FROM t WHERE x = 7").fetchone()[0] == 1
        finally:
            conn.close()

    def test_genuinely_readonly_db_still_refuses(self, hermes_home, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        db = outside / "custom.db"
        _make_db(db)
        os.chmod(db, 0o444)
        try:
            with pytest.raises(sqlite3.OperationalError) as exc_info:
                preflight_db_writability(db, db_label="custom.db")
            msg = str(exc_info.value)
            assert str(db) in msg
            assert "-wal" not in msg and "-shm" not in msg
            # The refusal is real: the store cannot be written.
            conn = sqlite3.connect(str(db))
            try:
                with pytest.raises(sqlite3.OperationalError):
                    conn.execute("INSERT INTO t VALUES (2)")
            finally:
                conn.close()
        finally:
            os.chmod(db, 0o644)


class TestSkips:



    def test_healthy_db_untouched(self, hermes_home):
        db = hermes_home / "state.db"
        _make_db(db)
        before = stat.S_IMODE(db.stat().st_mode)
        preflight_db_writability(db)
        assert stat.S_IMODE(db.stat().st_mode) == before


class TestSessionDBIntegration:
    def test_sessiondb_repairs_readonly_dir_before_fresh_db_lock(self, hermes_home):
        sub = hermes_home / "profiles" / "p1"
        sub.mkdir(parents=True)
        os.chmod(sub, 0o555)
        try:
            db = SessionDB(sub / "state.db")  # absent DB: the quarantine lock lives in this dir
            db.close()
            assert os.access(sub, os.W_OK)
        finally:
            os.chmod(sub, 0o755)

    def test_sessiondb_selfheals_readonly_db_in_home(self, hermes_home):
        db_path = hermes_home / "state.db"
        first = SessionDB(db_path)
        first.close()
        for suffix in ("", "-wal", "-shm"):
            p = db_path.with_name(db_path.name + suffix)
            if p.is_file():
                os.chmod(p, 0o444)

        db = SessionDB(db_path)  # must not raise "readonly database"
        try:
            assert os.access(db_path, os.W_OK)
        finally:
            db.close()

    def test_sessiondb_actionable_error_outside_home(
        self, hermes_home, tmp_path
    ):
        outside = tmp_path / "custom-loc"
        outside.mkdir()
        db_path = outside / "state.db"
        first = SessionDB(db_path)
        first.close()
        os.chmod(db_path, 0o444)
        hermes_state._set_last_init_error(None)
        try:
            with pytest.raises(sqlite3.OperationalError) as exc_info:
                SessionDB(db_path)
            msg = str(exc_info.value)
            assert str(db_path) in msg
        finally:
            os.chmod(db_path, 0o644)
            hermes_state._set_last_init_error(None)
