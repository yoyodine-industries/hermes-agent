"""The consumer-side CONTENT skew gate on the dispatcher tick, and the ACTING half of it.

Ruling ``t_8fed34c8`` (platform-stl, Design Authority, 2026-10-10), R2 + R3 -- plus the streak /
re-open leg of ruling ``t_eaadb60e`` as settled by card ``t_0b2118cb`` (leg A).

**R2 -- the signal must be CONTENT, not the ref.** ``gateway.code_skew.detect_code_skew()``
fingerprints only ``.git/HEAD`` -> ref -> sha and never reads the working tree. The measured
incident (gateway pid 56908, still serving at 17:23 on 2026-10-10) moved the WORKING TREE with
HEAD unchanged -- an interrupted merge leaves HEAD at the pre-merge commit and the recovery
restores paths "to HEAD" -- so the ref-only signal returned ``None`` while 120 paths differed
from HEAD and every dispatcher spawn died ``cannot import name 'ADVISORY_SKILLS_ENV'``. The test
that proves R2 is therefore: **a working-tree edit with HEAD unchanged must be DETECTED**, and it
must be detected by the gate that ships, not by a probe the gate does not make.

**R3 -- the consumer is the tick.** On proven skew the tick spawns nothing and records a NON-GREEN
status on a surface a human reads. The check is O(tick), never O(cards).

**Leg A -- the streak and the re-open leg** (cards ``t_eaadb60e`` / ``t_0b2118cb``):

* the act fires on the THIRD consecutive refusal of one signature, not the first (DEC-4);
* the signature is the FROZEN PROCESS -- ``boot_fingerprint:boot_pid`` -- never the disk (DEC-1):
  bytes landing under a frozen process change the disk side without ending the stall, so a
  disk-sensitive identity would reset the streak on every landing;
* the carrier's key is occurrence-ordinal (``tree-skew:<sig>:<n>``) and the lookup is status-aware:
  a LIVE carrier absorbs the repeat, **a DONE carrier never absorbs a new occurrence** (DEC-2).
  That last rule is the defect that cost 1h38m on 2026-10-11 -- three guard cards were closed on a
  correct measurement, the same gateway pid kept refusing, and the closed carrier's key swallowed
  every refusal, so the board carried NO card until a human bounced the gateway;
* the card is filed on the OPS board, at the rung the board's own priority ladder returns (DEC-3).

The cases are named A1..A10 after the acceptance plan in
``/opt/hermes_sandbox/platform/work/platform-stl/t_0b2118cb-tree-skew-streak/acceptance/TESTPLAN.md``.

Everything here runs on a SCRATCH surface: the ticking board and the ops board are both throwaway
DBs under ``tmp_path``, ``TICK_YIELD_ACTOR_BOARD`` is pointed at the scratch ops board, and the
repo's hermetic fixture (``tests/conftest.py::_hermetic_environment``) strips the dispatcher's
``HERMES_KANBAN_*`` pins -- so the fleet's ``migrations`` board is unreachable from this module.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from gateway import code_skew
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_priority_policy as pol
from hermes_cli import tree_fingerprint as tf


#: The scratch board the acting card rides in these tests. ``migrations`` is the production value
#: of ``TICK_YIELD_ACTOR_BOARD``; a test must never reach it.
OPS_BOARD = "scratch-ops"

#: The threshold as the pre-change CONTROL run sees it: on a tree without leg A the constant does
#: not exist, and a collection-time ``AttributeError`` would prove nothing about the behaviour under
#: test. Reading it tolerantly keeps every failure below behavioural.
THRESHOLD = getattr(kbd, "TICK_YIELD_ACTOR_THRESHOLD", 3)


# ---------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_boot_state(monkeypatch):
    """Every test starts with NO boot record, an empty memo, and an inert ref half.

    The boot record is process-global by design (one record, recorded once), so a test must never
    inherit another test's -- and the ref half is disarmed unless a test arms it, so a content-only
    finding cannot be mistaken for a ref finding.
    """
    tf.reset_boot()
    tf._content_memo.clear()
    monkeypatch.setattr(code_skew, "_boot_fingerprint", None)
    yield
    tf.reset_boot()
    tf._content_memo.clear()


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A sandbox pair: the TICKING board, plus the scratch OPS board the actor must ride.

    The status record must land in the sandbox home, never the live one -- and the ops board is a
    registered scratch board (``dispatch=False``: the dispatcher never serves it), so the acting
    card is filed on a real, separate DB rather than on the board that merely ticked.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr(kbd, "TICK_YIELD_ACTOR_BOARD", OPS_BOARD, raising=False)
    kb.create_board(OPS_BOARD, dispatch=False)
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A small fake source tree that ``hermes_cli.tree_fingerprint`` is pointed at."""
    root = tmp_path / "checkout"
    for package, module in (
        ("hermes_cli", "kanban_db.py"),
        ("agent", "skill_commands.py"),
        ("gateway", "run.py"),
        ("tools", "kanban_tools.py"),
    ):
        directory = root / package
        directory.mkdir(parents=True)
        (directory / module).write_text(f"# {package}/{module}\nVALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(tf, "default_root", lambda: root)
    return root


def _edit(root, package="hermes_cli", module="kanban_db.py", body="VALUE = 2\n"):
    """Change the WORKING TREE only -- no ref moves, exactly the incident's shape."""
    (root / package / module).write_text(f"# {package}/{module}\n{body}", encoding="utf-8")


def _arm_ref(monkeypatch, *, boot: str, disk: str) -> None:
    """Point the ref half of the signal at fixed revisions (the fast path, not the decision)."""
    monkeypatch.setattr(code_skew, "_boot_fingerprint", f"git:refs/heads/main:{boot}")
    monkeypatch.setattr(code_skew, "_fingerprint", lambda: f"git:refs/heads/main:{disk}")


def _tick(conn, **kwargs):
    """One dispatcher tick -- ``dispatch_once`` is the one entry the gate sits in."""
    return kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242, **kwargs)


