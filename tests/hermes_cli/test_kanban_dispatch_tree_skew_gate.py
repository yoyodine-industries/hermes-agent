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

**DEC-1..DEC-3 -- the per-episode identity, the freeze-proof door, and retirement** (cards
``t_a54c8157`` design / ``t_ad5dc5d5`` implementation):

* identity is the EPISODE, not the process: the signature is the imported CONTENT alone and the
  window is ``<sig>:<first_tick>``, so two pids on the same content are ONE stall with ONE streak and
  ONE card, while a stall that cleared and came back is a new window and gets a new card (DEC-1);
* the door that survives a frozen dispatcher is the escalation REGISTRY -- one durable row plus the
  DM it sends -- attempted ONCE per episode and bounded by the status record's ``delivery`` block,
  with the card as the RECORD and the ops-seat peer nudge as the fallback leg (DEC-2);
* the first tick that serves normally again retires the cleared episode's actor cards through the
  kernel's own doors -- ``unblock_task`` then ``complete_task`` -- while a card filed by a still-live
  FOREIGN process is left alone (DEC-3).

The cases are named A1..A10 after the acceptance plan in
``/opt/hermes_sandbox/platform/work/platform-stl/t_0b2118cb-tree-skew-streak/acceptance/TESTPLAN.md``;
the ``dod_*`` cases carry this card's own DoD (i)/(ii)/(iii).

Everything here runs on a SCRATCH surface: the ticking board and the ops board are both throwaway
DBs under ``tmp_path``, ``TICK_YIELD_ACTOR_BOARD`` is pointed at the scratch ops board, the
escalation registry is a FAKE module injected by path (never the live ``escalations.db``), the peer
leg is recorded instead of spawned, and the repo's hermetic fixture
(``tests/conftest.py::_hermetic_environment``) strips the dispatcher's ``HERMES_KANBAN_*`` pins --
so the fleet's ``migrations`` board is unreachable from this module.
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

#: The fake registry module. Written to ``tmp_path`` and loaded by the SHIPPED loader
#: (``importlib`` by path), so the delivery path exercised is the real one; it appends every call to
#: a JSONL file the assertions read back. ``__CALLS__`` is substituted with the file's path.
FAKE_LEDGER_TEMPLATE = '''
import json

CALLS = __CALLS__


def _read():
    try:
        with open(CALLS, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except OSError:
        return []


def _record(kind, payload):
    with open(CALLS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": kind, **payload}) + "\\n")


def register(**kwargs):
    rows = [call for call in _read() if call["kind"] == "register"]
    ref = "ESC-%05d" % (len(rows) + 1)
    payload = {key: value for key, value in kwargs.items() if key != "submission"}
    payload["ref"] = ref
    _record("register", payload)
    return {"ref": ref, "created": True}


def notify(ref, owner_lane, line, **kwargs):
    _record("notify", {"ref": ref, "owner_lane": owner_lane, "line": line})
    return {"ref": ref, "notify_state": "sent"}
'''

#: A registry candidate that raises on IMPORT -- DEC-2.1's named-failure arm.
BROKEN_LEDGER_TEMPLATE = '''
raise RuntimeError("simulated: the ledger module is half-landed")
'''


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


@pytest.fixture(autouse=True)
def _escalation_door(tmp_path, monkeypatch):
    """Point DEC-2's door at a FAKE registry by path, and make leg 2 inert-but-recorded.

    Nothing in this module may reach the live estate: the real ``escalations.db`` (a durable
    escalation row per test) or a ``hermes peer run`` child (a DM per test). The fake is injected
    through the SAME env-var first hop the shipped loader reads, so the loader, the call shape and
    the bounded-retry logic are all the real ones.
    """
    calls = tmp_path / "escalation-calls.jsonl"
    module = tmp_path / "fake_escalation_ledger.py"
    module.write_text(
        FAKE_LEDGER_TEMPLATE.replace("__CALLS__", repr(str(calls))), encoding="utf-8")
    monkeypatch.setenv(kbd.TICK_YIELD_ESCALATION_ENV, str(module))
    peer_calls: list = []

    def _peer(line, episode):
        peer_calls.append({"line": line, "episode": episode})
        return "sent", None

    monkeypatch.setattr(kbd, "_peer_run_tree_skew", _peer)
    yield {"calls": calls, "peer": peer_calls, "module": module, "tmp_path": tmp_path}


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


def _status_record():
    path = kbd.tick_yield_status_path()
    return path, (json.loads(path.read_text(encoding="utf-8")) if path.exists() else None)


def _ledger_calls(door, kind=None):
    """Every call the fake registry recorded (``register`` / ``notify``), oldest first."""
    path = door["calls"]
    if not path.exists():
        return []
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return [row for row in rows if kind is None or row.get("kind") == kind]


#: Monotonic counter so each :func:`_clear_the_episode` call edits DIFFERENT content.
_EPISODE_SEQ = [0]


def _clear_the_episode(conn, tree):
    """One refusal (marker written, no card yet), then a rewind: the next tick SERVES.

    The serving tick is DEC-3's trigger -- it retires the marker, and the episode with it. The
    edited body is unique per call so a SECOND call in one test is a second, genuinely different
    skew rather than a no-op write.
    """
    _EPISODE_SEQ[0] += 1
    tf.reset_boot()
    tf.record_boot()
    _edit(tree, body=f"VALUE = 2\n# episode {_EPISODE_SEQ[0]}\n")
    _tick(conn)
    tf.reset_boot()
    tf.record_boot()
    return _tick(conn)


def _seed_legacy_actor(*, pid, ordinal=1, status="scheduled", sig=None):
    """A carrier in the LEGACY key shape, ``tree-skew:<sig>:<pid>:<ord>``.

    78 of those are live on the fleet's ops board, and DEC-3 must drain them by the pid they carry.
    The signature is deliberately foreign, so the seeded card can never absorb (or be absorbed by)
    the episode under test.
    """
    sig = sig or ("tree-content:" + "a" * 64)
    conn = kbc.connect(board=OPS_BOARD)
    try:
        task_id = kb.create_task(
            conn,
            title=kbd.TICK_YIELD_ACTOR_TITLE,
            body="legacy carrier (pre-DEC-1 key shape)",
            assignee=kbd.TICK_YIELD_ACTOR_ASSIGNEE,
            created_by="kanban-dispatcher",
            idempotency_key=f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}{sig}:{pid}:{ordinal}",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        return task_id
    finally:
        conn.close()


def _actor_status(task_id):
    conn = kbc.connect(board=OPS_BOARD)
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row["status"] if row is not None else None
    finally:
        conn.close()


def _actor_event_kinds(task_id):
    conn = kbc.connect(board=OPS_BOARD)
    try:
        return [
            row["kind"] for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        ]
    finally:
        conn.close()


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


