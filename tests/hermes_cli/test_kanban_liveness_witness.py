"""One liveness witness, three answers.

The 2026-09-26 false-reclaim tick declared six live workers dead inside ONE dispatch tick and spawned
a duplicate beside each (``EVIDENCE-blast-radius.txt``). Two probes answered "dead" where the honest
answer was "cannot tell": a secondary ``ps`` that exited non-zero, and a start-time read that returned
nothing (``EVIDENCE-reproduce-defect.txt``). The recorded start also moved by ~9 s for a live process
because the macOS boot reference it was built from was adjusted mid-flight
(``EVIDENCE-reference-change.txt``).

These tests pin the contract that follows from that: a liveness read is ``alive`` / ``dead`` /
``unknown``, only PROOF of death releases a claim or authorizes a signal, and an unprovable read holds
the claim. They also pin the run fence that rides on the verdict, and the recovery that records a
reclaim honestly instead of leaving a crash as the attempt history's last word.
"""

import json
import os
import signal
import subprocess
import time

import pytest

from gateway import status as gw_status
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _claimed_running(conn, *, pid=None, started_at, max_runtime=None) -> str:
    """A ``running`` card claimed by a host-local worker whose pid/fingerprint we control."""
    tid = kb.create_task(conn, title="job", assignee="worker", max_runtime_seconds=max_runtime)
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid if pid is not None else os.getpid())
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_started_at = ?, started_at = ?, claim_expires = ? WHERE id = ?",
            (started_at, old, old, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (old, tid),
        )
    return tid


# The two fingerprints a deployed board carries that can no longer be compared: the pre-witness shape
# (a bare start tick) and the macOS shape whose boot-witness component is empty. Neither can be
# salvaged, because for both of them "the value differs" is indistinguishable from "the boot reference
# was adjusted" — so both must read UNKNOWN, never DEAD.
#
# The last two entries are the ESTATE's own rows, not invented: on 2026-09-29 the live dispatch path
# regressed to pre-witness bytes and every spawn from 01:02:10 to 12:36:53 recorded the 2-field
# empty-witness ABSOLUTE-epoch form (`current_instantiation_epoch()` is "" on macOS, so the pre-fix
# writer's `f"{epoch}|{get_process_start_time(pid)}"` collapses to `|<absolute epoch cs>`).
# Runs 5642 (pid 94448) and 5643 (pid 78994) carry these exact values.
UNCOMPARABLE_FINGERPRINTS = [
    "179041285600",
    "|1790412856",
    179041285600,
    "|179068367989",  # 2026-09-29 run 5642, pid 94448
    "|179068536576",  # 2026-09-29 run 5643, pid 78994
]


def test_pid_liveness_answers_three_ways():
    """``dead`` needs proof; a probe that could not read the process is not proof."""
    assert kbd._pid_liveness(os.getpid()) == kbd.WORKER_ALIVE
    assert kbd._pid_liveness(9_999_999) == kbd.WORKER_DEAD
    assert kbd._pid_liveness(None) == kbd.WORKER_DEAD
    assert kbd._pid_liveness(0) == kbd.WORKER_DEAD


def test_secondary_probe_exit_status_is_unknown_never_death(monkeypatch):
    """BRANCH B of the reproduction: ``ps`` exits non-zero for a PID that ``kill(0)`` says is live.

    ``ps`` also exits 1 for a PID it cannot see under load, so the only honest answer is ``unknown``;
    reading it as death is what let one tick reclaim six live cards.
    """
    fake = subprocess.CompletedProcess(args=["ps"], returncode=1, stdout="")
    monkeypatch.setattr(kbd.subprocess, "run", lambda *a, **k: fake)
    assert kbd._pid_liveness(os.getpid()) == kbd.WORKER_UNKNOWN

    # A worker whose identity DOES match stays alive: the witness/start agreement is proof on its own,
    # and a probe that could not read the process must not overrule it.
    real = kbd._process_fingerprint(os.getpid())
    assert kbd._worker_liveness(os.getpid(), real) == kbd.WORKER_ALIVE

    # An identity that cannot be compared is HELD rather than released beside the live process.
    assert kbd._worker_liveness(os.getpid(), "179041285600") == kbd.WORKER_UNKNOWN
    assert kbd._worker_not_dead(os.getpid(), "179041285600") is True

    # A foreign boot witness of the SAME form is proof of its own: that worker is not ours.
    monkeypatch.setattr(gw_status, "host_boot_witness", lambda: "bootsession:THIS-HOST")
    assert kbd._worker_liveness(os.getpid(), "bootsession:SOME-OTHER-BOOT|1") == kbd.WORKER_DEAD

    # A witness that came back in a DIFFERENT form is not proof of a reboot: the probe that yields it
    # (`sysctl` above, under the same `ps` outage) changed its answer's SHAPE, not the boot. Reading
    # that as "different boot" would declare every live row dead in one tick — the incident itself.
    monkeypatch.setattr(gw_status, "host_boot_witness", lambda: "boottime:179041285600")
    assert kbd._worker_liveness(os.getpid(), "bootsession:SOME-OTHER-BOOT|1") == kbd.WORKER_UNKNOWN


