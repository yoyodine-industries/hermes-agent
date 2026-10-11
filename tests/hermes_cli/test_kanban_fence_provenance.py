"""D1 — the delegated-child fence must survive ``env -u`` (ruling t_fcf7a321).

WHY
---
The two cooperative arms of the fence (the ``_DELEGATED_CHILD_CONTEXT`` ContextVar and the
``HERMES_DELEGATED_CHILD_CONTEXT`` marker inside ``kanban_path_is_fenced``) are both clearable
by the very shell they hold: a worker's terminal tool re-adds the marker, and the worker can
strip it with ``env -u``. The PROVENANCE arm is not clearable — a descendant cannot change who
its ancestors are — so an estate-mutating ``hermes kanban`` verb is refused when an ANCESTOR
pid equals the ``worker_pid`` of a LIVE claim on the board, exactly as the dispatcher defines a
live claim (``status='running'``, unexpired ``claim_expires``, ``worker_pid`` recorded) with the
``worker_started_at`` fingerprint as the PID-reuse guard.

This suite drives the REAL CLI seam (parse -> ``kanban_command``) in a CHILD process whose
parent IS that live-claim ``worker_pid``, with ``HERMES_DELEGATED_CHILD_CONTEXT`` absent — the
``env -u`` state. At the pre-fix base these verbs are ADMITTED; at the tip they are refused.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

REPO = Path(__file__).resolve().parents[2]

PROBE_SRC = """\
import argparse
import sys

from hermes_cli import kanban, kanban_parser

parser = argparse.ArgumentParser(prog="hermes")
sub = parser.add_subparsers(dest="command")
kanban_parser.build_parser(sub)
args = parser.parse_args(["kanban", *sys.argv[1:]])
raise SystemExit(kanban.kanban_command(args))
"""

#: Bypasses the CLI entirely and calls the real DB mutator — proves the DURABLE seam.
DURABLE_PROBE_SRC = """\
import sys

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

try:
    conn = kbc.connect()
    kb.create_task(conn, title="durable-probe")
except PermissionError as exc:
    print("REFUSED:", exc)
    raise SystemExit(7)
print("ADMITTED")
raise SystemExit(0)
"""

BOARD = "fence-probe"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    # A dispatched worker shell exports these; left in place the test process (a descendant of
    # the very worker whose card this is) would consult the LIVE board's claims. Clear every pin
    # so the suite reads only the temp root (yaan-kanban-board: "delenv every pin, then assert").
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_OPERATOR_ASK",
                "HERMES_KANBAN_ADVISORY_SKILLS", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return root


def _board_db(root: Path, slug: str) -> Path:
    """The board's store path WITHOUT the registration check (a probe board may not exist yet)."""
    return kb.boards_root() / slug / "kanban.db"


def _make_board_with_live_claim(root: Path, *, slug: str, worker_pid: int, fingerprint):
    """A board whose only card is a LIVE claim owned by ``worker_pid``."""
    kb.create_board(slug, name=slug)
    db = _board_db(root, slug)
    conn = kbc.connect(db_path=db)
    try:
        task_id = kb.create_task(conn, title="owned by a live worker", assignee="platform-coder",
                                 board=slug)
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, worker_started_at=?, "
            "claim_lock=?, claim_expires=? WHERE id=?",
            (int(worker_pid), fingerprint, "test-host:probe",
             int(time.time()) + 3600, task_id),
        )
        conn.commit()
    finally:
        conn.close()
    # The arm caches the claim set per store for ``LIVE_CLAIM_PROBE_TTL_SECONDS``; drop the empty
    # entry an earlier (pre-claim) read of this store may have left, so the fixture's claim is
    # visible immediately — exactly what a real worker's shell sees at spawn time.
    from agent.delegation_context import _live_claim_cache

    _live_claim_cache.clear()
    return task_id


def _write(path: Path, src: str) -> Path:
    path.write_text(src, encoding="utf-8")
    return path


