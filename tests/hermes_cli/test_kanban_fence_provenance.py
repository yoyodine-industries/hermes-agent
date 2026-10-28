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


# --- A NAMED board that resolves to nothing has NO fence verdict (card t_ecd51c9c) ---------------
#
# THREE OUTCOMES, NOT TWO (ruling t_ecd51c9c). The fence may allow or deny the board the caller
# NAMED; it may never SUBSTITUTE a different one. Before this fix a name that resolved to no board
# was judged against the AMBIENT/pinned board, so `hermes kanban --board <unregistered> create`
# from a worker-descended process came back as the fence refusal and MASKED the CLI's own
# `board '<x>' does not exist`. Measured on the sdlc birth node: run 20261008-0844 (gateway caller)
# reached the honest error and BIRTHED its item; run 20261008-0958 (worker caller) got the fence
# message instead; cron 16b8c8db4ed7 carried failure_streak=10.
#
# Outcome 3 must also be INERT: the unresolved name mints no store and moves no row on the board
# the process IS pinned to. That is asserted, not assumed — a hole here would be worse than the
# over-refusal it replaces.

GHOST = "fence-probe-ghost"  # well formed, resolves to no registered board


def _write_audit(root: Path, slug: str) -> tuple[int, bool]:
    """(row count of the named board's store, whether that store exists at all)."""
    db = _board_db(root, slug)
    if not db.exists():
        return 0, False
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]), True
    finally:
        conn.close()


def test_named_unregistered_board_reports_the_board_error_from_a_worker_descendant(home, tmp_path):
    """DEFECT: the provenance-arm shell (marker stripped) must get the honest board error."""
    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    script = _write(tmp_path / "cli.py", PROBE_SRC)
    before, _ = _write_audit(home, BOARD)
    res = _run(script, home, BOARD, "--board", GHOST, "create", "x")
    assert res.returncode == 1, res.stdout + res.stderr
    assert f"board {GHOST!r} does not exist" in res.stderr, res.stdout + res.stderr
    assert "child contexts cannot mutate" not in res.stderr
    assert "descends from a LIVE dispatched worker" not in res.stderr
    # HARD REQUIREMENT: the unresolved name is INERT — no store minted, no row moved.
    after, pinned_exists = _write_audit(home, BOARD)
    assert pinned_exists, "the pinned board's own store vanished"
    assert _write_audit(home, GHOST)[1] is False, "the unresolved --board MINTED a store"
    assert after == before, f"the pinned board's row count moved: {before} -> {after}"


def test_named_unregistered_board_reports_the_board_error_with_the_marker_set(home, tmp_path):
    """DEFECT: the cooperative marker (value ``1``) must not refuse a board that does not exist."""
    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    script = _write(tmp_path / "cli.py", PROBE_SRC)
    res = _run(script, home, BOARD, "--board", GHOST, "create", "x",
               HERMES_DELEGATED_CHILD_CONTEXT="1")
    assert res.returncode == 1, res.stdout + res.stderr
    assert f"board {GHOST!r} does not exist" in res.stderr, res.stdout + res.stderr
    assert "child contexts cannot mutate" not in res.stderr


def test_named_unregistered_board_reports_the_board_error_with_a_path_marker(home, tmp_path):
    """DEFECT: the marker carrying the fenced ROOT must not refuse a board that does not exist."""
    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    script = _write(tmp_path / "cli.py", PROBE_SRC)
    res = _run(script, home, BOARD, "--board", GHOST, "create", "x",
               HERMES_DELEGATED_CHILD_CONTEXT=str(home))
    assert res.returncode == 1, res.stdout + res.stderr
    assert f"board {GHOST!r} does not exist" in res.stderr, res.stdout + res.stderr
    assert "child contexts cannot mutate" not in res.stderr