@pytest.mark.platforms("macos")
def test_boot_reference_shift_does_not_move_the_fingerprint(monkeypatch):
    """The reproduction's ~9 s drift: psutil imported before the boot reference was adjusted.

    ``psutil._psosx.adjust_proc_create_time`` shifts ``create_time()`` by ``|INIT_BOOT_TIME - boot|``
    for the rest of that process's life, so two readers of the SAME live process disagree. The
    boot-relative accessor undoes that shift, and the fingerprint it produces is identical either way.
    """
    import importlib

    psosx = importlib.import_module("psutil._psosx")
    original = psosx.INIT_BOOT_TIME
    assert original, "psutil exposes no boot reference to shift"
    before = gw_status.get_process_uptime_start_time(os.getpid())
    assert before is not None
    try:
        psosx.INIT_BOOT_TIME = original + 9
        shifted = gw_status.get_process_uptime_start_time(os.getpid())
    finally:
        psosx.INIT_BOOT_TIME = original
    assert shifted == before
    assert abs(shifted - before) <= gw_status.START_TIME_DRIFT_TOLERANCE


def test_start_time_read_failure_is_unknown_and_holds_the_claim():
    """BRANCH A of the reproduction: the start-time read fails after a good fingerprint was recorded.

    The live worker is not "recycled" — nothing was read, which is not a mismatch. Every path that
    could release or signal it must hold instead.
    """
    assert kbd._worker_liveness(os.getpid(), None) == kbd.WORKER_ALIVE  # legacy NULL row: existence
    real = kbd._process_fingerprint(os.getpid())
    assert real is not None

    original = gw_status._get_process_start_time
    try:
        gw_status._get_process_start_time = lambda pid: None
        assert kbd._worker_liveness(os.getpid(), real) == kbd.WORKER_UNKNOWN
        assert kbd._worker_alive(os.getpid(), real) is False
        assert kbd._worker_not_dead(os.getpid(), real) is True
    finally:
        gw_status._get_process_start_time = original


@pytest.mark.parametrize("fingerprint", UNCOMPARABLE_FINGERPRINTS)
def test_an_incomparable_fingerprint_is_held_never_declared_dead(board, fingerprint):
    """The deploy-time consequence of the format change, pinned deliberately.

    Every row running at deploy time carries one of these shapes and is therefore HELD until it turns
    over. That is the safe direction: the alternative is declaring live work dead, which is the defect.
    """
    conn = board
    tid = _claimed_running(conn, started_at=fingerprint, max_runtime=1)
    assert kbd._worker_liveness(os.getpid(), fingerprint) == kbd.WORKER_UNKNOWN
    assert kbd._worker_alive(os.getpid(), fingerprint) is False
    assert kbd._worker_not_dead(os.getpid(), fingerprint) is True

    killed = []
    sig = lambda pid, s: killed.append((pid, s))  # noqa: E731
    assert kbd.enforce_max_runtime(conn, signal_fn=sig) == []
    assert kb.release_stale_claims(conn, signal_fn=sig) == 0
    assert killed == []
    assert kb.get_task(conn, tid).status == "running"


def test_one_tick_reclaims_no_live_worker_and_spawns_no_duplicate(board):
    """The board-chain scenario: six cards, one dispatch tick, every worker alive.

    Six reclaims in a single tick is the false-reclaim signature; a correct kernel reclaims NONE of
    them and leaves each claim standing so the dispatcher cannot spawn a duplicate beside it.
    """
    conn = board
    # Six cards, one tick: the count is the assertion, so build it explicitly rather than from the
    # maintained fingerprint inventory (which grows as the estate produces new incident shapes).
    six_fingerprints = UNCOMPARABLE_FINGERPRINTS[:3] * 2
    tids = [_claimed_running(conn, started_at=fingerprint, max_runtime=1)
            for fingerprint in six_fingerprints]
    assert len(tids) == 6

    assert kbd.detect_crashed_workers(conn) == []
    assert kb.release_stale_claims(conn) == 0
    assert kbd.reconcile_orphaned_running(conn) == []
    for tid in tids:
        assert kb.get_task(conn, tid).status == "running"
        assert kb.get_task(conn, tid).worker_pid == os.getpid()


