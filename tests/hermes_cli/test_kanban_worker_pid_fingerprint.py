"""A recycled worker PID is never mistaken for our worker.

``tasks.worker_pid`` survives a reboot; the number can then belong to an unrelated process. Every
liveness decision (extend/defer the claim) and every kill (SIGTERM/SIGKILL on timeout or reclaim)
must require the spawn-time start fingerprint to match, never bare PID existence.
"""

import os
import signal
import time
from datetime import datetime
from datetime import timezone

import pytest

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


def _claimed_running(conn, *, pid: int, started_at, max_runtime=None) -> str:
    tid = kb.create_task(conn, title="job", assignee="worker", max_runtime_seconds=max_runtime)
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid)
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_started_at = ?, started_at = ?, claim_expires = ? WHERE id = ?",
                     (started_at, old, old, tid))
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                     (old, tid))
    return tid


def test_recycled_pid_is_reclaimed_without_being_signalled(board):
    """Our own live PID with a foreign fingerprint models a post-reboot recycle: the claim is released
    (dead worker), no signal is sent, and max-runtime enforcement does not SIGTERM the stranger either."""
    conn = board
    killed = []
    stranger_fingerprint = 1  # no live process started at tick 1
    tid = _claimed_running(conn, pid=os.getpid(), started_at=stranger_fingerprint, max_runtime=1)

    assert kbd._worker_alive(os.getpid(), stranger_fingerprint) is False
    assert tid in kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))
    assert killed == []
    task = kb.get_task(conn, tid)
    assert task.status == "ready" and task.worker_pid is None

    tid2 = _claimed_running(conn, pid=os.getpid(), started_at=stranger_fingerprint)
    assert kb.release_stale_claims(conn, signal_fn=lambda pid, sig: killed.append((pid, sig))) == 1
    assert killed == []
    assert kb.get_task(conn, tid2).status == "ready"


def test_matching_fingerprint_keeps_the_live_worker(board):
    """The same PID with ITS OWN fingerprint (recorded at spawn) is our worker: the expired claim is
    extended rather than reclaimed, and the timeout path signals it."""
    from gateway.status import get_process_start_time

    conn = board
    killed = []
    tid = _claimed_running(conn, pid=os.getpid(), started_at=get_process_start_time(os.getpid()))
    assert kbd._worker_alive(os.getpid(), get_process_start_time(os.getpid())) is True
    assert kb.release_stale_claims(conn) == 0
    assert kb.get_task(conn, tid).status == "running"
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "claim_extended" in kinds

    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET max_runtime_seconds = 1 WHERE id = ?", (tid,))
    kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))
    assert killed and killed[0] == (os.getpid(), signal.SIGTERM)


def test_same_pid_and_start_tick_on_another_boot_is_foreign(board, monkeypatch):
    """A row that survived a reboot: the PID AND the boot-relative start tick both match a process on
    this boot (the Linux start time is clock ticks since boot, so that recurs), but the persisted
    instantiation epoch does not. The worker is foreign: claim released, zero signals."""
    from gateway import drain_control

    conn = board
    killed = []
    live_fingerprint = kbd._process_fingerprint(os.getpid())
    assert live_fingerprint is not None
    from gateway.status import get_process_start_time
    form, boot, start = live_fingerprint.split("|")
    assert form == kbd.WORKER_FINGERPRINT_FORM and all((form, boot, start))
    assert start == str(get_process_start_time(os.getpid()))
    tid = _claimed_running(conn, pid=os.getpid(), started_at=live_fingerprint, max_runtime=1)
    assert kbd._worker_alive(os.getpid(), live_fingerprint) is True

    # Same PID, same start reading, different boot identity.
    other_boot = "%s|deadbeef-boot:1|%s" % (kbd.WORKER_FINGERPRINT_FORM, start)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_started_at = ? WHERE id = ?", (other_boot, tid))
    assert kbd._worker_alive(os.getpid(), other_boot) is False
    assert tid in kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))
    assert killed == []
    task = kb.get_task(conn, tid)
    assert task.status == "ready" and task.worker_pid is None

    # The same value re-derived on THIS boot still identifies our worker (the witness is stable
    # within a boot, unlike the recorded epoch of a previous one).
    drain_control.current_instantiation_epoch.cache_clear()
    assert kbd._process_fingerprint(os.getpid()) == live_fingerprint


def test_unverified_fingerprint_capture_never_authorizes_a_signal(board, monkeypatch):
    """Fingerprint capture fails for a new spawn: the row is NOT a legacy NULL row. A live PID under
    it is never SIGTERM/SIGKILLed by any reclaim/timeout path, and the claim is held (not released
    beside the live process); once the PID is gone the claim is reclaimed normally."""
    import gateway.status as status

    conn = board
    killed = []
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: None)
    tid = kb.create_task(conn, title="job", assignee="worker", max_runtime_seconds=1)
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, os.getpid())
    row = conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["worker_started_at"] == kbd.UNVERIFIED_WORKER_FINGERPRINT
    monkeypatch.undo()
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ?, claim_expires = ? WHERE id = ?", (old, old, tid))
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                     (old, tid))

    sig = lambda pid, s: killed.append((pid, s))  # noqa: E731
    assert kbd.enforce_max_runtime(conn, signal_fn=sig) == []
    assert kb.release_stale_claims(conn, signal_fn=sig) == 0
    assert killed == []
    assert kb.get_task(conn, tid).status == "running"
    # An explicit operator reclaim releases the claim (human override) but still sends nothing.
    assert kb.reclaim_task(conn, tid, reason="operator", signal_fn=sig) is True
    assert killed == []

    # The process is gone (a dead PID): the row is reclaimed like any dead worker, still no signal.
    tid2 = kb.create_task(conn, title="job2", assignee="worker")
    kb.claim_task(conn, tid2)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ?, claim_expires = ? WHERE id = ?",
                     (os.getpid(), kbd.UNVERIFIED_WORKER_FINGERPRINT, old, tid2))
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    assert kb.release_stale_claims(conn, signal_fn=sig) == 1
    assert killed == [] and kb.get_task(conn, tid2).status == "ready"