def _rows(conn):
    # ``rowid`` is insertion order. ``created_at`` is epoch SECONDS, so two carriers filed in the
    # same second tie and the ``id`` tiebreak is arbitrary -- never order a multi-row assertion by it.
    return conn.execute(
        "SELECT id, assignee, priority, status, body, idempotency_key FROM tasks "
        "WHERE idempotency_key LIKE 'tree-skew:%' ORDER BY rowid"
    ).fetchall()


def _actor_rows():
    """The acting cards on the OPS board -- the board the gate must file them on."""
    conn = kbc.connect(board=OPS_BOARD)
    try:
        return _rows(conn)
    finally:
        conn.close()


def _ticking_rows(conn):
    """Anything that leaked onto the board that merely TICKED."""
    return _rows(conn)


def _close_card(task_id: str) -> None:
    """Close a carrier the way a worker does -- it leaves the LIVE set."""
    conn = kbc.connect(board=OPS_BOARD)
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    finally:
        conn.close()


def _status_record():
    path = kbd.tick_yield_status_path()
    return path, (json.loads(path.read_text(encoding="utf-8")) if path.exists() else None)


# ---------------------------------------------------------------------------------------------
# (a) the load-bearing one: a working-tree edit with HEAD unchanged
# ---------------------------------------------------------------------------------------------