def test_a1_one_refused_tick_records_the_streak_and_files_nothing(board, tree, _escalation_door):
    """A1. One refusal is churn, not a stall: the record carries the streak, the board carries
    no card.

    DEC-1.1/1.3: the signature is the imported CONTENT (no pid), and the episode is that content
    plus the window's start. The pid is still written, as evidence.
    """
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
    assert record["sig"] == record["boot_fingerprint"], "identity is the imported content, no pid"
    assert f":{record['pid']}" not in record["sig"], "the pid is NOT identity (DEC-1.1)"
    assert record["pid"] == tf.boot_record()["pid"], "the pid is still EVIDENCE"
    assert record["episode"] == f"{record['sig']}:{record['first_tick']}"
    assert record["threshold"] == THRESHOLD
    assert record["delivery"] is None, "below the threshold the door is not knocked on"
    assert _escalation_door["peer"] == []
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


def test_a3_the_third_refused_tick_files_exactly_one_actor_on_the_ops_board(
    board, tree, _escalation_door,
):
    """A3. The act fires AT the threshold, on the OPS board, addressed to the ops seat, keyed
    ``tree-skew:<signature>:<window_start>`` (DEC-1.4).

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
    record = _status_record()[1]
    assert record["streak"] == 3
    assert row["assignee"] == kbd.TICK_YIELD_ACTOR_ASSIGNEE == "default"
    assert row["idempotency_key"] == (
        f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}{boot['fingerprint']}:{record['first_tick']}"
    ), "the key is the CONTENT plus the WINDOW -- no pid, no ordinal"
    assert row["priority"] == 0, "no policy on the board: the kernel default, not an invented band"
    assert "no priority policy on board" in row["body"], row["body"]
    assert _ticking_rows(conn) == [], "the card must NOT land on the board that ticked"
    # DEC-2.4: the card quotes the escalation the door raised.
    regs = _ledger_calls(_escalation_door, "register")
    assert len(regs) == 1
    assert f"- escalation: `{regs[0]['ref']}`" in row["body"], row["body"]


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

    assert _actor_rows()[0]["idempotency_key"].endswith(
        f":{_status_record()[1]['first_tick']}"
    ), "a window key, not an ordinal"


# ---------------------------------------------------------------------------------------------
# A5 -- the WINDOW: a cleared episode never absorbs the next one
# ---------------------------------------------------------------------------------------------


class _Clock:
    """A forward-only clock shim for :mod:`hermes_cli.kanban_db_dispatch`.

    Windows are second-granular, and a test drives several windows inside one second. Real episodes
    are minutes apart (a reload plus a re-skew), so the shim moves the clock, never the code.
    """

    def __init__(self, real):
        self._real = real
        self.offset = 0

    def time(self):
        return self._real.time() + self.offset

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a5_a_cleared_episode_never_absorbs_the_next_window(
    board, tree, monkeypatch, _escalation_door,
):
    """A5. THE defect this card exists for, in its DEC-1.4 form.

    On 2026-10-11 three guard cards were closed on a correct measurement, the stall continued under
    the same gateway pid, and the closed carrier's key swallowed every refusal -- the board carried
    NO card until a human bounced the gateway at 04:45. One stall is one card, but a CLEARED stall is
    a different episode: its carrier is terminal, and the next window mints a new key from its own
    ``first_tick``.
    """
    import time as real_time

    clock = _Clock(real_time)
    monkeypatch.setattr(kbd, "time", clock)
    conn = board
    tf.record_boot()
    _edit(tree)
    skewed = dict(tf.boot_record())

    for _ in range(THRESHOLD):
        _tick(conn)
    first = _actor_rows()
    assert len(first) == 1, f"the first window files one card, got {len(first)}"
    first_key = first[0]["idempotency_key"]
    first_window = _status_record()[1]["first_tick"]

    # The episode CLEARS: rewind to the recorded content. The SERVING tick retires the carrier
    # (DEC-3), which is also what makes the old key terminal.
    tf.reset_boot()
    tf.record_boot()
    _tick(conn)
    assert _actor_rows()[0]["status"] == "done", "a cleared episode retires its carrier"
    assert _status_record()[1] is None

    # The SAME imported content skews again: same signature, NEW window.
    clock.offset += 60
    monkeypatch.setattr(tf, "_boot_record", skewed)
    for _ in range(THRESHOLD):
        _tick(conn)

    rows = _actor_rows()
    assert len(rows) == 2, f"the next window must file a SECOND card, got {rows}"
    record = _status_record()[1]
    assert record["first_tick"] > first_window
    assert rows[1]["idempotency_key"] == (
        f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}{skewed['fingerprint']}:{record['first_tick']}"
    ), "the new window keys from its OWN first tick"
    assert rows[1]["idempotency_key"] != first_key
    assert rows[1]["status"] not in ("done", "archived"), "the new carrier is live"

    # ...and THAT carrier is live, so a further refusal inside the window is absorbed again.
    _tick(conn)
    assert len(_actor_rows()) == 2
    # DEC-2.4: one delivery attempt per episode -- two windows, two rows, two nudges, no more.
    assert len(_ledger_calls(_escalation_door, "register")) == 2
    assert len(_ledger_calls(_escalation_door, "notify")) == 2


# ---------------------------------------------------------------------------------------------
# A6 -- the identity is the EPISODE, not the process
# ---------------------------------------------------------------------------------------------


def test_a6_two_pids_on_the_same_content_are_one_streak_and_one_card(board, tree, monkeypatch):
    """A6/DEC-1.1/1.2. ``boot_fingerprint`` alone is the identity: a respawned dispatcher on the
    same imported content continues the SAME window -- same streak, same ``first_tick``, and the
    SAME card -- instead of starting a second stall nobody asked for."""
    conn = board
    tf.record_boot()
    _edit(tree)
    boot = tf.boot_record()

    _tick(conn)
    _tick(conn)
    first_window = _status_record()[1]["first_tick"]

    # The dispatcher respawns (a launchd bounce, a crash) on the SAME imported content.
    monkeypatch.setattr(tf, "_boot_record", {**boot, "pid": int(boot["pid"]) + 1})
    _tick(conn)

    rows = _actor_rows()
    assert len(rows) == 1, f"one stalled revision is ONE card, got {rows}"
    record = _status_record()[1]
    assert record["streak"] == 3, "the streak CONTINUES across pids (DEC-1.2)"
    assert record["first_tick"] == first_window, "the window did NOT restart"
    assert record["pid"] == int(boot["pid"]) + 1, "the pid is still recorded, as evidence"
    assert rows[0]["idempotency_key"] == (
        f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}{boot['fingerprint']}:{first_window}"
    ), "one window, one key -- no pid in it"

    # A further refusal from the second pid is still absorbed.
    _tick(conn)
    assert len(_actor_rows()) == 1
    assert _status_record()[1]["streak"] == 4


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
    assert record["sig"] == skewed["fingerprint"], "the signature is the content, no pid"


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


def test_a_different_frozen_process_on_the_same_content_absorbs_into_the_existing_card(
    board, tree, monkeypatch,
):
    """DEC-1.1: the PID does NOT separate two stalls. A respawned dispatcher on the same imported
    content is the same stalled revision, so its third refusal is the SAME window's third refusal --
    one card, not one per process."""
    conn = board
    tf.record_boot()
    _edit(tree)
    boot = tf.boot_record()
    for _ in range(THRESHOLD):
        _tick(conn)
    assert len(_actor_rows()) == 1

    monkeypatch.setattr(tf, "_boot_record", {**boot, "pid": int(boot["pid"]) + 1})
    for _ in range(THRESHOLD):
        _tick(conn)

    rows = _actor_rows()
    assert len(rows) == 1, f"ONE OPEN actor per imported content, got {rows}"
    assert _status_record()[1]["streak"] == THRESHOLD * 2, "the streak carried across the respawn"


# ---------------------------------------------------------------------------------------------
# this card's own DoD (i)-(iv)
# ---------------------------------------------------------------------------------------------


def test_dod_i_two_pids_on_one_content_are_one_streak_one_card_one_row_one_nudge(
    board, tree, monkeypatch, _escalation_door,
):
    """DoD (i). Two pids, one refused content: ONE card, ONE streak, ONE window, ONE escalation
    row, ONE nudge. The double row was the post-fix killer -- a door knocked on per process."""
    conn = board
    tf.record_boot()
    _edit(tree)
    boot = tf.boot_record()

    for _ in range(THRESHOLD + 2):
        _tick(conn)
    first_window = _status_record()[1]["first_tick"]

    monkeypatch.setattr(tf, "_boot_record", {**boot, "pid": int(boot["pid"]) + 1})
    for _ in range(THRESHOLD + 2):
        _tick(conn)

    rows = _actor_rows()
    record = _status_record()[1]
    assert len(rows) == 1, f"one card, got {rows}"
    assert record["streak"] == 2 * (THRESHOLD + 2), "one streak"
    assert record["first_tick"] == first_window, "one window"
    assert record["episode"] == f"{boot['fingerprint']}:{first_window}"
    assert rows[0]["idempotency_key"].endswith(f":{first_window}")

    regs = _ledger_calls(_escalation_door, "register")
    notifies = _ledger_calls(_escalation_door, "notify")
    assert len(regs) == 1, f"ONE escalation row for the episode, got {regs}"
    assert len(notifies) == 1, f"ONE nudge for the episode, got {notifies}"
    assert regs[0]["source_id"] == record["episode"], "keyed by the EPISODE"
    assert regs[0]["board"] == OPS_BOARD
    assert regs[0]["owner_lane"] == "default", "explicit owner_lane, never a live resolver read"
    assert regs[0]["raised_by"] == "kanban-dispatcher"
    assert boot["fingerprint"] in regs[0]["reason"]
    assert notifies[0]["ref"] == regs[0]["ref"]
    assert notifies[0]["owner_lane"] == "default"
    assert record["delivery"]["ref"] == regs[0]["ref"]
    assert record["delivery"]["leg"] == "ledger"
    assert record["delivery"]["state"] == "sent"
    assert record["delivery"]["error"] is None
    assert _escalation_door["peer"] == [], "leg 2 must not run when leg 1 carried the door"


def test_dod_iii_the_delivery_is_attempted_once_per_episode(board, tree, _escalation_door):
    """DoD (iii). N more refused ticks after the attempt add NO second row and NO second nudge --
    the status record's ``delivery`` block is the bound, and it survives every rewrite."""
    conn = board
    tf.record_boot()
    _edit(tree)

    for _ in range(THRESHOLD):
        _tick(conn)
    first_attempt = dict(_status_record()[1]["delivery"])
    assert first_attempt["state"] == "sent"

    for _ in range(5):
        _tick(conn)

    record = _status_record()[1]
    assert record["streak"] == THRESHOLD + 5, "the streak keeps climbing"
    assert record["delivery"] == first_attempt, "the attempt is not repeated, not overwritten"
    assert len(_ledger_calls(_escalation_door, "register")) == 1
    assert len(_ledger_calls(_escalation_door, "notify")) == 1
    assert len(_actor_rows()) == 1


