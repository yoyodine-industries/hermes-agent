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


# --- the designation door fences the store the WRITE LANDS ON (card t_57e33100) ----------------
#
# ``designate_priority(conn, task_id, board=<name>)`` may NAME a board for its ledger row, but the
# write lands on ``conn``'s store. The fast-fail judged ``kanban_db_path(board=<name>)``, which
# answers the ENV-PINNED store whenever a worker's pin is authoritative
# (``_explicit_board_intent_pinned``: a dispatched worker, or any delegated child carrying the
# marker). From a delegated child the verdict therefore spoke about a board that was neither the
# write's board nor the board the invocation named: a designation whose write landed OUTSIDE the
# fence was refused because the PIN - this lineage's own store - was fenced.
#
# The subject is now the connection's store (``_write_fence_target_store``), with the named board
# only as the fallback for a connection whose store cannot be told. These tests pin the subject,
# and prove the arm is not weakened by it.

DESIGNATE_OWN = "fence-probe-designate"
DESIGNATE_ELSEWHERE = "fence-probe-elsewhere"
UNREGISTERED = "fence-probe-unregistered"

DESIGNATE_PROBE_SRC = """\
import sys

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

task_id = sys.argv[1]
try:
    conn = kbc.connect()
    kb.designate_priority(conn, task_id, reason="provenance probe")
except PermissionError as exc:
    print("REFUSED:", exc)
    raise SystemExit(7)
print("ADMITTED")
raise SystemExit(0)
"""


def _rooted(monkeypatch, root: Path) -> None:
    """Point the board resolution at *root* (the caller then creates/opens boards there)."""
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))


def _store_under(root: Path, slug: str) -> Path:
    return root / "kanban" / "boards" / slug / "kanban.db"


def _board_and_card(monkeypatch, root: Path, slug: str):
    """Create board *slug* under *root* with one ordinary card; returns (store, conn, task_id)."""
    _rooted(monkeypatch, root)
    kb.create_board(slug, name=slug)
    store = _store_under(root, slug)
    conn = kbc.connect(db_path=store)
    task_id = kb.create_task(conn, title="an ordinary card", assignee="platform-coder", board=slug)
    return store, conn, task_id


def test_designate_is_judged_by_the_connection_store_not_the_pin(home, tmp_path, monkeypatch):
    """REGRESSION (red on the pre-fix base, green after): the connection's store decides.

    The lineage is fenced for ``lineage/`` and PINNED to its own store there; the designation
    writes to a store in ``elsewhere/`` while naming an unregistered board. Judged against the pin
    (pre-fix) the write is refused although it lands outside the fence; judged against the
    connection (post-fix) it is admitted and the card really does take the designated rank.
    """
    lineage = tmp_path / "lineage"
    elsewhere = tmp_path / "elsewhere"
    pinned_store, pinned_conn, _ = _board_and_card(monkeypatch, lineage, DESIGNATE_OWN)
    _, conn, task_id = _board_and_card(monkeypatch, elsewhere, DESIGNATE_ELSEWHERE)
    try:
        assert pinned_store.is_file() and conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        # Arm the fence exactly as a delegated child's shell carries it: the marker names the
        # lineage root, and the env pin names this lineage's OWN store (both are the child's).
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(lineage))
        monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned_store))
        _rooted(monkeypatch, elsewhere)

        result = kb.designate_priority(
            conn, task_id, reason="fenced-target probe", board=UNREGISTERED)

        assert result["board"] == UNREGISTERED, result
        assert result["task_id"] == task_id
        row = kb.priority_designation(conn, task_id)
        assert row is not None and not row["revoked_at"], row
        assert int(conn.execute("SELECT priority FROM tasks WHERE id = ?",
                                (task_id,)).fetchone()["priority"]) == result["priority"]
    finally:
        conn.close()
        pinned_conn.close()


def test_designate_subject_is_the_connection_not_the_named_board(home, tmp_path, monkeypatch):
    """The pre-check's SUBJECT, both ways: the fenced connection refuses, the other one does not.

    The named board is foreign and unregistered in both calls, so only the connection can be
    deciding the verdict.
    """
    lineage = tmp_path / "lineage"
    elsewhere = tmp_path / "elsewhere"
    _, own_conn, _ = _board_and_card(monkeypatch, lineage, DESIGNATE_OWN)
    _, other_conn, _ = _board_and_card(monkeypatch, elsewhere, DESIGNATE_ELSEWHERE)
    try:
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(lineage))
        _rooted(monkeypatch, elsewhere)
        # The lineage's own store is fenced, so the named board cannot be used to escape it.
        with pytest.raises(PermissionError):
            kb._assert_not_delegated_child_write(own_conn, board=UNREGISTERED)
        # A store outside the fence is the caller's to write, whatever foreign board it names.
        kb._assert_not_delegated_child_write(other_conn, board=UNREGISTERED)
    finally:
        own_conn.close()
        other_conn.close()


def test_designate_from_a_delegated_child_on_its_own_board_is_still_refused(home, tmp_path,
                                                                            monkeypatch):
    """GUARD (marker arm): naming a paper board does not buy a delegated child its own store back."""
    _, conn, task_id = _board_and_card(monkeypatch, tmp_path / "lineage", DESIGNATE_OWN)
    try:
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(tmp_path / "lineage"))
        with pytest.raises(PermissionError) as raised:
            kb.designate_priority(conn, task_id, reason="self-service", board=UNREGISTERED)
        assert "child contexts cannot mutate" in str(raised.value)
        assert kb.priority_designation(conn, task_id) is None
    finally:
        conn.close()


def test_designate_from_a_worker_descendant_is_still_refused(home, tmp_path, monkeypatch):
    """GUARD (provenance arm): ``env -u`` does not clear this one — ancestry alone refuses.

    The child process below carries no marker and no pin beyond the dispatcher's; its parent is
    the ``worker_pid`` of a LIVE claim on the board, which is the only thing left to judge it by.
    """
    task_id = _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                          fingerprint=_fingerprint_of_this_process())
    script = _write(tmp_path / "designate.py", DESIGNATE_PROBE_SRC)
    res = _run(script, home, BOARD, task_id)
    assert res.returncode == 7, f"designation was ADMITTED from a worker descendant: {res.stdout}{res.stderr}"
    assert "descends from a LIVE dispatched worker" in res.stdout, res.stdout + res.stderr


def test_designate_from_a_top_level_process_is_unaffected(home, tmp_path, monkeypatch):
    """CONTROL: no marker, no claim — the ordinary caller still designates."""
    _, conn, task_id = _board_and_card(monkeypatch, tmp_path / "top", DESIGNATE_OWN)
    try:
        result = kb.designate_priority(conn, task_id, reason="ordinary", board=DESIGNATE_OWN)
        assert result["board"] == DESIGNATE_OWN
        assert kb.priority_designation(conn, task_id) is not None
    finally:
        conn.close()