def test_orphan_reconcile_defers_an_unprovable_worker_and_records_why(board):
    """An orphan whose identity cannot be read must NOT be requeued, and the deferral must be
    recorded — that record is what a later recovery reads before it deletes anything."""
    conn = board
    tid = _claimed_running(conn, started_at="|1790412856")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_lock = NULL, claim_expires = NULL WHERE id = ?", (tid,))

    assert kbd.reconcile_orphaned_running(conn) == []
    assert kb.get_task(conn, tid).status == "running"
    events = [e for e in kb.list_events(conn, tid) if e.kind == "reclaim_deferred"]
    assert events, "the deferral must be recorded, not silent"
    payload = events[-1].payload
    assert payload["reason"] == "orphaned_running_worker_alive"
    assert payload["liveness"] == kbd.WORKER_UNKNOWN


def test_recovery_reconciliation_needs_a_disowned_premise(board):
    """History a worker recorded is never rewritten; only a proven-infrastructure abandonment
    is reconciled, and the reconciliation attributes nothing to the worker."""
    conn = board
    worker_owned = _claimed_running(conn, started_at=kbd._process_fingerprint(os.getpid()))
    assert kb._synthesize_empty_completion_run(conn, worker_owned) is None
    assert len(kb.list_runs(conn, worker_owned) if hasattr(kb, "list_runs") else
               conn.execute("SELECT id FROM task_runs WHERE task_id = ?", (worker_owned,)).fetchall()) == 1

    disowned = _claimed_running(conn, started_at="|1790412856")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET outcome = 'reclaimed', status = 'reclaimed', metadata = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            ('{"reason": "orphaned_running"}', disowned),
        )
        conn.execute("UPDATE tasks SET worker_pid = NULL, claim_lock = NULL WHERE id = ?", (disowned,))
    run_id = kb._synthesize_empty_completion_run(conn, disowned)
    assert run_id is not None
    row = conn.execute("SELECT outcome, summary, metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["outcome"] == "completed"
    assert row["summary"] in (None, ""), "the recovery attributes no prose to the worker"
    assert "'reason': 'orphaned_running'" not in (row["metadata"] or ""), "must not look disowned itself"



# The A/B subset: these read only API that exists on the BASE commit, so this file can be run
# against the base blob set (worktree) to show the failure each one fixes. The tests above are the
# contract; these are the ones that go red without the change.
def _failing_ps(monkeypatch):
    """The secondary macOS probe exits non-zero under load -- the live evidence's BRANCH B."""
    real_run = subprocess.run

    def _run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "ps":
            return subprocess.CompletedProcess(args=list(cmd), returncode=1, stdout="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(kbd.subprocess, "run", _run)


@pytest.mark.platforms("macos")
def test_the_start_time_accessor_does_not_move_with_the_boot_reference():
    """BRANCH A at the source: the 9 s offsets in the evidence are psutil's own adjustment."""
    psosx = pytest.importorskip("psutil._psosx")
    before = gw_status.get_process_start_time(os.getpid())
    assert before is not None
    original = psosx.INIT_BOOT_TIME
    try:
        psosx.INIT_BOOT_TIME = original + 9  # psutil imported before the reference moved by 9 s
        after = gw_status.get_process_start_time(os.getpid())
        shifted = gw_status.get_process_uptime_start_time(os.getpid())
    finally:
        psosx.INIT_BOOT_TIME = original
    assert after is not None and shifted is not None
    # The absolute accessor is the one that moves (its callers keep needing epoch time; the fix leaves
    # its semantics alone) — that movement is the incident, and it is what the pair below replaces.
    assert abs(before - after) > gw_status.START_TIME_DRIFT_TOLERANCE
    assert shifted == gw_status.get_process_uptime_start_time(os.getpid())


@pytest.mark.platforms("macos")
def test_a_failed_secondary_probe_holds_the_claim(board, monkeypatch):
    """BRANCH B end to end: a `ps` that fails must not release a live worker's claim."""
    conn = board
    tid = _claimed_running(conn, pid=os.getpid(), started_at=kbd._process_fingerprint(os.getpid()))
    _failing_ps(monkeypatch)
    assert kb.release_stale_claims(conn) == 0
    assert kb.get_task(conn, tid).status == "running"
    row = conn.execute("SELECT worker_pid FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["worker_pid"] == os.getpid()


@pytest.mark.platforms("macos")
def test_the_reclaim_tick_does_not_take_six_live_workers(board, monkeypatch):
    """The board-chain signature of 2026-09-26: six live cards declared dead in ONE tick."""
    conn = board
    ids = [
        _claimed_running(conn, pid=os.getpid(), started_at=kbd._process_fingerprint(os.getpid()))
        for _ in range(6)
    ]
    _failing_ps(monkeypatch)
    assert kbd.detect_crashed_workers(conn) == []
    assert [kb.get_task(conn, t).status for t in ids] == ["running"] * 6


def test_a_legacy_fingerprint_does_not_release_the_claim(board):
    """The accepted deploy consequence: an old-shape row is HELD, never declared dead."""
    conn = board
    tid = _claimed_running(conn, pid=os.getpid(), started_at=1)
    assert kb.release_stale_claims(conn) == 0
    assert kb.get_task(conn, tid).status == "running"


def _fingerprint_reference_and_start(fingerprint: "str | None") -> tuple[int, int]:
    """``(boot reference, start)`` from a recorded ``"<witness>|<reference>|<start>"`` fingerprint."""
    assert fingerprint is not None
    _, reference, start = fingerprint.split("|")
    return int(reference), int(start)


# ---------------------------------------------------------------------------
# 2026-09-27: the boot REFERENCE moved between the recording and the read
# ---------------------------------------------------------------------------
# Two cards (t_02653675 pid 10079, t_2ad1da22 pid 25285) were reclaimed as "pid N not alive" inside the
# 06:06:04 tick while both workers were running and heartbeating. Their fingerprints were recorded at
# 05:18 and 05:38, before macOS moved ``kern.boottime`` 800 cs forward (it is recomputed so that uptime
# stays continuous): pid 25285 recorded start 19191323, live reading 19190523 at this tick — exactly
# the move, 4× the 200 cs tolerance. The reference a start is expressed against therefore travels WITH
# the fingerprint, and a mismatch across a moved reference is not evidence of a recycled PID.
INCIDENT_WITNESS = "bootsession:3C88AF02-5EDE-41E7-AEEA-A79209AA30EB"
INCIDENT_REFERENCE_CS = 179030881300
INCIDENT_RECORDED_START_CS = 19191323
INCIDENT_MOVED_REFERENCE_CS = 179030882100
INCIDENT_LIVE_START_CS = 19190523


def _incident_reading(monkeypatch) -> None:
    """The live half of the 06:06:04 read: same boot, reference 800 cs forward, start 800 cs back."""
    monkeypatch.setattr(gw_status, "host_boot_witness", lambda: INCIDENT_WITNESS)
    monkeypatch.setattr(
        gw_status, "get_process_uptime_fingerprint",
        lambda pid: (INCIDENT_MOVED_REFERENCE_CS, INCIDENT_LIVE_START_CS),
    )


def test_the_20260927_false_dead_read_is_refused(monkeypatch):
    """The recorded case, literally: the mismatch IS the reference move, so the verdict is ``unknown``.

    ``unknown`` holds the claim, so the single worker keeps its card and finishes normally — the
    alternative (``dead``) is what released t_2ad1da22's claim and spawned a second worker beside it.
    """
    recorded = f"{INCIDENT_WITNESS}|{INCIDENT_REFERENCE_CS}|{INCIDENT_RECORDED_START_CS}"
    _incident_reading(monkeypatch)
    assert kbd._worker_liveness(os.getpid(), recorded) == kbd.WORKER_UNKNOWN
    assert kbd._worker_alive(os.getpid(), recorded) is False
    assert kbd._worker_not_dead(os.getpid(), recorded) is True

    # Proof of death is still proof: the same row once the PID is actually gone releases normally.
    monkeypatch.setattr(kbd, "_pid_liveness", lambda pid: kbd.WORKER_DEAD)
    assert kbd._worker_liveness(os.getpid(), recorded) == kbd.WORKER_DEAD


def test_the_20260927_false_dead_read_cannot_release_a_claim(board, monkeypatch):
    """End to end on a board: every path that could release or signal holds, and no duplicate spawns."""
    conn = board
    recorded = f"{INCIDENT_WITNESS}|{INCIDENT_REFERENCE_CS}|{INCIDENT_RECORDED_START_CS}"
    tid = _claimed_running(conn, started_at=recorded, max_runtime=1)
    _incident_reading(monkeypatch)

    killed = []
    sig = lambda pid, s: killed.append((pid, s))  # noqa: E731
    assert kbd.detect_crashed_workers(conn) == []
    assert kb.release_stale_claims(conn, signal_fn=sig) == 0
    assert kbd.reconcile_orphaned_running(conn) == []
    assert kbd.enforce_max_runtime(conn, signal_fn=sig) == []
    assert killed == []
    task = kb.get_task(conn, tid)
    assert task.status == "running"
    assert task.worker_pid == os.getpid()


def test_a_held_reference_still_proves_a_recycled_pid(board):
    """The strictness that must survive the fix: same reference, starts apart → NOT our worker.

    A recycled PID is the case this branch exists for, and the reference is what distinguishes it from
    a moved one: while the reference holds, a start mismatch is a difference.
    """
    conn = board
    reference, start = _fingerprint_reference_and_start(kbd._process_fingerprint(os.getpid()))
    recycled = f"{gw_status.host_boot_witness()}|{reference}|{start - 5_000}"
    assert kbd._worker_liveness(os.getpid(), recycled) == kbd.WORKER_DEAD
    assert kbd._worker_not_dead(os.getpid(), recycled) is False

    tid = _claimed_running(conn, started_at=recycled)
    task = kb.get_task(conn, tid)
    assert kbd.boot_reference_held(reference, reference) is True
    assert kbd.boot_reference_held(reference, reference + 800) is False
    assert kbd.boot_reference_held(None, reference) is False
    assert task.status == "running"  # this row's own worker is real; only its identity is foreign


def test_a_fingerprint_without_a_reference_is_held_on_mismatch(board):
    """The pre-existing shape at deploy time: an exact match still certifies, a mismatch never does.

    Rows written before this change carry witness+start only. The reference they were built against is
    not in the row, so a mismatch is indistinguishable from a moved reference — held, never dead.
    """
    conn = board
    reference, start = _fingerprint_reference_and_start(kbd._process_fingerprint(os.getpid()))
    witness = gw_status.host_boot_witness()
    assert reference != 0, "macOS records a real boot reference"
    assert kbd._worker_liveness(os.getpid(), f"{witness}|{start}") == kbd.WORKER_ALIVE
    assert kbd._worker_liveness(os.getpid(), f"{witness}|{start - 5_000}") == kbd.WORKER_UNKNOWN
    assert kbd._worker_not_dead(os.getpid(), f"{witness}|{start - 5_000}") is True

    tid = _claimed_running(conn, started_at=f"{witness}|{start - 5_000}")
    assert kb.release_stale_claims(conn) == 0
    assert kb.get_task(conn, tid).status == "running"


def test_the_recorded_fingerprint_carries_the_reference_it_was_built_against():
    """The format half of the fix: a reader can only attribute a mismatch if the reference travels."""
    fingerprint = kbd._process_fingerprint(os.getpid())
    reference, start = _fingerprint_reference_and_start(fingerprint)
    assert reference == gw_status.get_process_uptime_fingerprint(os.getpid())[0]
    assert start == gw_status.get_process_uptime_start_time(os.getpid())
    assert kbd._fingerprint_parts(fingerprint) == (gw_status.host_boot_witness(), start, reference)
    # The older two-field shape still parses — with no reference to compare against.
    assert kbd._fingerprint_parts(f"{gw_status.host_boot_witness()}|{start}") == (
        gw_status.host_boot_witness(), start, None,
    )
    assert kbd._fingerprint_parts("179041285600") is None


# ---------------------------------------------------------------------------
# 2026-09-29: the DEPLOYED READER was older than the fix (an override drop)
# ---------------------------------------------------------------------------
# The dispatched worker of t_b0c10dc8 (run 5642, pid 94448) was booked `crashed` at 08:41:06 with
# error `pid 94448 not alive ...` — 38 s after its last heartbeat, while the process was alive and
# working. The row it was booked from says which code produced it: the spawn event at 08:07:51
# recorded `started_at = "|179068367989"`, the pre-witness writer's 2-field empty-witness
# ABSOLUTE-epoch shape. The witness-aware writer cannot emit that shape (it refuses to record a
# fingerprint without a boot witness), and the census proves the reader had gone back with it: every
# spawn from 2026-09-29 01:02:10 to 12:36:53 carries the same 2-field form (run 5643, pid 78994,
# `"|179068536576"`), with 3-field `bootsession:<uuid>|<ref>|<boot-relative start>` before and after.
#
# So this is NOT a hole in the witness-aware comparator: reading those rows with it answers UNKNOWN
# and HOLDS. It is a deployment that ran bytes older than the fix — the fix must therefore be legs on
# the DEPLOYED artifact, which is what t_d98471e5 (parents=[t_daca6493]) carries. These tests pin the
# old shape's verdict so a future reader regression cannot quietly re-acquire the old answer.
INCIDENT_20260929_FINGERPRINTS = ("|179068367989", "|179068536576")


def _pre_witness_fingerprint(pid: int) -> str:
    """The pre-witness writer, verbatim from fork main: `f"{epoch}|{get_process_start_time(pid)}"`."""
    from gateway.drain_control import current_instantiation_epoch

    start = gw_status.get_process_start_time(int(pid))
    assert start is not None
    return f"{current_instantiation_epoch()}|{start}"


@pytest.mark.platforms("macos")
def test_the_20260929_false_dead_read_is_refused():
    """The incident's rows answer UNKNOWN for a live pid — the verdict only old bytes could invert."""
    for fingerprint in INCIDENT_20260929_FINGERPRINTS:
        assert kbd._fingerprint_parts(fingerprint) is None
        assert kbd._worker_liveness(os.getpid(), fingerprint) == kbd.WORKER_UNKNOWN
        assert kbd._worker_alive(os.getpid(), fingerprint) is False
        assert kbd._worker_not_dead(os.getpid(), fingerprint) is True

    # The old reader's only test was inequality against a fresh reading. A peer psutil generation
    # moves that reading by |INIT_BOOT_TIME - boot| — the #123811/#124570 shift — so "differs" is not
    # "recycled", and a live pid answered DEAD on the deployed pre-witness bytes. The witness-aware
    # fingerprint answers the same skew with the same value.
    import importlib

    psosx = importlib.import_module("psutil._psosx")
    original = psosx.INIT_BOOT_TIME
    assert original, "psutil exposes no boot reference to shift"
    read_here = _pre_witness_fingerprint(os.getpid())
    fixed_here = kbd._process_fingerprint(os.getpid())
    try:
        psosx.INIT_BOOT_TIME = original + 8
        read_by_peer = _pre_witness_fingerprint(os.getpid())
        fixed_by_peer = kbd._process_fingerprint(os.getpid())
    finally:
        psosx.INIT_BOOT_TIME = original

    assert read_here.startswith("|") and read_here.count("|") == 1  # the incident's shape
    assert read_here != read_by_peer  # what the pre-witness comparator read as "recycled"
    assert fixed_by_peer == fixed_here  # the witness-aware reader is invariant to the same skew
    assert fixed_here is not None and not fixed_here.startswith("|")


def test_the_20260929_false_dead_read_cannot_release_a_claim(board):
    """End to end on a board: an incident-shape row keeps its live worker through every reclaim path."""
    conn = board
    fingerprint = INCIDENT_20260929_FINGERPRINTS[0]
    tid = _claimed_running(conn, started_at=fingerprint, max_runtime=1)

    killed = []
    sig = lambda pid, s: killed.append((pid, s))  # noqa: E731
    assert kbd.detect_crashed_workers(conn) == []
    assert kb.release_stale_claims(conn, signal_fn=sig) == 0
    assert kbd.reconcile_orphaned_running(conn) == []
    assert kbd.enforce_max_runtime(conn, signal_fn=sig) == []
    assert killed == []
    task = kb.get_task(conn, tid)
    assert task.status == "running"
    assert task.worker_pid == os.getpid()


# ---------------------------------------------------------------------------
# 2026-10-05 (card t_d98471e5): the R1 deployed-bytes contract, the R2 heartbeat
# veto and the R3 orphan stand-down.
# ---------------------------------------------------------------------------

from tools import kanban_tools  # noqa: E402  (appended section, after the suite's fixtures)


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def _dead_running(conn, **kw):
    """A ``running`` card on a host-local claim whose pid is PROVEN dead."""
    return _claimed_running(conn, pid=9_999_999, started_at=1, **kw)


def _set_heartbeat(conn, tid, when):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?", (when, tid))


def _failures(conn, tid):
    return int(conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id = ?", (tid,)).fetchone()[0])


# --- R1: the deployed-bytes contract gate ----------------------------------

@pytest.mark.parametrize("contract", [None, "witness-v0", "not-a-contract", ""])
def test_crash_sweep_refuses_to_release_without_a_current_contract(board, monkeypatch, contract):
    """A tree that cannot declare a contract at or above the required one HOLDS: no release,
    no failure counted, and the refusal is RECORDED so a reader can see why."""
    conn = board
    monkeypatch.setattr(kbd, "LIVENESS_CONTRACT_ID", contract)
    tid = _dead_running(conn)

    assert kbd.detect_crashed_workers(conn) == []
    task = kb.get_task(conn, tid)
    assert task.status == "running"
    assert task.worker_pid == 9_999_999
    assert _failures(conn, tid) == 0
    refusals = _events(conn, tid, "reclaim_refused")
    assert refusals, "the refusal must be recorded, not silent"
    assert refusals[-1].payload["reason"] == "stale_liveness_contract"


def test_crash_sweep_releases_with_a_current_contract(board):
    """With the contract present the SAME row is reclaimed as before — the gate bites only when
    the declared contract is behind."""
    conn = board
    tid = _dead_running(conn)
    assert kbd.detect_crashed_workers(conn) == [tid]
    assert kb.get_task(conn, tid).status != "running"


def test_crash_decision_carries_the_liveness_contract(board):
    """The reclaim DECISION says which contract made it: run error, run metadata and the event."""
    conn = board
    tid = _dead_running(conn)
    assert kbd.detect_crashed_workers(conn) == [tid]
    run = conn.execute(
        "SELECT error, metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,)
    ).fetchone()
    assert kbd.LIVENESS_CONTRACT_ID in (run["error"] or "")
    assert kbd.LIVENESS_CONTRACT_ID in (run["metadata"] or "")
    crashed = _events(conn, tid, "crashed")
    assert crashed and crashed[-1].payload.get("liveness_contract") == kbd.LIVENESS_CONTRACT_ID


def test_liveness_contract_rank_is_fail_closed():
    assert kbd._liveness_contract_rank("witness-v1") == 1
    assert kbd._liveness_contract_rank("witness-v12") == 12
    assert kbd._liveness_contract_rank("witness-v") is None
    assert kbd._liveness_contract_rank("") is None
    assert kbd._liveness_contract_rank(None) is None
    assert kbd._liveness_contract_rank(1) is None


def test_announcing_the_contract_is_once_per_process(monkeypatch):
    monkeypatch.setattr(kbd, "_liveness_contract_announced", False)
    line = kbd.announce_liveness_contract()
    assert line and kbd.LIVENESS_CONTRACT_ID in line
    assert kbd.announce_liveness_contract() is None


# --- R2: the worker's heartbeat vetoes a dead-pid verdict -------------------

def test_crash_sweep_holds_a_fresh_heartbeat_row(board, monkeypatch):
    """A dead pid verdict does NOT release a claim whose worker beat within the veto window:
    hold, record ``heartbeat_fresh``, count no failure, keep the claim and the heartbeat."""
    conn = board
    monkeypatch.delenv("HERMES_KANBAN_HEARTBEAT_VETO_SECONDS", raising=False)
    monkeypatch.setattr(kbd, "HEARTBEAT_VETO_SECONDS", 120)
    tid = _dead_running(conn)
    _set_heartbeat(conn, tid, int(time.time()))

    assert kbd.detect_crashed_workers(conn) == []
    task = kb.get_task(conn, tid)
    assert task.status == "running"
    assert task.worker_pid == 9_999_999
    assert _failures(conn, tid) == 0
    defers = _events(conn, tid, "reclaim_deferred")
    assert defers and defers[-1].payload["reason"] == "heartbeat_fresh"
    assert conn.execute(
        "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (tid,)).fetchone()[0] is not None


def test_crash_sweep_reclaims_when_the_heartbeat_is_older_than_the_window(board, monkeypatch):
    """Older than the window is not a veto: the reclaim proceeds as today."""
    conn = board
    monkeypatch.delenv("HERMES_KANBAN_HEARTBEAT_VETO_SECONDS", raising=False)
    monkeypatch.setattr(kbd, "HEARTBEAT_VETO_SECONDS", 120)
    tid = _dead_running(conn)
    _set_heartbeat(conn, tid, int(time.time()) - 600)

    assert kbd.detect_crashed_workers(conn) == [tid]
    assert kb.get_task(conn, tid).status != "running"


def test_heartbeat_veto_off_by_default(board, monkeypatch):
    """Default 0 = off: a fresh heartbeat does NOT change behaviour for other callers."""
    conn = board
    monkeypatch.delenv("HERMES_KANBAN_HEARTBEAT_VETO_SECONDS", raising=False)
    monkeypatch.setattr(kbd, "HEARTBEAT_VETO_SECONDS", 0)
    tid = _dead_running(conn)
    _set_heartbeat(conn, tid, int(time.time()))
    assert kbd.detect_crashed_workers(conn) == [tid]


def test_heartbeat_veto_env_override_arms_the_window(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HEARTBEAT_VETO_SECONDS", "120")
    assert kbd.resolve_heartbeat_veto_seconds() == 120
    monkeypatch.setenv("HERMES_KANBAN_HEARTBEAT_VETO_SECONDS", "nonsense")
    assert kbd.resolve_heartbeat_veto_seconds() == kbd.HEARTBEAT_VETO_SECONDS
    monkeypatch.setenv("HERMES_KANBAN_HEARTBEAT_VETO_SECONDS", "-5")
    assert kbd.resolve_heartbeat_veto_seconds() == 0


# --- R3: the superseded orphan gets one stand-down steer --------------------

class _FakeAgent:
    def __init__(self):
        self.steers = []

    def steer(self, text):
        self.steers.append(text)
        return True


def _supersede(conn, tid):
    """Point the card at a NEWER run than the caller's, as a reclaim + respawn would."""
    our_run = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    with kb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) "
            "VALUES (?, 'worker', 'running', ?)",
            (tid, int(time.time())),
        )
        conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (cur.lastrowid, tid))
    return int(our_run), int(cur.lastrowid)


def _r3_env(monkeypatch, tid, run_id):
    """Clear the worker's own board pins so ``_board(None)`` lands in the fixture's temp home,
    then scope this process to the fixture's task/run."""
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setattr(kanban_tools, "_auto_heartbeat_last_attempt", 0.0)
    monkeypatch.setattr(kanban_tools, "_stand_down_steered", {})


def test_stand_down_fires_once_for_a_superseded_run(board, monkeypatch):
    conn = board
    tid = kb.create_task(conn, title="orphan", assignee="worker")
    kb.claim_task(conn, tid)
    our_run, new_run = _supersede(conn, tid)
    _r3_env(monkeypatch, tid, our_run)
    agent = _FakeAgent()

    assert kanban_tools.heartbeat_current_worker_from_env(agent) is False
    assert len(agent.steers) == 1, "exactly one stand-down for a superseded run"
    note = agent.steers[0]
    assert "STAND DOWN" in note
    assert str(our_run) in note and str(new_run) in note

    # Idempotent: a later beat does not steer the same run again.
    kanban_tools._auto_heartbeat_last_attempt = 0.0
    kanban_tools.heartbeat_current_worker_from_env(agent)
    assert len(agent.steers) == 1


def test_stand_down_does_not_fire_on_the_throttled_path(board, monkeypatch):
    conn = board
    tid = kb.create_task(conn, title="orphan", assignee="worker")
    kb.claim_task(conn, tid)
    our_run, _new = _supersede(conn, tid)
    _r3_env(monkeypatch, tid, our_run)
    agent = _FakeAgent()
    # Inside the throttle window the bridge must not even reach the board.
    monkeypatch.setattr(kanban_tools, "_auto_heartbeat_last_attempt", time.monotonic())

    assert kanban_tools.heartbeat_current_worker_from_env(agent) is False
    assert agent.steers == []


def test_stand_down_does_not_fire_when_the_run_still_owns_the_card(board, monkeypatch):
    """The no-op path (same run) is not a supersession and must not steer."""
    conn = board
    tid = kb.create_task(conn, title="mine", assignee="worker")
    kb.claim_task(conn, tid)
    our_run = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    _r3_env(monkeypatch, tid, our_run)
    agent = _FakeAgent()

    kanban_tools.heartbeat_current_worker_from_env(agent)
    assert agent.steers == []


def test_stand_down_without_an_agent_is_a_plain_heartbeat(board, monkeypatch):
    """Back-compat: no agent -> the refusal is swallowed exactly as before."""
    conn = board
    tid = kb.create_task(conn, title="orphan", assignee="worker")
    kb.claim_task(conn, tid)
    our_run, _new = _supersede(conn, tid)
    _r3_env(monkeypatch, tid, our_run)
    assert kanban_tools.heartbeat_current_worker_from_env() is False