def test_dod_ii_a_cleared_episode_retires_its_card_including_a_scheduled_one(board, tree):
    """DoD (ii). The first tick that serves normally again retires the card the episode filed --
    and it does so through ``unblock_task`` first, because a ``scheduled`` card is exactly the live
    shape that ``complete_task`` refuses (42 of the 51 stale cards were ``scheduled``)."""
    conn = board
    tf.record_boot()
    _edit(tree)
    for _ in range(THRESHOLD):
        _tick(conn)
    rows = _actor_rows()
    assert len(rows) == 1
    task_id = rows[0]["id"]

    # The live shape: scheduled, not ready.
    conn2 = kbc.connect(board=OPS_BOARD)
    try:
        with kb.write_txn(conn2):
            conn2.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (task_id,))
    finally:
        conn2.close()
    assert _actor_status(task_id) == "scheduled"

    # The episode clears.
    tf.reset_boot()
    tf.record_boot()
    result = _tick(conn)

    assert result.tick_yielded == "", "the tick served"
    assert _actor_status(task_id) == "done", "the cleared episode's card is retired"
    assert kbd.TICK_YIELD_RETIRED_EVENT in _actor_event_kinds(task_id)
    assert _status_record()[1] is None, "the marker was retired by the same tick"

    # A retirement writes its own evidence: who retired it, whose card it was, and the key.
    conn3 = kbc.connect(board=OPS_BOARD)
    try:
        row = conn3.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC "
            "LIMIT 1", (task_id, kbd.TICK_YIELD_RETIRED_EVENT),
        ).fetchone()
        payload = json.loads(row["payload"])
        task = conn3.execute("SELECT status, result FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn3.close()
    assert payload["reason"] == "gate-clear"
    assert payload["retired_by_pid"] == os.getpid()
    assert payload["key"] == rows[0]["idempotency_key"]
    assert task["status"] == "done"
    assert "[retired] episode cleared at" in task["result"]


def test_dod_ii_a_card_whose_filing_pid_is_a_LIVE_foreign_process_is_left_alone(
    board, tree, monkeypatch,
):
    """DEC-3.3. A frozen peer still holds that episode: retiring its card on OUR clean tick would
    read green over a frozen gateway -- the exact false green this block exists to prevent."""
    conn = board
    _clear_the_episode(conn, tree)
    foreign_pid = os.getpid() + 1
    task_id = _seed_legacy_actor(pid=foreign_pid)
    monkeypatch.setattr(kbd, "_pid_liveness", lambda pid: kbd.WORKER_ALIVE)

    _clear_the_episode(conn, tree)

    assert _actor_status(task_id) == "scheduled", "a LIVE foreign filing pid is not retired"
    assert kbd.TICK_YIELD_RETIRED_EVENT not in _actor_event_kinds(task_id)


def test_dod_ii_a_card_whose_filing_pid_is_PROVEN_dead_is_retired(board, tree, monkeypatch):
    """The other arm of the fence: a DEAD filing pid's card is history, and history is retirable."""
    conn = board
    _clear_the_episode(conn, tree)
    task_id = _seed_legacy_actor(pid=os.getpid() + 1)
    monkeypatch.setattr(kbd, "_pid_liveness", lambda pid: kbd.WORKER_DEAD)

    _clear_the_episode(conn, tree)

    assert _actor_status(task_id) == "done"
    assert kbd.TICK_YIELD_RETIRED_EVENT in _actor_event_kinds(task_id)


def test_dod_ii_a_card_from_our_own_pid_is_retired_without_a_liveness_probe(board, tree):
    """DEC-3.5: our own pid is alive by definition, so no probe is needed or wanted."""
    conn = board
    _clear_the_episode(conn, tree)
    task_id = _seed_legacy_actor(pid=os.getpid())

    _clear_the_episode(conn, tree)

    assert _actor_status(task_id) == "done"


def test_dod_ii_a_RUNNING_card_is_never_retired(board, tree):
    """DEC-3.6: a card under a live claim is somebody's work, gate-clear or not."""
    conn = board
    _clear_the_episode(conn, tree)
    task_id = _seed_legacy_actor(pid=os.getpid(), status="running")

    _clear_the_episode(conn, tree)

    assert _actor_status(task_id) == "running"
    assert kbd.TICK_YIELD_RETIRED_EVENT not in _actor_event_kinds(task_id)


def test_dod_ii_a_window_key_is_never_mistaken_for_a_legacy_pid():
    """The key parse must not read a 10-digit epoch as a pid (DEC-3.3)."""
    prefix = kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX
    sig = "tree-content:" + "b" * 64
    assert kbd._tree_skew_actor_pid_from_key(f"{prefix}{sig}:1770000000") is None
    assert kbd._tree_skew_actor_pid_from_key(f"{prefix}{sig}:4321:2") == 4321
    assert kbd._tree_skew_actor_pid_from_key("") is None
    assert kbd._tree_skew_actor_pid_from_key("tree-skew:") is None


def test_dod_iv_the_signature_and_the_streak_are_invariant_under_a_pid_change():
    """DoD (iv), as a unit: the same content under two pids yields ONE signature, ONE episode, and
    ONE key -- the pid is evidence, never identity."""
    yielded_a = kbd.DispatcherTickYielded("tree-content:aaa", "tree-content:bbb", boot_pid=111)
    yielded_b = kbd.DispatcherTickYielded("tree-content:aaa", "tree-content:bbb", boot_pid=222)
    assert kbd._yield_signature(yielded_a) == kbd._yield_signature(yielded_b) == "tree-content:aaa"
    assert kbd._episode_identity(kbd._yield_signature(yielded_a), 1770000000) == (
        kbd._episode_identity(kbd._yield_signature(yielded_b), 1770000000)
    )
    assert yielded_a.boot_pid != yielded_b.boot_pid, "the pid is still carried, as evidence"


# ---------------------------------------------------------------------------------------------
# DEC-2 -- the door's two legs and its boundaries
# ---------------------------------------------------------------------------------------------


def test_the_escalation_source_prefers_env_then_config_then_the_estate_default(
    monkeypatch, tmp_path,
):
    """DEC-2.1: the module path is resolved ENV first, then ``kanban.escalation_ledger``, then the
    estate default -- and the default is what keeps the door armed on a tree declaring nothing."""
    monkeypatch.setenv(kbd.TICK_YIELD_ESCALATION_ENV, str(tmp_path / "env_module.py"))
    assert kbd._escalation_ledger_source() == str(tmp_path / "env_module.py")

    monkeypatch.delenv(kbd.TICK_YIELD_ESCALATION_ENV)
    holder = {"cfg": {"kanban": {"escalation_ledger": str(tmp_path / "cfg_module.py")}}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: holder["cfg"])
    assert kbd._escalation_ledger_source() == str(tmp_path / "cfg_module.py")

    holder["cfg"] = {"kanban": {}}
    assert kbd._escalation_ledger_source() == kbd.TICK_YIELD_ESCALATION_DEFAULT


def test_a_disabled_registry_falls_through_to_the_peer_leg(
    board, tree, monkeypatch, _escalation_door,
):
    """DEC-2.1: ``none``/``off`` is a kill switch, never a silent no-op -- leg 2 carries the door
    and the card says no ref was filed instead of quoting one that does not exist."""
    monkeypatch.setenv(kbd.TICK_YIELD_ESCALATION_ENV, "none")
    conn = board
    tf.record_boot()
    _edit(tree)

    for _ in range(THRESHOLD):
        _tick(conn)

    record = _status_record()[1]
    delivery = record["delivery"]
    assert delivery["leg"] == "peer"
    assert delivery["state"] == "sent"
    assert delivery["ref"] is None
    assert "disabled" in (delivery["error"] or ""), delivery
    assert _ledger_calls(_escalation_door, "register") == []
    assert len(_escalation_door["peer"]) == 1
    assert record["episode"] in _escalation_door["peer"][0]["episode"]
    assert record["episode"] in _escalation_door["peer"][0]["line"]
    rows = _actor_rows()
    assert len(rows) == 1, "the RECORD still files"
    assert "- escalation: `(none filed" in rows[0]["body"], rows[0]["body"]


def test_a_registry_that_cannot_import_is_a_NAMED_failure_and_leg_2_still_runs(
    board, tree, monkeypatch, _escalation_door, tmp_path,
):
    """DEC-2.1: a half-landed ledger must degrade to a named failure, never an exception in the
    tick -- and the fallback leg must still knock."""
    broken = tmp_path / "broken_ledger.py"
    broken.write_text(BROKEN_LEDGER_TEMPLATE, encoding="utf-8")
    monkeypatch.setenv(kbd.TICK_YIELD_ESCALATION_ENV, str(broken))
    conn = board
    tf.record_boot()
    _edit(tree)

    for _ in range(THRESHOLD):
        _tick(conn)

    delivery = _status_record()[1]["delivery"]
    assert delivery["leg"] == "peer", "leg 1 could not load, so leg 2 carried it"
    assert delivery["state"] == "sent"
    assert "RuntimeError" in delivery["error"] and "half-landed" in delivery["error"]
    assert len(_escalation_door["peer"]) == 1
    assert len(_actor_rows()) == 1


def test_when_BOTH_legs_fail_the_record_names_it_and_the_card_still_files(
    board, tree, monkeypatch, _escalation_door, tmp_path,
):
    """DEC-2's no-silent-failure arm: an undeliverable door is RECORDED as failed, with both
    reasons, and the card is still filed -- a failed DM must never cost the record."""
    broken = tmp_path / "broken_ledger.py"
    broken.write_text(BROKEN_LEDGER_TEMPLATE, encoding="utf-8")
    monkeypatch.setenv(kbd.TICK_YIELD_ESCALATION_ENV, str(broken))
    monkeypatch.setattr(
        kbd, "_peer_run_tree_skew",
        lambda line, episode: ("failed", "gateway unreachable (simulated)"),
    )
    conn = board
    tf.record_boot()
    _edit(tree)

    for _ in range(THRESHOLD):
        _tick(conn)

    delivery = _status_record()[1]["delivery"]
    assert delivery["state"] == "failed"
    assert delivery["leg"] == "peer"
    assert "RuntimeError" in delivery["error"], "leg 1's named failure is kept"
    assert "gateway unreachable (simulated)" in delivery["error"], "leg 2's reason is kept too"
    rows = _actor_rows()
    assert len(rows) == 1
    assert "- escalation: `(none filed" in rows[0]["body"]


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


# ---------------------------------------------------------------------------------------------
# (f) the guard card's OWN contract (card t_4dbee32e): born ready, in the TOP band, closed by
#     the tick that clears it, and escalated -- not duplicated -- when a reload does not help
# ---------------------------------------------------------------------------------------------

#: The fleet's own ladder. The guard card must be placed by the REAL policy, not by a stub that
#: restates it; when the fleet tree is not on this host the assertion degrades to the mechanism
#: (the declaration line the ladder reads) instead of asserting nothing.
FLEET_BANDS = (
    "/opt/hermes_prod/yaan-platform/scripts/kanban_priority_bands.py",
    "/opt/hermes_sandbox/yaan-platform/scripts/kanban_priority_bands.py",
)


def _fleet_band_module():
    import importlib.util

    for candidate in FLEET_BANDS:
        if not os.path.exists(candidate):
            continue
        spec = importlib.util.spec_from_file_location("fleet_kanban_priority_bands", candidate)
        if spec is None or spec.loader is None:  # pragma: no cover - a broken path is a skip
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if hasattr(module, "band_birth"):
            return module
    return None


def _point_ops_board_at_fleet_policy(module_path: str) -> None:
    """Give the SCRATCH ops board the fleet's ladder, so the filing is judged by the real one."""
    import pathlib

    board_json = pathlib.Path(os.environ["HERMES_KANBAN_HOME"]) / "kanban" / "boards" / OPS_BOARD / "board.json"
    doc = json.loads(board_json.read_text(encoding="utf-8"))
    doc["priority_policy"] = {"module": module_path, "function": "band_birth"}
    board_json.write_text(json.dumps(doc), encoding="utf-8")


def _file_a_guard_card(board, tree) -> None:
    tf.record_boot()
    _edit(tree)
    for _ in range(THRESHOLD):
        _tick(board)
    assert len(_actor_rows()) == 1, "the third refusal must file exactly one guard card"


def test_guard_card_is_ready_and_top_band(board, tree):
    """The card that CARRIES the deadline is ready, and the ladder puts it in the top band.

    Measured before this: 116 guard cards across six boards, 42 of them in ``scheduled`` (a
    deadline nobody is dispatched against) and every one of them at 790000 -- the tier's REST band
    -- because the filing never declared anything for the ladder to read.
    """
    conn = board
    bands = _fleet_band_module()
    if bands is not None:
        _point_ops_board_at_fleet_policy(FLEET_BANDS[0] if os.path.exists(FLEET_BANDS[0]) else FLEET_BANDS[1])
    _file_a_guard_card(conn, tree)

    row = _actor_rows()[0]
    assert row["status"] == "ready", f"a guard card is born READY, got {row['status']!r}"
    assert row["body"].startswith(kbd.TICK_YIELD_ACTOR_DECLARATION), (
        "the card must declare its blast radius FIRST: the ladder reads that line and nothing else"
    )
    if bands is None:  # pragma: no cover - the fleet tree is not on this host
        return
    verdict = bands.band_birth(
        0, kbd.TICK_YIELD_ACTOR_ASSIGNEE, OPS_BOARD, kbd.TICK_YIELD_ACTOR_TITLE, row["body"],
    )
    assert verdict["qualified"] is True, verdict
    assert "gateway" in verdict["qualification"], verdict
    assert verdict["applied"] == 800000, verdict
    assert row["priority"] == verdict["applied"], (
        f"the filing must keep the ladder's answer: row={row['priority']} ladder={verdict['applied']}"
    )


def test_a_normal_tick_closes_the_guard_card_it_cleared(board, tree):
    """Auto-close is proof-driven: a SERVED tick after the frozen process is done, nothing less."""
    conn = board
    _file_a_guard_card(conn, tree)
    card = _actor_rows()[0]
    assert card["status"] == "ready"

    # A REFUSED tick (skew still there) must leave the card exactly where it is.
    _tick(conn)
    assert _actor_rows()[0]["status"] == "ready", "a yielding tick is not a proof of anything"

    # The reload landed: the tree now matches what this process imported, so the tick SERVES.
    _edit(tree, body="VALUE = 1\n")
    result = _tick(conn)
    assert result.tick_yielded == "", result.tick_yielded

    assert _actor_rows()[0]["status"] == "done", (
        "a verified normal tick is the evidence the card's own 'What closes it' names"
    )
    ops = kbc.connect(board=OPS_BOARD)
    try:
        evidence = ops.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (card["id"],)
        ).fetchone()
    finally:
        ops.close()
    assert evidence is not None, "the close must land as a run with its proof recorded"
    payload = json.loads(evidence["metadata"] or "{}")
    assert "tree_skew_cleared" in payload, payload
    assert payload["tree_skew_cleared"]["observed_by_pid"] == os.getpid()