def test_a_working_tree_edit_with_head_unchanged_is_detected(board, tree, monkeypatch):
    """R2's acceptance test. A ref-only guard fails this by construction; the shipped gate passes.

    Proves both halves in one place: the ref-only probe the incident defeated reports NO skew,
    and the tick still refuses.
    """
    conn = board
    tf.record_boot()
    _edit(tree)
    # HEAD did not move (the interrupted merge / restore-to-HEAD shape), so the fast path is blind.
    _arm_ref(monkeypatch, boot="a" * 40, disk="a" * 40)
    assert code_skew.detect_code_skew() is None, "the ref-only signal must be blind here"
    assert tf.detect_skew() is not None, "the content signal must see the working-tree edit"

    result = _tick(conn)

    assert result.tick_yielded.startswith(kbd.TICK_YIELD_PREFIX)
    assert result.spawned == [], "a refused tick spawns nothing"
    assert result.promoted == 0 and result.reclaimed == 0, "a refused tick mutates no board state"


# ---------------------------------------------------------------------------------------------
# (b) HEAD-only drift is still caught (the ref half rides along as a fast path)
# ---------------------------------------------------------------------------------------------


def test_head_only_drift_is_detected(board, tree, monkeypatch):
    conn = board
    tf.record_boot()
    # The files are byte-identical; only the revision moved (a checkout of an identical tree).
    _arm_ref(monkeypatch, boot="a" * 40, disk="b" * 40)
    assert tf.detect_skew() is None, "content alone cannot see a pure ref move"

    result = _tick(conn)

    assert result.tick_yielded.startswith(kbd.TICK_YIELD_PREFIX)
    assert "b" * 10 in result.tick_yielded, result.tick_yielded


# ---------------------------------------------------------------------------------------------
# (c) no false positive on a clean tree
# ---------------------------------------------------------------------------------------------


def test_a_clean_tree_is_not_refused(board, tree):
    conn = board
    tf.record_boot()

    result = _tick(conn)

    assert result.tick_yielded == ""
    assert _actor_rows() == []
    path, record = _status_record()
    assert record is None, f"a healthy tick must leave no non-green marker ({path})"


def test_a_process_that_never_recorded_a_boot_fingerprint_fails_open(board, tree):
    """A one-shot CLI dispatch imports fresh from disk: it cannot be stale, so it never refuses."""
    conn = board
    result = _tick(conn)
    assert result.tick_yielded == ""


def test_an_unreadable_tree_fails_open(board, tree, monkeypatch):
    """An IO error is 'cannot certify identity', never proof of skew."""
    conn = board
    tf.record_boot()
    monkeypatch.setattr(tf, "content_fingerprint", lambda root=None: None)
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: None)
    result = _tick(conn)
    assert result.tick_yielded == ""


def test_a_byte_identical_rewrite_reads_clean(board, tree):
    """The contract is CONTENT, not mtime: re-writing the same bytes (a `git checkout`, a re-applied
    override, the restore after a break) is not drift and must not refuse a healthy tick."""
    conn = board
    tf.record_boot()
    boot_fingerprint = tf.boot_record()["fingerprint"]

    _edit(tree, body="VALUE = 1\n")  # byte-identical to what the fixture wrote

    assert tf.content_fingerprint() == boot_fingerprint
    assert tf.detect_skew() is None
    result = _tick(conn)
    assert result.tick_yielded == ""


# ---------------------------------------------------------------------------------------------
# A1/A2 -- the first refusals of a streak file NOTHING
# ---------------------------------------------------------------------------------------------


def test_a1_one_refused_tick_records_the_streak_and_files_nothing(board, tree):
    """A1. One refusal is churn, not a stall: the record carries the streak, the board carries
    no card."""
    conn = board
    tf.record_boot()
    _edit(tree)

    result = _tick(conn)
    assert result.tick_yielded.startswith(kbd.TICK_YIELD_PREFIX)

    _path, record = _status_record()
    assert record is not None, "a refusing tick must leave a record a human reads"
    assert record["version"] == 2, "the record must carry the streak fields (version 2)"
    assert record["streak"] == 1
    assert record["first_tick"] == record["last_tick"], "one tick is its own window"
    assert record["sig"] == f"{record['boot_fingerprint']}:{record['pid']}"
    assert record["threshold"] == THRESHOLD
    assert _actor_rows() == [], "the FIRST refusal must not file a card"
    assert _ticking_rows(conn) == []