def _boot_witness() -> str:
    """The witness the canonical form carries for THIS host (``-`` where the platform has none)."""
    from gateway.drain_control import current_instantiation_epoch

    return current_instantiation_epoch() or kbd.ABSENT_BOOT_WITNESS


def _live_start() -> str:
    from gateway.status import get_process_start_time

    return str(get_process_start_time(os.getpid()))


def test_spawn_event_publishes_the_instant_and_keeps_the_fingerprint_separate(board):
    """The ``spawned`` event's ``started_at`` is the spawn INSTANT in epoch seconds — readable with
    ``int()``/``datetime``, the same form ``task_runs.started_at`` uses — and the identity token goes
    under ``worker_fingerprint``. Red on the shape this card was filed against, which wrote the
    fingerprint ``"|<start reading>"`` under ``started_at`` and could only be misread as a timestamp."""
    conn = board
    tid = kb.create_task(conn, title="job", assignee="worker")
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, os.getpid())

    event = [e for e in kb.list_events(conn, tid) if e.kind == "spawned"][-1]
    payload = event.payload
    started_at = payload["started_at"]
    assert isinstance(started_at, int) and started_at > 0
    assert datetime.fromtimestamp(started_at, timezone.utc).year >= 2026
    run = conn.execute("SELECT started_at FROM task_runs WHERE id = ?", (event.run_id,)).fetchone()
    assert started_at == run["started_at"]  # one instant, one form, one value

    fingerprint = payload["worker_fingerprint"]
    form, boot, start = fingerprint.split("|")
    assert form == kbd.WORKER_FINGERPRINT_FORM
    assert all((form, boot, start)), fingerprint  # never a dangling/empty field
    assert boot == _boot_witness()
    assert start == _live_start()
    assert fingerprint == kbd._process_fingerprint(os.getpid())
    assert payload["pid"] == os.getpid()


def test_start_reading_drift_inside_the_tolerance_keeps_the_worker(board):
    """The recorded start reading may drift from a later read of the SAME process (#117505). Inside
    the documented tolerance the worker is ours (claim extended, no signal); past it the PID is
    recycled (claim released, still no signal — the stranger is never touched)."""
    from gateway.status import START_TIME_DRIFT_TOLERANCE

    conn = board
    killed = []
    live = int(_live_start())
    witnessed = "%s|%s|%d" % (kbd.WORKER_FINGERPRINT_FORM, _boot_witness(), live)

    tid = _claimed_running(conn, pid=os.getpid(), started_at=witnessed)
    assert kbd._worker_alive(os.getpid(), witnessed) is True
    assert kb.release_stale_claims(conn, signal_fn=lambda pid, sig: killed.append((pid, sig))) == 0
    assert kb.get_task(conn, tid).status == "running" and killed == []

    recycled = "%s|%s|%d" % (
        kbd.WORKER_FINGERPRINT_FORM, _boot_witness(), live + START_TIME_DRIFT_TOLERANCE + 1)
    tid2 = _claimed_running(conn, pid=os.getpid(), started_at=recycled)
    assert kbd._worker_alive(os.getpid(), recycled) is False
    assert kb.release_stale_claims(conn, signal_fn=lambda pid, sig: killed.append((pid, sig))) == 1
    assert killed == [] and kb.get_task(conn, tid2).status == "ready"


def test_pre_canonical_two_field_fingerprint_still_identifies_its_own_worker(board):
    """A row written before the canonical form landed (``"<witness>|<start>"``, empty witness on a
    platform without one — the measured ``"|179036519519"``) must still identify its live worker after
    the upgrade: same witness, same start reading, claim extended and not reclaimed."""
    conn = board
    killed = []
    legacy = "|%s" % _live_start()  # the pre-canonical macOS shape, verbatim
    assert kbd._fingerprint_parts(legacy) == (_boot_witness(), _live_start())

    tid = _claimed_running(conn, pid=os.getpid(), started_at=legacy)
    assert kbd._worker_alive(os.getpid(), legacy) is True
    assert kb.release_stale_claims(conn, signal_fn=lambda pid, sig: killed.append((pid, sig))) == 0
    assert kb.get_task(conn, tid).status == "running" and killed == []

    # The witness is still load-bearing in the legacy shape: a foreign boot is a foreign process.
    foreign = "deadbeef-boot:1|%s" % _live_start()
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_started_at = ? WHERE id = ?", (foreign, tid))
    assert kbd._worker_alive(os.getpid(), foreign) is False