def test_a_skew_that_survives_a_reload_escalates_instead_of_refiling(board, tree, monkeypatch):
    """A second frozen process, same disk content: the reload door is broken -> ONE escalation.

    This is the 116-card pile in one test. A reload DID happen (a different pid), the skew came
    back on the SAME checkout, and repeating "reload it" is not a plan -- the design authority has
    to say whether the fingerprint or the reload is wrong.
    """
    conn = board
    _file_a_guard_card(conn, tree)
    first = _actor_rows()[0]
    assert first["idempotency_key"].startswith(kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX)

    # The reload: a NEW process now serves, and the OLD pid is proven dead.
    frozen_pid = os.getpid()
    tf.boot_record()["pid"] = frozen_pid + 1
    real_liveness = kbd._pid_liveness
    monkeypatch.setattr(
        kbd, "_pid_liveness",
        lambda pid: kbd.WORKER_DEAD if int(pid) == frozen_pid else real_liveness(pid),
    )

    for _ in range(THRESHOLD):
        _tick(conn)

    ops = kbc.connect(board=OPS_BOARD)
    try:
        cards = ops.execute(
            "SELECT id, title, assignee, status, idempotency_key FROM tasks "
            "WHERE idempotency_key LIKE ? ORDER BY rowid",
            (f"{kbd.TICK_YIELD_ESCALATION_KEY_PREFIX}%",),
        ).fetchall()
    finally:
        ops.close()
    assert len(cards) == 1, f"exactly ONE escalation for this disk content, got {len(cards)}"
    escalation = cards[0]
    assert escalation["assignee"] == kbd.TICK_YIELD_ESCALATION_ASSIGNEE == "platform-stl"
    assert escalation["status"] == "ready"
    assert escalation["title"].startswith("[escalation]")
    ops = kbc.connect(board=OPS_BOARD)
    try:
        guard_cards = ops.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key LIKE ?", ("tree-skew:%",),
        ).fetchone()[0]
    finally:
        ops.close()
    assert guard_cards == 1, f"no 117th guard card: {guard_cards} guard cards on the board"