def test_named_unregistered_board_is_unchanged_from_a_top_level_process(home, tmp_path):
    """CONTROL: with no claim and no marker the behaviour is the honest error, as before."""
    _make_foreign_board(home, OTHER_BOARD)
    script = _write(tmp_path / "cli.py", PROBE_SRC)
    res = _run(script, home, BOARD, "--board", GHOST, "create", "x", pin_board=False)
    assert res.returncode == 1, res.stdout + res.stderr
    assert f"board {GHOST!r} does not exist" in res.stderr, res.stdout + res.stderr


def test_named_own_board_is_still_refused_by_the_marker_arm(home, tmp_path):
    """GUARD (unchanged trust boundary): naming the board the lineage claims is still refused."""
    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    script = _write(tmp_path / "cli.py", PROBE_SRC)
    res = _run(script, home, BOARD, "--board", BOARD, "create", "x",
               HERMES_DELEGATED_CHILD_CONTEXT="1")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "child contexts cannot mutate" in res.stderr


def test_named_own_board_is_still_refused_by_the_provenance_arm(home, tmp_path):
    """GUARD (unchanged trust boundary): env-stripped shell, ancestor holds the claim -> refused."""
    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    script = _write(tmp_path / "cli.py", PROBE_SRC)
    res = _run(script, home, BOARD, "--board", BOARD, "create", "x")
    assert res.returncode == 1, f"the claimed board was ADMITTED: {res.stdout}{res.stderr}"
    assert "child contexts cannot mutate" in res.stderr


def test_unregistered_name_is_not_a_pin_violation(home, tmp_path):
    """THE PIN ARM (ruling t_ecd51c9c): it judges only names that RESOLVE.

    A READ never reaches the fast-fail, so the pin layer decides alone here — which is exactly
    the layer that used to refuse one step EARLIER than the store resolution and re-mask the
    honest error. A registered foreign board must still be refused by the same layer: that
    boundary protects data on boards that EXIST, and it must not move.
    """
    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                fingerprint=_fingerprint_of_this_process())
    _make_foreign_board(home, OTHER_BOARD)
    script = _write(tmp_path / "cli.py", PROBE_SRC)

    unresolved = _run(script, home, BOARD, "--board", GHOST, "show", "t_deadbeef",
                      HERMES_DELEGATED_CHILD_CONTEXT="1")
    assert unresolved.returncode == 1, unresolved.stdout + unresolved.stderr
    assert f"board {GHOST!r} does not exist" in unresolved.stderr, unresolved.stderr
    assert "pinned to" not in unresolved.stderr, (
        "an unresolved name is not a pin violation — the pin re-masked the honest error: "
        f"{unresolved.stderr}"
    )

    foreign = _run(script, home, BOARD, "--board", OTHER_BOARD, "show", "t_deadbeef",
                   HERMES_DELEGATED_CHILD_CONTEXT="1")
    assert foreign.returncode == 1, foreign.stdout + foreign.stderr
    assert "pinned to" in foreign.stderr, (
        "the pin stopped speaking for a board that DOES exist — the isolation moved: "
        f"{foreign.stderr}"
    )



# --- EVERY durable write door answers the ONE verdict (card t_e88054a6) -------------------------
#
# DEFECT: the arms were answerable SEPARATELY. ``kanban_path_is_fenced`` (the two cooperative
# arms) was consulted at every durable seam; ``ancestor_owns_live_kanban_claim`` (provenance) only
# in the CLI's ``--board`` fast-fail. A shell that stripped the marker was therefore refused at
# the CLI and ADMITTED at the durable seam behind it. Each door below drives the REAL mutator from
# such a shell (parent == the live claim's ``worker_pid``, no marker): at the pre-fix base the
# write LANDS, with the fix it is REFUSED and the durable state is untouched.
#
# One table, because a door that answers differently is the defect. Adding a door here is how the
# next seam gets covered instead of re-deriving which arms it happens to consult.