def _child_env(root: Path, board: str, *, pin_board: bool = True, **extra) -> dict:
    """A worker-shell-shaped env: no marker, no ContextVar, board pinned (as scrub keeps it).

    ``pin_board=False`` drops ``HERMES_KANBAN_DB``/``HERMES_KANBAN_BOARD`` so the effective
    board is whatever ``<kanban>/current`` points at — exactly what a flow's ``clean_env()``
    call leaves behind, and the state in which the pre-fix fast-fail went non-deterministic.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HERMES_KANBAN_") and not k.startswith("HERMES_DELEGATED")}
    env["HERMES_HOME"] = str(root)
    env["HERMES_KANBAN_HOME"] = str(root)
    if pin_board:
        env["HERMES_KANBAN_DB"] = str(_board_db(root, board))
        env["HERMES_KANBAN_BOARD"] = board
    env["PYTHONPATH"] = str(REPO)
    env.update(extra)
    return env


def _run(script: Path, root: Path, board: str, *argv, **extra):
    return subprocess.run(
        [sys.executable, str(script), *argv],
        env=_child_env(root, board, **extra), cwd=str(REPO),
        capture_output=True, text=True, timeout=120,
    )


def _fingerprint_of_this_process():
    """The dispatcher's own restart-stable fingerprint, or ``None`` (legacy NULL shape)."""
    from hermes_cli.kanban_db_dispatch import _process_fingerprint

    return _process_fingerprint(os.getpid())


def _sanity_the_probe_imports_the_tree_under_test(tmp_path, home):
    script = _write(tmp_path / "which_tree.py", "import hermes_cli, sys\n"
                                                "print(hermes_cli.__file__)\n")
    res = subprocess.run([sys.executable, str(script)], env=_child_env(home, BOARD),
                         cwd=str(REPO), capture_output=True, text=True, timeout=60)
    assert str(REPO) in res.stdout, res.stdout + res.stderr


def test_the_child_tree_is_the_live_checkout(tmp_path, home):
    _sanity_the_probe_imports_the_tree_under_test(tmp_path, home)


def test_marker_arm_still_refuses_a_plain_delegated_child(home, tmp_path):
    """Control: with the marker SET the cooperative arm refuses (the harness can see refusals)."""
    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    script = _write(tmp_path / "cli.py", PROBE_SRC)
    res = _run(script, home, BOARD, "create", "x",
               HERMES_DELEGATED_CHILD_CONTEXT="1")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "child contexts cannot mutate" in res.stderr


def test_provenance_arm_refuses_env_u_shell_descended_from_live_worker(home, tmp_path):
    """THE LOAD-BEARING CASE: marker stripped, ancestor pid == live-claim worker_pid."""
    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    script = _write(tmp_path / "cli.py", PROBE_SRC)
    # No HERMES_DELEGATED_CHILD_CONTEXT anywhere -> exactly what `env -u` produces.
    res = _run(script, home, BOARD, "create", "x")
    assert res.returncode == 1, f"verb was ADMITTED at the pre-fix base: {res.stdout}{res.stderr}"
    assert "child contexts cannot mutate" in res.stderr
    assert "boards rm --estate" in res.stderr  # the refusal names the ONE release


def test_provenance_arm_also_guards_the_durable_db_seam(home, tmp_path):
    """A child that imports the DB mutator directly is refused by the same arm."""
    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    script = _write(tmp_path / "durable.py", DURABLE_PROBE_SRC)
    res = _run(script, home, BOARD)
    assert res.returncode == 7, res.stdout + res.stderr
    assert "REFUSED" in res.stdout
    assert "descends from a LIVE dispatched worker" in res.stdout


def test_provenance_arm_ignores_a_claim_owned_by_another_pid(home, tmp_path):
    """PRECISION: a live claim owned by a pid that is NOT an ancestor never refuses."""
    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid() + 400000,
                                fingerprint=None)
    script = _write(tmp_path / "cli.py", PROBE_SRC)
    res = _run(script, home, BOARD, "create", "x")
    assert res.returncode == 0, res.stdout + res.stderr


def test_provenance_arm_ignores_an_expired_claim(home, tmp_path):
    """PRECISION: an EXPIRED claim is not live, so the same ancestry is admitted."""
    task_id = _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                          fingerprint=_fingerprint_of_this_process())
    conn = kbc.connect(db_path=_board_db(home, BOARD))
    try:
        conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?",
                     (int(time.time()) - 60, task_id))
        conn.commit()
    finally:
        conn.close()
    script = _write(tmp_path / "cli.py", PROBE_SRC)
    res = _run(script, home, BOARD, "create", "x")
    assert res.returncode == 0, res.stdout + res.stderr


def test_reads_are_never_fenced(home, tmp_path):
    """ADDITIVE: a read from the same ancestry is untouched (every read verb the CLI has)."""
    task_id = _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                          fingerprint=_fingerprint_of_this_process())
    script = _write(tmp_path / "cli.py", PROBE_SRC)
    for argv in (("list",), ("show", task_id), ("boards", "list"), ("stats",)):
        res = _run(script, home, BOARD, *argv)
        assert res.returncode == 0, f"{argv}: {res.stdout}{res.stderr}"