# ---------------------------------------------------------------------------------------------
# R-1..R-4 (card t_5a54ca29) -- SURVIVAL is the IMPORTED content coming back, never the disk
# ---------------------------------------------------------------------------------------------


def _dead_pid(monkeypatch, pid):
    """Make exactly ``pid`` answer the liveness contract's PROVEN-DEAD verdict."""
    real = kbd._pid_liveness
    monkeypatch.setattr(
        kbd, "_pid_liveness",
        lambda other: kbd.WORKER_DEAD if int(other) == int(pid) else real(other),
    )


def _escalation_rows():
    conn = kbc.connect(board=OPS_BOARD)
    try:
        return conn.execute(
            "SELECT id, assignee, status, body, idempotency_key FROM tasks "
            "WHERE idempotency_key LIKE ? ORDER BY rowid",
            (f"{kbd.TICK_YIELD_ESCALATION_KEY_PREFIX}%",),
        ).fetchall()
    finally:
        conn.close()


def _persist_prior_boot(pid, fingerprint, *, identity="boot-witness|1200|340000"):
    """The filer's OWN persisted boot record -- what certifies that today's pid is that process."""
    directory = tf.boot_records_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{int(pid)}.json").write_text(
        json.dumps({
            "fingerprint": fingerprint,
            "pid": int(pid),
            "tree": None,
            "recorded_at": 1,
            "identity": identity,
        }),
        encoding="utf-8",
    )