def test_a2_two_refused_ticks_climb_the_streak_and_still_file_nothing(board, tree):
    """A2. The streak is the guard's own refusal count, and two is still below the act."""
    conn = board
    tf.record_boot()
    _edit(tree)

    _tick(conn)
    _tick(conn)

    _path, record = _status_record()
    assert record["streak"] == 2
    assert record["last_tick"] >= record["first_tick"]
    assert _actor_rows() == []


# ---------------------------------------------------------------------------------------------
# A3/A4 -- the third refusal of one signature acts, exactly once
# ---------------------------------------------------------------------------------------------


def test_a3_the_third_refused_tick_files_exactly_one_actor_on_the_ops_board(board, tree):
    """A3. The act fires AT the threshold, on the OPS board, addressed to the ops seat, keyed
    ``tree-skew:<signature>:1``.

    ``priority`` is asked of the board's own ladder. This scratch board carries no
    ``priority_policy``, so the ladder answers ``None`` and the card keeps the kernel default --
    and the body SAYS the ladder was asked and answered nothing, rather than inventing a band.
    """
    conn = board
    tf.record_boot()
    _edit(tree)
    boot = tf.boot_record()

    _tick(conn)
    _tick(conn)
    assert _actor_rows() == [], "nothing may be filed before the threshold"

    _tick(conn)

    rows = _actor_rows()
    assert len(rows) == 1, f"the third refusal files exactly ONE card, got {len(rows)}"
    row = rows[0]
    assert row["assignee"] == kbd.TICK_YIELD_ACTOR_ASSIGNEE == "default"
    assert row["idempotency_key"] == (
        f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}{boot['fingerprint']}:{boot['pid']}:1"
    )
    assert row["priority"] == 0, "no policy on the board: the kernel default, not an invented band"
    assert "no priority policy on board" in row["body"], row["body"]
    assert _ticking_rows(conn) == [], "the card must NOT land on the board that ticked"
    assert _status_record()[1]["streak"] == 3


def test_a3_the_priority_is_asked_of_the_ladder_and_used(board, tree, monkeypatch):
    """A3 (ladder half). DEC-3: priority is ASKED of the board's policy, never typed."""
    conn = board
    tf.record_boot()
    _edit(tree)
    calls: list = []

    def _ladder(requested, *, assignee="", board="", title="", body="", spec=None):
        calls.append({
            "requested": requested, "assignee": assignee, "board": board, "title": title,
            "spec": spec,
        })
        return pol.Verdict(7, {"band": "test"})

    monkeypatch.setattr(pol, "priority_for_create", _ladder)
    for _ in range(THRESHOLD):
        _tick(conn)

    rows = _actor_rows()
    assert len(rows) == 1
    assert rows[0]["priority"] == 7, "the card is born where the ladder placed it"
    assert calls, "the ladder must be asked"
    assert calls[0]["assignee"] == "default"
    assert calls[0]["board"] == OPS_BOARD, "the ladder is asked about the board the card rides"


def test_a4_n_more_ticks_keep_exactly_one_card_and_the_streak_climbing(board, tree):
    """A4. A LIVE carrier absorbs every further refusal: no second card, no comment-twin."""
    conn = board
    tf.record_boot()
    _edit(tree)

    for _ in range(THRESHOLD):
        _tick(conn)
    assert len(_actor_rows()) == 1

    for expected in (4, 5, 6):
        _tick(conn)
        assert len(_actor_rows()) == 1, "one stall is one card, however long it runs"
        assert _status_record()[1]["streak"] == expected, "the streak keeps climbing"

    assert _actor_rows()[0]["idempotency_key"].endswith(":1")


# ---------------------------------------------------------------------------------------------
# A5 -- the re-open leg: a DONE carrier never absorbs a new occurrence
# ---------------------------------------------------------------------------------------------