def test_same_pid_is_not_an_ancestor_match():
    """The arm is ancestor-only: a process is never its own ancestor."""
    from agent.delegation_context import process_ancestors

    chain = process_ancestors()
    assert os.getpid() not in chain


def test_live_claim_read_is_bounded_and_read_only(home):
    """The arm reads the claim set read-only and never mutates the store."""
    from agent.delegation_context import live_claim_workers

    task_id = _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                          fingerprint=_fingerprint_of_this_process())
    db = _board_db(home, BOARD)
    before = db.read_bytes()
    claims = live_claim_workers(db)
    assert claims, "the live claim was not visible to the provenance read"
    assert before == db.read_bytes()


def test_store_path_is_resolved_from_the_env_pin(home, monkeypatch):
    """With no explicit path the arm falls back to the pinned board (what a worker's shell has)."""
    from agent.delegation_context import _resolve_claim_store

    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    monkeypatch.setenv("HERMES_KANBAN_DB", str(_board_db(home, BOARD)))
    assert _resolve_claim_store(None) == str(_board_db(home, BOARD).resolve())


def test_connect_serves_a_provenance_fenced_descendant_read_only(home, monkeypatch):
    """A read on the fenced board still works: the schema pass is skipped, not refused."""
    from agent.delegation_context import live_claim_workers

    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    store = _board_db(home, BOARD)
    assert live_claim_workers(store), "fixture did not leave a live claim"
    # Same shape the CLI's init_db()/handler uses; it must NOT raise for a read.
    conn = kbc.connect(db_path=store)
    try:
        assert conn.execute("select count(*) from tasks").fetchone()[0] == 1
    finally:
        conn.close()


# --- Target-board resolution of the fast-fail (card t_63a0c2d9) ---------------------------------
#
# The fast-fail answered "which board?" from the AMBIENT chain (HERMES_KANBAN_DB ->
# HERMES_KANBAN_BOARD -> <kanban>/current) and never read the ``--board`` the invocation TARGETS.
# A flow that drops the pins (``primitives.clean_env()``) therefore resolved to the host-global
# ``<kanban>/current``: a mutation targeting the worker's OWN board could slip through when another
# board was current, and one targeting a foreign board was refused whenever its own board was
# current. These tests pin the fast-fail to the TARGET board and prove the verdict is invariant
# under ``<kanban>/current``.

OTHER_BOARD = "fence-probe-other"


def _make_foreign_board(root: Path, slug: str) -> Path:
    """A registered board with NO live claim — the foreign target of a ``--board`` override."""
    kb.create_board(slug, name=slug)
    return _board_db(root, slug)


def test_board_override_refuses_only_the_target_board(home, tmp_path):
    """``--board X`` (a board this process holds a claim on) refused; ``--board Y`` (no claim) admitted.

    The pin is deliberately left at the OLD ambient board (``_child_env`` default) so the pre-fix
    code judges ``X`` for both commands and over-refuses the foreign one.
    """
    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    _make_foreign_board(home, OTHER_BOARD)
    script = _write(tmp_path / "cli.py", PROBE_SRC)

    own = _run(script, home, BOARD, "--board", BOARD, "create", "x")
    assert own.returncode == 1, own.stdout + own.stderr
    assert "child contexts cannot mutate" in own.stderr

    foreign = _run(script, home, BOARD, "--board", OTHER_BOARD, "create", "x")
    assert foreign.returncode == 0, (
        "the fast-fail judged the ambient board instead of the --board target: "
        f"{foreign.stdout}{foreign.stderr}"
    )


def test_board_override_verdict_is_independent_of_the_current_pointer(home, tmp_path):
    """The same command's verdict must not move when ``<kanban>/current`` is switched.

    With the board pins absent (a real flow call drops them), the ambient board IS
    ``<kanban>/current``. Judging the fast-fail against it made the worker's OWN board writable
    when another board was current, and a foreign board refused when its own was current — the
    non-determinism this fix removes. Run the identical pair under both pointer values and require
    the SAME verdicts.
    """
    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    _make_foreign_board(home, OTHER_BOARD)
    script = _write(tmp_path / "cli.py", PROBE_SRC)

    for pointed_at in (BOARD, OTHER_BOARD):
        kb.set_current_board(pointed_at)
        own = _run(script, home, BOARD, "--board", BOARD, "create", "x", pin_board=False)
        assert own.returncode == 1, f"current={pointed_at}: {own.stdout}{own.stderr}"
        foreign = _run(script, home, BOARD, "--board", OTHER_BOARD, "create", "x", pin_board=False)
        assert foreign.returncode == 0, f"current={pointed_at}: {foreign.stdout}{foreign.stderr}"