def _seed_prior_guard_event(
    *, boot_fingerprint, disk_fingerprint, pid, status="ready", completed_at=None,
    first_tick=None, persist_boot=True,
):
    """A prior guard card, plus the event the shipped door writes for it.

    Seeded rather than driven so a test can pick the ONE field under test -- the imported content,
    the pid's liveness verdict, the card's status and its clearance time -- without the other two
    having to be produced by a whole episode first. ``persist_boot`` writes the filer's own boot
    record, without which the pid is NOT certified as that process (R-2, recycled-pid guard).
    """
    first_tick = int(first_tick or 1)
    if persist_boot:
        _persist_prior_boot(pid, boot_fingerprint)
    conn = kbc.connect(board=OPS_BOARD)
    try:
        card_id = kb.create_task(
            conn,
            title=kbd.TICK_YIELD_ACTOR_TITLE,
            body="prior guard card (seeded by the R-1..R-4 tests)",
            assignee=kbd.TICK_YIELD_ACTOR_ASSIGNEE,
            created_by="kanban-dispatcher",
            idempotency_key=(
                f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}{boot_fingerprint}:{first_tick}"
            ),
        )
        with kb.write_txn(conn):
            kb._append_event(conn, card_id, kbd.TICK_YIELD_ACTOR_EVENT, {
                "boot_fingerprint": boot_fingerprint,
                "disk_fingerprint": disk_fingerprint,
                "boot_pid": int(pid),
                "sig": boot_fingerprint,
                "streak": THRESHOLD,
                "first_tick": first_tick,
                "last_tick": first_tick,
                "window_start": first_tick,
                "status_record": "(seeded)",
                "board": OPS_BOARD,
                "filed_on": OPS_BOARD,
                "escalation_ref": None,
            })
            conn.execute(
                "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
                (status, int(completed_at) if completed_at is not None else None, card_id),
            )
        return card_id
    finally:
        conn.close()


def _close_card(task_id, *, completed_at):
    """A human closed the card -- the state R-3 must survive without swallowing the next episode."""
    conn = kbc.connect(board=OPS_BOARD)
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (int(completed_at), task_id),
            )
    finally:
        conn.close()


def test_r1_the_incident_replay_is_not_a_surviving_skew(board, tree, monkeypatch):
    """R-1. b1b9cb6 -> 47558cf inside five minutes: the SAME disk, a DIFFERENT imported content.

    The disk fingerprint is the one thing a reload does NOT change, so a prior event that measured
    this same checkout proves nothing about whether a reload landed. The prior card here is left
    OPEN and filed by a pid that is proven dead, so R-2 cannot be what rejects it: the imported
    content is the only difference, which is the whole point. The ordinary acting card is the right
    object for a re-skew onto a revision the fleet had already left -- not a design escalation
    citing a card from an earlier story.
    """
    conn = board
    tf.record_boot()
    imported = tf.boot_record()["fingerprint"]
    _edit(tree)
    _tick(conn)  # one refusal: this is the disk content the fleet is now serving
    state = _status_record()[1]
    disk = state["disk_fingerprint"]
    assert disk != imported, "the fixture's edit must move the disk content"

    dead = os.getpid() + 900001
    prior = _seed_prior_guard_event(
        boot_fingerprint="tree-content:" + "c" * 64,  # an EARLIER story's imported content
        disk_fingerprint=disk,                        # the same checkout this tick measures
        pid=dead,
        status="ready",
        first_tick=int(state["first_tick"]) - 900,
    )
    _dead_pid(monkeypatch, dead)

    for _ in range(THRESHOLD):
        _tick(conn)

    assert _escalation_rows() == [], (
        "a prior event on the same DISK content is not a surviving skew when the IMPORTED content "
        "differs (R-1): this is the fleet leaving a revision and coming back to it"
    )
    actors = [row for row in _actor_rows() if row["id"] != prior]
    assert len(actors) == 1, f"the ordinary acting card is the right object here, got {actors}"
    assert actors[0]["idempotency_key"] == (
        f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}{imported}:{state['first_tick']}"
    ), "the acting card is keyed on THIS episode's imported content and window"


def test_r2_a_card_cleared_before_this_process_booted_is_not_survival(board, tree, monkeypatch):
    """R-2. Same imported content, same proven-dead pid -- but that card was CLEARED before boot.

    Without R-2 the match is indistinguishable from a broken reload door, and every re-entry cites
    a card that was answered hours ago. The clearance time against this process's boot
    ``recorded_at`` is what separates "the reload landed and the skew is back" (escalate) from
    "this episode began after the last one was closed" (file the ordinary card).
    """
    conn = board
    tf.record_boot()
    imported = tf.boot_record()["fingerprint"]
    booted_at = int(tf.boot_record()["recorded_at"])
    _edit(tree)
    _tick(conn)  # one refusal: the disk content the earlier episode had also measured
    disk = _status_record()[1]["disk_fingerprint"]

    dead = os.getpid() + 900002
    prior = _seed_prior_guard_event(
        boot_fingerprint=imported,
        disk_fingerprint=disk,          # the SAME checkout: R-1 and R-2 both let it through
        pid=dead,
        status="done",
        completed_at=booted_at - 600,   # cleared ten minutes BEFORE this process booted
        first_tick=booted_at - 900,
    )
    _dead_pid(monkeypatch, dead)

    for _ in range(THRESHOLD):
        _tick(conn)

    assert _escalation_rows() == [], (
        "a card cleared BEFORE this process booted is a previous episode, not a surviving reload "
        "(R-2): re-filing it as survival is how one card becomes 116"
    )
    actors = [row for row in _actor_rows() if row["id"] != prior]
    assert len(actors) == 1, f"this episode gets its own acting card, got {actors}"