def test_a5_a_closed_carrier_is_re_opened_at_the_next_ordinal(board, tree):
    """A5. THE defect this card exists for.

    On 2026-10-11 three guard cards were closed on a correct measurement, the stall continued under
    the same gateway pid, and the closed carrier's key swallowed every refusal -- the board carried
    NO card until a human bounced the gateway at 04:45. A closed carrier must therefore never
    absorb the recurrence: the next streak files a NEW card at the next ordinal.
    """
    conn = board
    tf.record_boot()
    _edit(tree)

    for _ in range(THRESHOLD):
        _tick(conn)
    first = _actor_rows()
    assert len(first) == 1, f"the first streak files one card, got {len(first)}"
    _close_card(first[0]["id"])

    for _ in range(THRESHOLD):
        _tick(conn)

    rows = _actor_rows()
    assert len(rows) == 2, f"the recurrence must file a SECOND card, got {len(rows)}: {rows}"
    assert rows[0]["id"] != rows[1]["id"], "a new occurrence is a new card, not a comment"
    closed = [r for r in rows if r["status"] == "done"]
    live = [r for r in rows if r["status"] not in ("done", "archived")]
    assert len(closed) == 1 and closed[0]["idempotency_key"].endswith(":1"), rows
    assert len(live) == 1, f"exactly one carrier must be live, got {rows}"
    assert live[0]["idempotency_key"].endswith(":2"), live[0]["idempotency_key"]

    # ...and THAT carrier is live again, so a further refusal is absorbed once more.
    _tick(conn)
    assert len(_actor_rows()) == 2


# ---------------------------------------------------------------------------------------------
# A6 -- the signature is the frozen process, and each process runs its own streak
# ---------------------------------------------------------------------------------------------


def test_a6_each_frozen_process_runs_its_own_streak(board, tree, monkeypatch):
    """A6/DEC-1. ``boot_fingerprint:boot_pid`` is the identity: a process that boots AFTER a reload
    and is AGAIN skewed is a genuinely new stall, with its own streak and its own card."""
    conn = board
    tf.record_boot()
    _edit(tree)
    boot = tf.boot_record()

    for _ in range(THRESHOLD):
        _tick(conn)
    assert len(_actor_rows()) == 1

    monkeypatch.setattr(tf, "_boot_record", {**boot, "pid": int(boot["pid"]) + 1})

    _tick(conn)
    assert len(_actor_rows()) == 1, "the new process is only on its FIRST refusal"
    record = _status_record()[1]
    assert record["streak"] == 1, "a new signature starts its own streak"
    assert record["pid"] == int(boot["pid"]) + 1
    _tick(conn)
    assert len(_actor_rows()) == 1

    _tick(conn)
    rows = _actor_rows()
    assert len(rows) == 2, "its OWN third refusal files its own card"
    prefix = kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX
    assert {r["idempotency_key"] for r in rows} == {
        f"{prefix}{boot['fingerprint']}:{boot['pid']}:1",
        f"{prefix}{boot['fingerprint']}:{int(boot['pid']) + 1}:1",
    }, "each frozen process keys its OWN stall, from its own first ordinal"


# ---------------------------------------------------------------------------------------------
# A7 -- a healthy tick retires the record, and the streak is CONSECUTIVE
# ---------------------------------------------------------------------------------------------


def test_a7_a_healthy_tick_retires_the_marker_and_the_streak_restarts(board, tree, monkeypatch):
    """A7. ``_clear_tick_yield_status`` retires the record on the first tick that serves normally,
    which is what makes ``streak`` consecutive by construction -- never cumulative."""
    conn = board
    tf.record_boot()
    _edit(tree)
    skewed = dict(tf.boot_record())

    _tick(conn)
    assert _status_record()[1]["streak"] == 1

    # Rewind to the recorded content (the reload door's effect, without a bounce): the tick serves.
    tf.reset_boot()
    tf.record_boot()
    _tick(conn)
    assert _status_record()[1] is None, "a healthy tick retires its own marker"

    # The SAME signature skews again: the counter must start over, not resume at 2.
    monkeypatch.setattr(tf, "_boot_record", skewed)
    _edit(tree, body="VALUE = 3\n")
    _tick(conn)
    record = _status_record()[1]
    assert record is not None and record["streak"] == 1, "consecutive, never cumulative"
    assert record["sig"] == f"{skewed['fingerprint']}:{skewed['pid']}"


