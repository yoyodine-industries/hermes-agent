"""D2/D3 — declared-estate teardown + accurate refusal text (ruling t_fcf7a321).

D2: a board whose OWN ``board.json`` carries ``"dispatch": false`` is archived SINGLE-ACTOR
through the real CLI (``hermes kanban boards rm --estate <slug> --reason "…"``): the ask/APR
clause is waived while the destination claim, the VERIFIED preimage and a ledger/audit row are
all still taken. Four fail-closed refusals guard the waiver, and a hard delete (``--delete``)
keeps the FULL ceremony.

D3: the refusal a gated verb prints must state the ACTUAL requirement — one ask filed by
``platform-stl`` with ``REQUIRED_APPROVALS=0`` (no second approval), plus the estate waiver path.

Every case drives ``kanban_parser.build_parser`` -> ``kanban.kanban_command`` — the real CLI
seam — never ``evaluate()`` directly. The only injections are the guard's own documented
host-injection knobs (``BULK_GUARD_LEDGER_DIR``, ``BULK_GUARD_PREIMAGE_CMD``).
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_bulk_guard as kbg
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_parser

ESTATE = "estate-probe"
LIVE = "live-probe"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv(kbg.LEDGER_DIR_ENV, str(tmp_path / "ledger"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    # The guard's sanctioned test/host-injection point for the preimage unit.
    stub = tmp_path / "preimage-stub.sh"
    stub.write_text(
        '#!/bin/sh\nprintf \'{"path": "%s", "bytes": 1, "sha256": "%s", '
        '"quick_check": "ok"}\\n\' "$2" "0000000000000000000000000000000000000000000000000000000000000000"\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(kbg.PREIMAGE_CMD_ENV, str(stub))
    return root


def _cli(*argv) -> int:
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kanban_parser.build_parser(sub)
    args = parser.parse_args(["kanban", *argv])
    return kb_cli.kanban_command(args)


def _make_estate(slug: str, *, task: bool = False):
    kb.create_board(slug, name=slug, dispatch=False)
    if task:
        conn = kbc.connect(board=slug)
        try:
            tid = kb.create_task(conn, title="estate card", assignee="platform-coder", board=slug)
        finally:
            conn.close()
        return tid
    return None


def _make_live(slug: str):
    kb.create_board(slug, name=slug)


def _ledger_rows():
    path = kbg.ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _audit_rows():
    path = kbg.audit_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- D2

def test_estate_board_is_archived_single_actor(home, capsys):
    _make_estate(ESTATE, task=True)
    rc = _cli("boards", "rm", ESTATE, "--estate", "--reason", "rehearsal board, task done")
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert not kb.board_exists(ESTATE)
    assert list(kb.boards_root().glob("_archived/" + ESTATE + "-*")), "board was not archived"
    teardown = [r for r in _ledger_rows() if r.get("role") == "estate-teardown"]
    assert len(teardown) == 1
    assert teardown[0]["board"] == ESTATE
    assert teardown[0]["reason"] == "rehearsal board, task done"
    assert teardown[0]["actor"], "the teardown record names no actor"
    assert teardown[0]["snapshot"], "the teardown record names no verified preimage"
    admitted = [r for r in _audit_rows() if r.get("outcome") == "admitted"]
    assert admitted and admitted[-1]["estate"] is True
    assert admitted[-1]["reason"] == "rehearsal board, task done"


def test_estate_marker_alone_keys_the_waiver(home, capsys):
    """An estate board archives single-actor by its own declaration, --estate or not."""
    _make_estate(ESTATE)
    rc = _cli("boards", "rm", ESTATE, "--reason", "declared estate, no ceremony")
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert [r for r in _ledger_rows() if r.get("role") == "estate-teardown"]


def test_estate_flag_on_a_non_estate_board_is_refused(home, capsys):
    _make_live(LIVE)
    rc = _cli("boards", "rm", LIVE, "--estate", "--reason", "nope")
    out = capsys.readouterr()
    assert rc == 1
    assert "estate_marker_absent" in out.err
    assert kb.board_exists(LIVE)


def test_estate_without_a_reason_is_refused(home, capsys):
    _make_estate(ESTATE)
    rc = _cli("boards", "rm", ESTATE, "--estate")
    out = capsys.readouterr()
    assert rc == 1
    assert "estate_reason_missing" in out.err
    assert kb.board_exists(ESTATE)


def test_estate_that_is_the_current_board_is_refused(home, capsys):
    _make_estate(ESTATE)
    kb.set_current_board(ESTATE)
    rc = _cli("boards", "rm", ESTATE, "--estate", "--reason", "still current")
    out = capsys.readouterr()
    assert rc == 1
    assert "estate_is_current_board" in out.err
    assert kb.board_exists(ESTATE)


def test_estate_with_a_live_claim_is_refused(home, capsys):
    tid = _make_estate(ESTATE, task=True)
    conn = kbc.connect(board=ESTATE)
    try:
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, claim_expires=? "
            "WHERE id=?",
            (os.getpid(), "test-host:probe", int(time.time()) + 3600, tid),
        )
        conn.commit()
    finally:
        conn.close()
    rc = _cli("boards", "rm", ESTATE, "--estate", "--reason", "worker still running")
    out = capsys.readouterr()
    assert rc == 1
    assert "estate_live_claim" in out.err
    assert kb.board_exists(ESTATE)


def test_estate_flag_with_hard_delete_is_refused(home, capsys):
    _make_estate(ESTATE)
    rc = _cli("boards", "rm", ESTATE, "--estate", "--delete", "--reason", "no")
    out = capsys.readouterr()
    assert rc == 1
    assert "estate_hard_delete" in out.err
    assert kb.board_exists(ESTATE)


def test_estate_board_with_absent_metadata_is_refused(home, capsys):
    """(7) an absent board.json is not an estate — a minted stub proves nothing."""
    kb.create_board(ESTATE, name=ESTATE, dispatch=False)
    kb.board_metadata_path(ESTATE).unlink()
    rc = _cli("boards", "rm", ESTATE, "--estate", "--reason", "no stub")
    out = capsys.readouterr()
    assert rc == 1
    assert "estate_marker_absent" in out.err
    assert kb.board_exists(ESTATE)


def test_estate_board_with_garbled_metadata_is_refused(home, capsys):
    """(7) a MALFORMED board.json is not an estate (read_board_metadata itself must not raise)."""
    kb.create_board(ESTATE, name=ESTATE, dispatch=False)
    kb.board_metadata_path(ESTATE).write_text("{ not json", encoding="utf-8")
    rc = _cli("boards", "rm", ESTATE, "--estate", "--reason", "garbled stub")
    out = capsys.readouterr()
    assert rc == 1, f"a garbled stub was treated as a declared estate: {out.out}{out.err}"
    assert "estate_marker_absent" in out.err
    assert kb.board_exists(ESTATE)


def test_non_estate_archive_still_needs_the_ceremony(home, capsys):
    """The waiver is NARROW: an ordinary board is still refused without its ask."""
    _make_live(LIVE)
    rc = _cli("boards", "rm", LIVE)
    out = capsys.readouterr()
    assert rc == 1
    assert "bulk_unapproved" in out.err
    assert kb.board_exists(LIVE)


# --------------------------------------------------------------------------- D3

def test_unapproved_refusal_states_the_real_requirement(home, capsys):
    _make_live(LIVE)
    rc = _cli("boards", "rm", LIVE)
    out = capsys.readouterr()
    assert rc == 1
    err = out.err
    assert "platform-stl" in err
    assert "no separate bot approvals are required" in err
    assert "REQUIRED_APPROVALS=0" in err
    assert "boards rm --estate" in err
    # The retired role and the phantom second approval must not survive anywhere in the refusal.
    assert "ops head" not in err.lower()
    assert "two distinct bot approvals" not in err.lower()


def test_approval_help_names_the_estate_waiver():
    _args, kwargs = kanban_parser._approval_flag("this action")
    help_text = kwargs["help"]
    assert "boards rm --estate" in help_text
    assert "no separate bot approvals" in help_text
    assert "ops head" not in help_text.lower()


def test_quorum_phrase_matches_the_guard():
    phrase = kanban_parser._quorum_phrase()
    assert "no separate bot approvals" in phrase
    assert "boards rm --estate" in phrase
    assert kbg.REQUIRED_APPROVALS == 0
    assert kbg.AUTHORIZED_ASK_PROFILE == "platform-stl"


def test_the_fifth_named_site_is_corrected():
    """D3 names tests/hermes_cli/bulk_guard_admission.py — its prose drives what tests assert."""
    text = (REPO / "tests/hermes_cli/bulk_guard_admission.py").read_text(encoding="utf-8")
    assert "ops head" not in text.lower()
    assert "two distinct bot approval" not in text.lower()
    assert "platform-stl" in text