def test_r2_an_uncorroborated_filer_is_not_proven_dead(board, tree, monkeypatch):
    """R-2, fail closed. No persisted boot record for the filer: NOT certified as that process.

    A pid NUMBER is only evidence while it still means the same process, so the prior card is
    corroborated against its filer's own boot record. Without one, the board cannot tell a dead
    filer from a recycled pid -- and an unreadable claim does not carry a design escalation.
    """
    conn = board
    tf.record_boot()
    imported = tf.boot_record()["fingerprint"]
    _edit(tree)

    dead = os.getpid() + 900005
    prior = _seed_prior_guard_event(
        boot_fingerprint=imported,
        disk_fingerprint="tree-content:" + "e" * 64,
        pid=dead,
        status="ready",
        first_tick=1,
        persist_boot=False,             # nothing on disk says this pid ever booted
    )
    _dead_pid(monkeypatch, dead)
    assert tf.recorded_boot(dead) is None

    for _ in range(THRESHOLD):
        _tick(conn)

    assert _escalation_rows() == [], (
        "an uncorroborated filer is not PROVEN DEAD, so it cannot carry an escalation (R-2)"
    )
    assert [row["id"] for row in _actor_rows()] == [prior], (
        "the ordinary acting path carries it: the open guard card absorbs, no escalation"
    )


def test_r3_a_reload_onto_the_same_imported_content_escalates_once_per_episode(
    board, tree, monkeypatch, _escalation_door,
):
    """R-3 + R-4. The GENUINE door failure: same imported content, dead filer, open card.

    Exactly one escalation, keyed on the imported content and THIS window's first tick, and its
    body quotes the three facts R-4 demands -- each printed beside the pid that measured it -- with
    the disk half labelled as evidence. The 116-card sentence stays; the "carried the SAME on-disk
    content" sentence, refuted by R-1, does not.
    """
    conn = board
    tf.record_boot()
    imported = tf.boot_record()["fingerprint"]
    _edit(tree)
    _tick(conn)
    state = _status_record()[1]
    first_tick = int(state["first_tick"])

    dead = os.getpid() + 900003
    prior = _seed_prior_guard_event(
        boot_fingerprint=imported,                 # the SAME imported content
        disk_fingerprint=state["disk_fingerprint"],
        pid=dead,
        status="ready",                            # not closed: R-2 cannot reject it
        first_tick=first_tick - 30,
    )
    _dead_pid(monkeypatch, dead)

    for _ in range(THRESHOLD - 1):
        _tick(conn)

    rows = _escalation_rows()
    assert len(rows) == 1, f"exactly ONE escalation for this episode, got {rows}"
    escalation = rows[0]
    assert escalation["idempotency_key"] == (
        f"{kbd.TICK_YIELD_ESCALATION_KEY_PREFIX}{imported}:{first_tick}"
    ), "the key is the IMPORTED content plus THIS window's first tick (R-3)"
    assert escalation["assignee"] == kbd.TICK_YIELD_ESCALATION_ASSIGNEE == "platform-stl"

    body = escalation["body"]
    assert prior in body, "the body must NAME the prior card it matched"
    assert imported in body, "this process's imported content, verbatim"
    assert state["disk_fingerprint"] in body, "the measured disk content, verbatim (as evidence)"
    assert str(dead) in body, "the pid that filed the prior card, verbatim"
    assert "status `ready`" in body and "(not closed)" in body
    assert "take NO part in the identity" in body, "the disk half must be labelled EVIDENCE"
    assert "carried the SAME on-disk content" not in body, "the claim R-1 refuted is gone"
    assert "116" in body, "the pile this card exists to stop is still named"

    assert len(_actor_rows()) == 1, "the escalation REPLACES the guard card, it does not sit beside it"

    # A repeat refusal INSIDE the same episode returns the same card -- never a second one.
    for _ in range(2):
        _tick(conn)
    assert [row["id"] for row in _escalation_rows()] == [escalation["id"]]


def test_r3_a_closed_escalation_never_swallows_the_next_episode(board, tree, monkeypatch):
    """R-3. The window is in the key, so a NEW episode mints a NEW key and a NEW card.

    Keyed on the disk fingerprint alone, a closed escalation absorbed the next stall for as long as
    that checkout stayed frozen -- and in the incident the cited card had been closed months
    earlier. Closing this episode's card and letting the gate clear must not silence the next one.
    """
    import time as real_time

    clock = _Clock(real_time)
    monkeypatch.setattr(kbd, "time", clock)
    conn = board
    tf.record_boot()
    imported = tf.boot_record()["fingerprint"]
    _edit(tree)
    _tick(conn)
    first_window = int(_status_record()[1]["first_tick"])

    dead = os.getpid() + 900004
    _seed_prior_guard_event(
        boot_fingerprint=imported,
        disk_fingerprint=_status_record()[1]["disk_fingerprint"],
        pid=dead,
        status="ready",
        first_tick=first_window - 30,
    )
    _dead_pid(monkeypatch, dead)
    for _ in range(THRESHOLD - 1):
        _tick(conn)
    first = _escalation_rows()
    assert len(first) == 1, first
    _close_card(first[0]["id"], completed_at=first_window + 30)

    # The episode CLEARS: the checkout is rewound to the imported content and the tick serves.
    clock.offset += 60
    _edit(tree, body="VALUE = 1\n")
    assert _tick(conn).tick_yielded == "", "the rewind must let the tick serve"
    assert _status_record()[1] is None, "a serving tick retires the refused-tick record"

    # ...and the SAME imported content is skewed again: a NEW window, a NEW episode.
    clock.offset += 60
    _edit(tree, body="VALUE = 2\n# re-entry\n")
    for _ in range(THRESHOLD):
        _tick(conn)

    rows = _escalation_rows()
    assert len(rows) == 2, f"the next episode must file its OWN escalation, got {rows}"
    second = rows[1]
    assert second["id"] != first[0]["id"]
    assert second["idempotency_key"] != first[0]["idempotency_key"]
    assert second["idempotency_key"] == (
        f"{kbd.TICK_YIELD_ESCALATION_KEY_PREFIX}{imported}:"
        f"{_status_record()[1]['first_tick']}"
    ), "the new card keys from ITS OWN first tick (R-3)"
    assert second["status"] not in ("done", "archived"), "the new escalation is live"