# ---------------------------------------------------------------------------------------------
# A8 -- the card body carries the evidence a human acts on
# ---------------------------------------------------------------------------------------------


def test_a8_the_card_body_carries_the_streak_the_measurement_and_the_remedy(board, tree, tmp_path):
    """A8. Deterministic evidence, no model: what was measured, when, and what closes it."""
    conn = board
    tf.record_boot()
    _edit(tree)
    boot = tf.boot_record()
    # A real spawn line for THIS board, in the tail the body reads.
    log = tmp_path / "logs" / "gateway.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "kanban dispatcher [default]: spawned=0 reclaimed=0\n"
        "unrelated line\n"
        "kanban dispatcher [default]: spawned=2 reclaimed=0 promoted=1\n",
        encoding="utf-8",
    )

    for _ in range(THRESHOLD):
        _tick(conn, board="default")
    body = _actor_rows()[0]["body"]
    record = _status_record()[1]

    assert f"consecutive refused ticks: **{THRESHOLD}**" in body
    assert re.search(r"first tick: `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}`", body), body
    assert re.search(r"last tick: `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}`", body), body
    assert f"epoch {record['first_tick']}" in body and f"epoch {record['last_tick']}" in body
    assert f"frozen process pid: {boot['pid']}" in body
    assert f"imported tree content (what the running process loaded): `{boot['fingerprint']}`" in body
    assert "on-disk checkout (what the tree holds now): `" in body
    assert (
        "- last successful spawn: `kanban dispatcher [default]: spawned=2 reclaimed=0 promoted=1`"
        in body
    ), body
    assert "## DEADLINE -- 30 minutes" in body
    assert "bounce_gateway" in body, "the remedy must be the sanctioned door"
    assert f"- card filed on: `{OPS_BOARD}`" in body


def test_a8_the_last_spawn_line_is_named_when_the_board_never_spawned(board, tree, tmp_path):
    """A named answer, never an empty string: `nothing spawned` is a different claim from
    `I could not read the log`."""
    conn = board
    tf.record_boot()
    _edit(tree)
    log = tmp_path / "logs" / "gateway.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("kanban dispatcher [some-other-board]: spawned=1\n", encoding="utf-8")

    for _ in range(THRESHOLD):
        _tick(conn, board="default")

    body = _actor_rows()[0]["body"]
    assert (
        "- last successful spawn: `(no successful spawn line for this board in the log tail)`" in body
    ), body


# ---------------------------------------------------------------------------------------------
# A9 -- an unopenable ops board is a visible fallback, never a silent loss
# ---------------------------------------------------------------------------------------------


def test_a9_an_unopenable_ops_board_files_on_the_ticking_board_and_says_so(
    board, tree, monkeypatch,
):
    """A9/DEC-3. The fallback is allowed; a SILENT fallback is not."""
    conn = board
    tf.record_boot()
    _edit(tree)
    real_connect = kbc.connect

    def _connect(db_path=None, *, board=None):
        if board == kbd.TICK_YIELD_ACTOR_BOARD:
            raise OSError("simulated: the ops board cannot be opened")
        return real_connect(db_path, board=board)

    monkeypatch.setattr(kbd._kbc, "connect", _connect)

    for _ in range(THRESHOLD):
        _tick(conn)

    rows = _ticking_rows(conn)
    assert len(rows) == 1, "the actor is never silently lost"
    assert "- card filed on: `default`" in rows[0]["body"], (
        "the body must name the board it actually landed on"
    )
    assert kbd.TICK_YIELD_ACTOR_BOARD not in rows[0]["body"]


