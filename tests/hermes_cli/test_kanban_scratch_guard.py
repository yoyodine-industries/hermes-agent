"""The scratch-store guard: a harness can never write a LIVE board store (#kanban-scratch).

Regression for the 2026-09-27 incident: an evidence harness resolved its "scratch" board
store through ``kanban_db.kanban_db_path()``, which follows the HOST's current-board pointer,
so a backup overwrote the live ``boards/ops/kanban.db``. These tests pin the two required
properties — a live destination is REFUSED, and an AMBIGUOUS resolution ABORTS — plus the
positive half: a scratch destination is reached without any live path moving.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_scratch


@pytest.fixture(autouse=True)
def _isolate_kanban_env(monkeypatch):
    """Restore the kanban env after each test — ``apply_scratch_env`` writes ``os.environ``."""
    for key in ("HERMES_KANBAN_HOME", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(key, raising=False)


def _board_db(path: Path, rows: int) -> Path:
    """A minimal board store (the shape the incident clobbered)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute("create table if not exists tasks (id text primary key, title text)")
        for i in range(rows):
            con.execute("insert or replace into tasks values (?, ?)", (f"t_{i}", f"card {i}"))
        con.commit()
    finally:
        con.close()
    return path


@pytest.fixture
def live_root(tmp_path, monkeypatch):
    """A stand-in for the operator's REAL kanban root, with two live board stores."""
    root = tmp_path / "live-hermes"
    _board_db(root / "kanban" / "boards" / "ops" / "kanban.db", 4)
    _board_db(root / "kanban" / "boards" / "defcon" / "kanban.db", 3)
    (root / "kanban" / "current").write_text("ops\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    assert kanban_scratch.live_kanban_root() == root.resolve()
    return root


@pytest.fixture
def run_root(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    return root


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── the refusal half ────────────────────────────────────────────────────────


def test_writing_a_live_board_store_is_refused(live_root, run_root):
    """A live store is refused as a destination, and the refusal changes nothing."""
    live_ops = live_root / "kanban" / "boards" / "ops" / "kanban.db"
    before = _digest(live_ops)

    with pytest.raises(kanban_scratch.UnsafeKanbanStore) as excinfo:
        kanban_scratch.assert_private_store(live_ops, run_root=run_root)

    assert str(live_ops) in str(excinfo.value)
    assert _digest(live_ops) == before  # the live store was not opened, let alone written


def test_every_live_store_shape_is_on_the_deny_list(live_root):
    """Named boards, the current board, and the back-compat default are all recognised."""
    live_root / "kanban.db"  # back-compat default board store
    stores = kanban_scratch.live_board_stores(live_root)
    assert live_root / "kanban.db" in stores
    assert live_root / "kanban" / "boards" / "ops" / "kanban.db" in stores
    assert live_root / "kanban" / "boards" / "defcon" / "kanban.db" in stores


def test_the_current_board_pointer_cannot_steer_the_destination(live_root, run_root, monkeypatch):
    """The incident's exact shape: the destination came from the current-board resolver.

    ``kanban_db_path()`` pointed at the live ops store; the harness backed a board up onto it.
    Whatever that resolver answers, the guard refuses the result as a destination.
    """
    from hermes_cli.kanban_db import kanban_db_path

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(live_root))  # the harness's own override
    resolved = kanban_db_path(board="ops")
    assert resolved == live_root / "kanban" / "boards" / "ops" / "kanban.db"  # live path

    with pytest.raises(kanban_scratch.UnsafeKanbanStore):
        kanban_scratch.assert_private_store(resolved, run_root=run_root)


# ── the ambiguity half (fail closed) ────────────────────────────────────────


def test_a_destination_outside_the_run_directory_aborts(live_root, run_root, tmp_path):
    """Not provably under the run's own directory ⇒ abort, not warn."""
    elsewhere = tmp_path / "elsewhere" / "kanban" / "boards" / "ops" / "kanban.db"
    with pytest.raises(kanban_scratch.UnsafeKanbanStore, match="not under this run"):
        kanban_scratch.assert_private_store(elsewhere, run_root=run_root)


def test_a_run_directory_that_owns_the_live_root_aborts(live_root, tmp_path):
    """A run whose directory contains the live install IS the live install."""
    with pytest.raises(kanban_scratch.UnsafeKanbanStore, match="owns the LIVE root"):
        kanban_scratch.assert_private_store(
            live_root / "scratch" / "kanban.db", run_root=live_root
        )
    with pytest.raises(kanban_scratch.UnsafeKanbanStore, match="owns the LIVE root"):
        kanban_scratch.assert_private_store(tmp_path / "x.db", run_root=tmp_path)

    # …and the live store itself is refused by the more specific rule, ahead of both.
    with pytest.raises(kanban_scratch.UnsafeKanbanStore, match="LIVE board store"):
        kanban_scratch.assert_private_store(
            live_root / "kanban" / "boards" / "ops" / "kanban.db", run_root=live_root
        )


def test_the_workspace_convention_works_and_a_live_store_is_still_refused(live_root):
    """A scratch run INSIDE the live tree (the host's own workspace convention) is fine.

    What is refused is a live store SLOT: nothing below ``boards/<slug>/`` is enumerated by
    any resolver, so a card's evidence dir is a safe root while ``boards/ops/kanban.db``
    stays refused even when it is handed to the guard as a run-relative path.
    """
    run = live_root / "kanban" / "boards" / "defcon" / "workspaces" / "t_x" / "evidence"
    run.mkdir(parents=True)

    dest = kanban_scratch.assert_private_store(
        run / "kanban" / "boards" / "ops" / "kanban.db", run_root=run
    )
    assert dest == (run / "kanban" / "boards" / "ops" / "kanban.db").resolve()
    assert kanban_scratch.apply_scratch_env(run) == run.resolve()

    with pytest.raises(kanban_scratch.UnsafeKanbanStore, match="LIVE board store"):
        kanban_scratch.assert_private_store(
            live_root / "kanban" / "boards" / "ops" / "kanban.db", run_root=run
        )


def test_pinning_the_kanban_root_at_or_over_the_live_root_aborts(live_root, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(live_root))
    with pytest.raises(kanban_scratch.UnsafeKanbanStore):
        kanban_scratch.apply_scratch_env(live_root)
    monkeypatch.setenv("HERMES_HOME", str(live_root / "kanban"))
    with pytest.raises(kanban_scratch.UnsafeKanbanStore):
        kanban_scratch.apply_scratch_env(live_root)


# ── the positive half: scratch works, live never moves ──────────────────────


def test_a_scratch_snapshot_lands_privately_and_leaves_the_live_store_alone(
    live_root, run_root
):
    """Copying a board OUT of the live install is allowed; the live bytes never change."""
    live_ops = live_root / "kanban" / "boards" / "ops" / "kanban.db"
    before = _digest(live_ops)

    dest, copied = kanban_scratch.snapshot_board_into_scratch(
        "ops", source_root=live_root, run_root=run_root
    )

    assert dest == (run_root / "kanban" / "boards" / "ops" / "kanban.db").resolve()
    assert copied == 4
    assert _digest(live_ops) == before
    assert _digest(dest) != before or dest.stat().st_size  # the copy exists and is real
    con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        assert con.execute("select count(*) from tasks").fetchone()[0] == 4
    finally:
        con.close()


def test_the_snapshot_never_writes_through_the_hosts_current_board(live_root, run_root):
    """The regression proper: pointer at 'ops', snapshot of 'defcon', live ops untouched."""
    live_ops = live_root / "kanban" / "boards" / "ops" / "kanban.db"
    before_digest, before_mtime = _digest(live_ops), live_ops.stat().st_mtime

    dest, copied = kanban_scratch.snapshot_board_into_scratch(
        "defcon", source_root=live_root, run_root=run_root
    )

    assert copied == 3
    assert dest.name == "kanban.db" and "boards/defcon" in str(dest)
    assert _digest(live_ops) == before_digest
    assert live_ops.stat().st_mtime == before_mtime
    assert sqlite3.connect(f"file:{live_ops}?mode=ro", uri=True).execute(
        "select count(*) from tasks"
    ).fetchone()[0] == 4


def test_apply_scratch_env_pins_every_resolver_to_the_run(live_root, run_root, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live_root / "kanban" / "boards" / "ops" / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "ops")

    root = kanban_scratch.apply_scratch_env(run_root)

    assert root == run_root.resolve()
    assert kanban_scratch.kanban_root_in_use() == run_root.resolve()
    from hermes_cli.kanban_db import kanban_db_path

    assert kanban_db_path(board="ops") == run_root.resolve() / "kanban" / "boards" / "ops" / "kanban.db"
    assert kanban_db_path(board="ops") not in kanban_scratch.live_board_stores(live_root)


def test_a_missing_source_board_store_is_refused_not_created(live_root, run_root):
    with pytest.raises(kanban_scratch.UnsafeKanbanStore, match="does not exist"):
        kanban_scratch.snapshot_board_into_scratch(
            "no-such-board", source_root=live_root, run_root=run_root
        )
    assert not (run_root / "kanban" / "boards" / "no-such-board").exists()