# ---------------------------------------------------------------------------------------------
# card t_028ad743 -- the two shapes the retirement doors could not close
# ---------------------------------------------------------------------------------------------


def _card_kinds(conn, task_id):
    return [
        row["kind"] for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
    ]


def _ops_card_kinds(task_id):
    conn = kbc.connect(board=OPS_BOARD)
    try:
        return _card_kinds(conn, task_id)
    finally:
        conn.close()


def test_a_guard_filed_on_the_FALLBACK_board_is_retired_by_a_served_tick(board, tree):
    """Card t_028ad743: the fallback-filed duplicate (the fleet's t_35f10525).

    A guard filed while the ops board was UNOPENABLE lands on the board that was merely TICKING --
    with our own ``tree-skew:`` key, but with no ``tree_skew_actor_filed`` event and a title no seat
    would recognise. The narrow door selects by that EVENT and the broad one by the EXACT TITLE on
    the ACTOR board, so this card was invisible to both: immortal, and re-dispatched every tick.
    Its own key says it is OURS, so a served tick must retire it wherever it was filed.
    """
    conn = board
    task_id = kb.create_task(
        conn,
        title="[guard] mixed revision -- DUPLICATE, filed on the fallback board",
        body="filed only because the ops board was unopenable",
        assignee=kbd.TICK_YIELD_ACTOR_ASSIGNEE,
        created_by="kanban-dispatcher",
        idempotency_key=(
            f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}tree-content:{'b' * 64}:1792800954"
        ),
    )
    # ``blocked`` is the fleet's own shape for a fallback-filed guard (t_4a8ae67f, t_9ed0a953): not
    # spawnable, and NOT completable -- so the narrow door's ``complete_task`` refused it with
    # ``cause='not_running'`` on every tick and it stayed live forever.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,))
    assert kbd.TICK_YIELD_ACTOR_EVENT not in _card_kinds(conn, task_id), (
        "the defect is a card carrying the KEY but no filing EVENT"
    )

    # The tree on disk is the tree this process imported: the tick SERVES, and a served tick is
    # the proof that the episode the card records is over.
    tf.record_boot()
    result = _tick(conn)
    assert result.tick_yielded == "", result.tick_yielded

    row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["status"] == "done", (
        "a card carrying our own tree-skew key is ours to retire on whatever board it was filed"
    )
    assert "[retired] episode cleared at" in (row["result"] or ""), row["result"]
    assert kbd.TICK_YIELD_RETIRED_EVENT in _card_kinds(conn, task_id)


@pytest.mark.parametrize("status", ["ready", "scheduled", "blocked", "todo"])
def test_a_cleared_guard_is_retired_from_EVERY_non_terminal_status(board, tree, status):
    """Card t_028ad743 (class guardrail): ANY live status ends TERMINAL, not just running/done.

    The retirement doors used to close only what the kernel already accepted: a ``scheduled`` or
    ``blocked`` guard was refused with ``cause='not_running'`` and a ``ready`` one was claimed by the
    tick before the close. Each status is taken to terminal through ITS OWN kernel door --
    ``promote_task`` for ``todo``, ``unblock_task`` for ``blocked``/``scheduled``, ``complete_task``
    for all of them -- never by writing ``status`` directly.
    """
    conn = board
    ops = kbc.connect(board=OPS_BOARD)
    try:
        task_id = kb.create_task(
            ops,
            title="[guard] cleared episode, parked in a status the close used to refuse",
            body="seeded by card t_028ad743's class guardrail",
            assignee=kbd.TICK_YIELD_ACTOR_ASSIGNEE,
            created_by="kanban-dispatcher",
            idempotency_key=(
                f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}tree-content:{status[0] * 64}:1792800954"
            ),
        )
        with kb.write_txn(ops):
            ops.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    finally:
        ops.close()
    assert _actor_status(task_id) == status

    # The tree on disk is the tree this process imported: the tick is serving, so the episode the
    # card records is over.
    tf.record_boot()
    result = _tick(conn)
    assert result.tick_yielded == "", result.tick_yielded

    assert _actor_status(task_id) == "done", f"a cleared guard in {status!r} must end terminal"
    assert kbd.TICK_YIELD_RETIRED_EVENT in _ops_card_kinds(task_id)


def test_a_guard_the_kernel_keeps_refusing_GIVES_UP_once_instead_of_warning_forever(board, tree):
    """Card t_028ad743: bounded retries, then give up OUT LOUD and ONCE.

    A guard the kernel will never close -- here a ``todo`` card whose PARENT is unfinished, so the
    kernel's own ``promote_task`` refuses it -- used to be re-attempted and re-warned on EVERY tick,
    forever. Now every refusal is a ``..._retire_refused`` event ON the card (so the bound survives
    a respawned dispatcher), and at ``TICK_YIELD_RETIRE_MAX_ATTEMPTS`` ONE ``..._retire_failed``
    event names the last refusal, after which the card leaves every sweep: no new events, and no
    further warnings.
    """
    conn = board
    ops = kbc.connect(board=OPS_BOARD)
    try:
        parent_id = kb.create_task(
            ops,
            title="[guard] an unfinished parent (keeps the guard out of ready)",
            body="seeded by card t_028ad743's bound test",
            created_by="kanban-dispatcher",
        )
        with kb.write_txn(ops):
            ops.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent_id,))
        task_id = kb.create_task(
            ops,
            title="[guard] a shape the kernel will not take",
            body="seeded by card t_028ad743's bound test",
            assignee=kbd.TICK_YIELD_ACTOR_ASSIGNEE,
            created_by="kanban-dispatcher",
            parents=(parent_id,),
            idempotency_key=(
                f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}tree-content:{'c' * 64}:1792800954"
            ),
        )
    finally:
        ops.close()
    assert _actor_status(task_id) == "todo", "an unfinished parent parks the guard in todo"

    tf.record_boot()
    for _ in range(kbd.TICK_YIELD_RETIRE_MAX_ATTEMPTS):
        assert _tick(conn).tick_yielded == ""

    kinds = _ops_card_kinds(task_id)
    assert kinds.count(kbd.TICK_YIELD_RETIRE_FAILED_EVENT) == 1, (
        f"the sweep must give up ONCE, in a durable event; got {kinds}"
    )
    assert kinds.count(kbd.TICK_YIELD_RETIRE_REFUSED_EVENT) <= (
        kbd.TICK_YIELD_RETIRE_MAX_ATTEMPTS - 1
    ), f"retries are BOUNDED; got {kinds}"

    # ... and it stays quiet: a given-up card is an audit finding, not a per-tick siren.
    before = kinds
    assert _tick(conn).tick_yielded == ""
    assert _ops_card_kinds(task_id) == before, (
        "a card that gave up must not be re-attempted tick after tick"
    )
    assert _actor_status(task_id) == "todo", "a refused card keeps its status"