# ---------------------------------------------------------------------------------------------
# A10 -- the last-spawn reader: a line, or a named reason
# ---------------------------------------------------------------------------------------------


def test_a10_last_spawned_line_reads_the_tail_or_says_why(tmp_path, monkeypatch):
    """A10. Three shapes, three answers -- and never an empty string."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = kbd._last_spawned_line("default")
    assert path.startswith("(gateway log unreadable:"), path

    log = tmp_path / "logs" / "gateway.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("kanban dispatcher [other]: spawned=1\n", encoding="utf-8")
    assert kbd._last_spawned_line("default") == (
        "(no successful spawn line for this board in the log tail)"
    )

    log.write_text(
        "kanban dispatcher [default]: spawned=1 reclaimed=0\n"
        "kanban dispatcher [default]: spawned=4 reclaimed=2\n"
        "kanban dispatcher [other]: spawned=9\n",
        encoding="utf-8",
    )
    assert kbd._last_spawned_line("default") == (
        "kanban dispatcher [default]: spawned=4 reclaimed=2"
    )


def test_a10_last_spawned_line_reads_only_the_tail(tmp_path, monkeypatch):
    """The read is O(tail), never O(log): a line older than the window is not the last one it sees
    -- and it does not read the whole file to find that out."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    log = tmp_path / "logs" / "gateway.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "kanban dispatcher [default]: spawned=1\n" + ("x" * 4000 + "\n"),
        encoding="utf-8",
    )

    assert kbd._last_spawned_line("default", limit_bytes=200) == (
        "(no successful spawn line for this board in the log tail)"
    )


# ---------------------------------------------------------------------------------------------
# (d) exactly ONE acting card for one stall
# ---------------------------------------------------------------------------------------------


def test_n_ticks_on_a_skewed_tree_file_exactly_one_actor_card(board, tree):
    conn = board
    tf.record_boot()
    _edit(tree)

    for _ in range(5):
        result = _tick(conn)
        assert result.tick_yielded.startswith(kbd.TICK_YIELD_PREFIX)

    rows = _actor_rows()
    assert len(rows) == 1, f"one skewed process must resolve to ONE card, got {len(rows)}"
    assert rows[0]["assignee"] == kbd.TICK_YIELD_ACTOR_ASSIGNEE == "default"
    assert rows[0]["idempotency_key"].startswith(f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}")


def test_a_different_frozen_process_may_file_its_own_card(board, tree, monkeypatch):
    """The PID is what separates two stalls: the same tree skewing under a NEW process is a new
    stall, and each must run its own streak to the threshold."""
    conn = board
    tf.record_boot()
    _edit(tree)
    boot = tf.boot_record()
    for _ in range(THRESHOLD):
        _tick(conn)

    monkeypatch.setattr(tf, "_boot_record", {**boot, "pid": int(boot["pid"]) + 1})
    for _ in range(THRESHOLD):
        _tick(conn)

    assert len(_actor_rows()) == 2


# ---------------------------------------------------------------------------------------------
# the non-green status surface
# ---------------------------------------------------------------------------------------------


def test_a_refused_tick_records_a_non_green_status_a_human_reads(board, tree, caplog):
    conn = board
    tf.record_boot()
    _edit(tree)

    with caplog.at_level("ERROR", logger="hermes_cli.kanban_db_dispatch"):
        result = _tick(conn)

    path, status_record = _status_record()
    assert status_record is not None, "a refusing tick must leave a record a human reads"
    assert status_record["yielded"] is True
    assert status_record["boot_fingerprint"] != status_record["disk_fingerprint"]
    boot_record = tf.boot_record()
    assert boot_record is not None
    assert status_record["pid"] == boot_record["pid"]
    assert status_record["reason"] == result.tick_yielded
    # Loud, not silent: the status record's path is in the same line.
    messages = [r.getMessage() for r in caplog.records]
    assert any(kbd.TICK_YIELD_PREFIX in message for message in messages)
    assert any(str(path) in message for message in messages)