DURABLE_DOOR_PROBE_SRC = """\
import sys

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

door, task_id, board = sys.argv[1], sys.argv[2], sys.argv[3]
conn = kbc.connect()
try:
    if door == "write_txn":
        # The cheapest real ``write_txn`` caller a read verb runs (kanban list's mini-dispatch).
        kb.recompute_ready(conn)
    elif door == "board_metadata":
        kb.write_board_metadata(board, name="renamed-by-a-descendant")
    elif door == "board_removal":
        kb.remove_board(board, archive=False)
    elif door == "designate":
        kb.designate_priority(conn, task_id, reason="provenance-door probe", board=board)
    elif door == "revoke":
        kb.revoke_priority_designation(conn, task_id, reason="provenance-door probe")
    elif door == "arm_guard":
        kb.arm_priority_tranche_guard(board)
    elif door == "disarm_guard":
        kb.disarm_priority_tranche_guard(board)
    else:
        raise SystemExit("unknown door: " + door)
except PermissionError as exc:
    print("REFUSED:", exc)
    raise SystemExit(7)
print("ADMITTED")
raise SystemExit(0)
"""

_TRANCHE_TRIGGER_SQL = (
    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'tasks_priority_tranche_guard%'"
)


def _door_state(root: Path, slug: str, task_id: str, door: str):
    """The piece of durable state *door* would move, read the way its own reader reads it."""
    if door == "board_metadata":
        return kb.read_board_metadata(slug).get("name")
    if door == "board_removal":
        return _board_db(root, slug).parent.exists()
    conn = kbc.connect(db_path=_board_db(root, slug))
    try:
        if door == "write_txn":
            return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        if door == "designate":
            row = kb.priority_designation(conn, task_id)
            return row["designated_at"] if row else None
        if door == "revoke":
            row = kb.priority_designation(conn, task_id)
            return row["revoked_at"] if row else "no-designation"
        return sorted(r[0] for r in conn.execute(_TRANCHE_TRIGGER_SQL).fetchall())
    finally:
        conn.close()


def _prepare_door(door: str, root: Path, slug: str, task_id: str) -> None:
    """The state a door needs to exist before its write can be observed moving it."""
    if door == "disarm_guard":
        kb.arm_priority_tranche_guard(slug)
    elif door == "revoke":
        conn = kbc.connect(db_path=_board_db(root, slug))
        try:
            kb.designate_priority(conn, task_id, reason="the designation a revoke would end",
                                  board=slug)
        finally:
            conn.close()


@pytest.mark.parametrize("door", [
    "write_txn", "board_metadata", "board_removal", "designate", "revoke",
    "arm_guard", "disarm_guard",
])
def test_every_durable_write_door_answers_the_provenance_arm(door, home, tmp_path):
    """Each door: REFUSED from the marker-stripped descendant, and nothing moved."""
    task_id = _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                          fingerprint=_fingerprint_of_this_process())
    _prepare_door(door, home, BOARD, task_id)
    before = _door_state(home, BOARD, task_id, door)
    script = _write(tmp_path / "door.py", DURABLE_DOOR_PROBE_SRC)

    res = _run(script, home, BOARD, door, task_id, BOARD)

    assert res.returncode == 7, (
        f"{door} was ADMITTED from a shell descended from the live claim of this board: "
        f"{res.stdout}{res.stderr}"
    )
    assert "descends from a LIVE dispatched worker" in res.stdout, res.stdout
    assert _door_state(home, BOARD, task_id, door) == before, f"{door} moved durable state"


def test_the_durable_doors_still_admit_a_top_level_caller(home, tmp_path):
    """CONTROL: the same doors are not blanket-refused — no live-claim ancestry, no fence.

    Without this the table above would pass against a ``raise PermissionError`` on every seam.
    """
    task_id = _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid() + 400000,
                                          fingerprint=None)
    script = _write(tmp_path / "door.py", DURABLE_DOOR_PROBE_SRC)
    for door in ("write_txn", "board_metadata", "designate", "arm_guard", "disarm_guard"):
        res = _run(script, home, BOARD, door, task_id, BOARD)
        assert res.returncode == 0, f"{door}: {res.stdout}{res.stderr}"