def test_the_healthy_tick_retires_its_own_non_green_marker(board, tree):
    """Once the process serves normally again the marker goes -- nobody has to remember to clear it."""
    conn = board
    tf.record_boot()
    _edit(tree)
    _tick(conn)
    _path, record = _status_record()
    assert record is not None

    _edit(tree, body="VALUE = 1\n" + "#" * 40)  # still skewed: the marker must SURVIVE this
    _tick(conn)
    assert _status_record()[1] is not None

    # Rewind to the recorded content (the reload door's effect, without a bounce): the marker clears.
    tf.reset_boot()
    tf.record_boot()
    _tick(conn)
    assert _status_record()[1] is None


def test_a_live_foreign_marker_is_never_cleared_by_a_healthy_process(board, tree, monkeypatch):
    """Clearing a LIVE gateway's marker from another process would read green over a frozen one."""
    conn = board
    tf.record_boot()
    _edit(tree)
    _tick(conn)
    path, status_record = _status_record()
    assert status_record is not None
    # The marker now names a FOREIGN, STILL-LIVE process (as a sibling gateway's would).
    path.write_text(json.dumps({**status_record, "pid": os.getpid() + 1}), encoding="utf-8")
    monkeypatch.setattr(kbd, "_pid_liveness", lambda pid: kbd.WORKER_ALIVE)

    kbd._clear_tick_yield_status()

    assert path.exists(), "a live process's refusal must not be cleared by a sibling"

    # A marker whose recorder is PROVEN dead is history, not a live condition: that one retires.
    monkeypatch.setattr(kbd, "_pid_liveness", lambda pid: kbd.WORKER_DEAD)
    kbd._clear_tick_yield_status()
    assert not path.exists()


# ---------------------------------------------------------------------------------------------
# (e) cost: O(tick), never O(cards)
# ---------------------------------------------------------------------------------------------


def test_the_skew_check_runs_once_per_tick_not_per_card(
    board, tree, monkeypatch, all_assignees_spawnable,
):
    conn = board
    tf.record_boot()
    calls: list = []
    real = tf.content_fingerprint

    def _counting(root=None):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(tf, "content_fingerprint", _counting)

    # Empty board: one tick, one check.
    _tick(conn)
    assert len(calls) == 1, f"one tick must make one check, made {len(calls)}"

    # A board with several spawnable cards: STILL one check -- the check is per tick, not per card.
    for index in range(4):
        kb.create_task(conn, title=f"card {index}", assignee="alice")
    calls.clear()
    result = _tick(conn)

    assert len(result.spawned) == 4, "the clean-tick control must still spawn the cards"
    assert len(calls) == 1, (
        f"the check must not run per card: {len(calls)} checks for {len(result.spawned)} cards"
    )


def test_the_steady_state_check_does_not_reread_file_bytes(board, tree, monkeypatch):
    """The memo keeps a per-tick check cheap: a stat walk, not 32MB of hashing, every tick."""
    tf.record_boot()
    tf._content_memo.clear()  # the boot snapshot warmed the memo; this test measures the ticks
    reads: list = []
    real_digest = tf._digest_files

    def _counting(files, root):
        reads.append(len(files))
        return real_digest(files, root)

    monkeypatch.setattr(tf, "_digest_files", _counting)
    tf.content_fingerprint()
    tf.content_fingerprint()
    tf.content_fingerprint()

    assert len(reads) == 1, f"an unchanged tree must be digested once, not {len(reads)} times"
    _edit(tree)
    tf.content_fingerprint()
    assert len(reads) == 2, "a moved file must force exactly one re-digest"