def test_the_subject_is_the_connection_not_the_named_board(home):
    """D1: with a connection in hand, a NAME never becomes the subject of the verdict.

    ``kanban_db_path(board=<name>)`` answers the ENV-PINNED store whenever a worker's pin is
    authoritative, so keying a door off the name judged a store the write never touched
    (card t_57e33100). The subject is the file ``write_txn`` will fence.
    """
    from hermes_cli.kanban_db_fence import fence_subject

    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid() + 400000, fingerprint=None)
    _make_foreign_board(home, OTHER_BOARD)
    conn = kbc.connect(db_path=_board_db(home, BOARD))
    try:
        assert fence_subject(conn=conn, board=OTHER_BOARD) == _board_db(home, BOARD).resolve()
    finally:
        conn.close()


def test_a_name_that_is_no_board_is_no_subject(home):
    """D2: a malformed or unregistered name resolves to NO subject — never to another board."""
    from hermes_cli.kanban_db_fence import fence_subject

    _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid() + 400000, fingerprint=None)
    for name in (GHOST, "..", "a/b/c", ""):
        assert fence_subject(board=name) is None, f"{name!r} became a fence subject"


# --- the two conn doors are judged by the CONNECTION (ruling t_e88054a6 D3) ---------------------

def test_the_conn_doors_are_judged_by_the_connection_not_the_pin(home, tmp_path, monkeypatch):
    """The write lands on the CONNECTION's store — a name must not move the verdict off it.

    ``kanban_db_path(board=<name>)`` answers the ENV-PINNED store whenever a worker's pin is
    authoritative, so keyed off the name this door refused a caller whose write landed on a store
    the fence does not hold: an over-refusal of the caller's own board, and one that flips when
    the pin changes. The verdict is about the file ``write_txn`` fences. RED at the pre-fix base:
    the write is refused.
    """
    from agent import delegation_context as dc

    task_id = _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid() + 400000,
                                          fingerprint=None)
    elsewhere = tmp_path / "elsewhere" / "kanban.db"
    conn = kbc.connect(db_path=elsewhere)
    try:
        own_task = kb.create_task(conn, title="a store outside the fence")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(_board_db(home, BOARD)))
        monkeypatch.setenv(dc.DELEGATED_CHILD_ENV_MARKER, str(_board_db(home, BOARD).parent))
        # The disagreement the old door suffered from: the name's store is fenced, this one is not.
        assert dc.kanban_path_is_fenced(_board_db(home, BOARD))
        assert not dc.kanban_path_is_fenced(elsewhere)

        kb.designate_priority(conn, own_task, reason="a store the fence does not hold", board=BOARD)
        assert kb.priority_designation(conn, own_task) is not None
        assert kb.priority_designation(conn, task_id) is None
    finally:
        conn.close()


def test_the_two_conn_doors_refuse_a_delegated_child_on_its_own_board(home, tmp_path):
    """D5: a delegated child may not move its OWN board's designation through either door.

    Asserted on BOTH arms — the cooperative ContextVar (in-process) and PROVENANCE (a shell
    descended from the live claim) — because the defect was that the arms answered separately.
    """
    from agent import delegation_context as dc

    task_id = _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid() + 400000,
                                          fingerprint=None)
    conn = kbc.connect()
    try:
        with dc.delegated_child_context():
            with pytest.raises(PermissionError, match="child contexts cannot mutate"):
                kb.designate_priority(conn, task_id, reason="own board, marker arm", board=BOARD)
            with pytest.raises(PermissionError, match="child contexts cannot mutate"):
                kb.revoke_priority_designation(conn, task_id, reason="own board, marker arm")
    finally:
        conn.close()

    claim_task = _make_board_with_live_claim(home, slug=BOARD, worker_pid=os.getpid(),
                                             fingerprint=_fingerprint_of_this_process())
    script = _write(tmp_path / "door.py", DURABLE_DOOR_PROBE_SRC)
    for door in ("designate", "revoke"):
        res = _run(script, home, BOARD, door, claim_task, BOARD)
        assert res.returncode == 7, f"{door} admitted on the provenance arm: {res.stdout}{res.stderr}"
        assert "descends from a LIVE dispatched worker" in res.stdout, res.stdout
