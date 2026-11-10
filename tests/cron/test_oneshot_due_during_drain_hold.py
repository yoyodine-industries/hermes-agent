"""A one-shot that comes due while a gateway drain holds cron dispatch fires once afterwards.

A restart or ``hermes update`` drain waits on in-flight turns with the ticker's dispatch held
(``can_dispatch`` false). The one-shot grace (120s) must count from the end of that hold, not
from the due time: an update that waited three minutes on a Kanban turn retired the reminder
the user scheduled for that minute as ``missed`` without ever running it. A hold whose gateway
never came back, or a reminder due hours into an endless external drain, is still retired (#93526).
"""

import json
import threading
import time
from datetime import datetime, timedelta, UTC

import pytest

from cron.dispatch_hold import note_held
from cron.jobs import get_due_jobs, load_jobs, save_jobs
from cron.scheduler_provider import InProcessCronScheduler


@pytest.fixture()
def cron_store(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    (tmp_path / "cron").mkdir()
    return tmp_path


def _oneshot(jid, run_at):
    return {
        "id": jid, "name": jid, "prompt": "x",
        "schedule": {"kind": "once", "run_at": run_at.isoformat()},
        "next_run_at": run_at.isoformat(), "last_run_at": None,
        "enabled": True, "state": "scheduled", "repeat": {"times": 1, "completed": 0},
        "deliver": "local", "run_claim": None, "fire_claim": None,
    }


def _scan_at(monkeypatch, when):
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: when)
    return [d["id"] for d in get_due_jobs()]


def _held_ticker_cycle():
    """One cycle of the real in-process ticker while the gateway drains."""
    stop = threading.Event()

    def draining():
        stop.set()
        return False

    InProcessCronScheduler().start(stop, interval=1, can_dispatch=draining)


def test_oneshot_due_during_a_drain_fires_once_the_next_gateway_ticks(cron_store, monkeypatch):
    t0 = time.time()
    note_held(now=t0 - 100)  # the drain began holding dispatch 100s ago ...
    _held_ticker_cycle()     # ... and the ticker is still holding it now
    start = datetime.fromtimestamp(t0, UTC)
    save_jobs([_oneshot("reminder", start - timedelta(seconds=90))])

    # The next gateway's first tick lands 190s after the due time, past the 120s grace.
    assert _scan_at(monkeypatch, start + timedelta(seconds=100)) == ["reminder"]
    assert [j["id"] for j in load_jobs()] == ["reminder"]


@pytest.mark.parametrize("hold_ticks_ago,due_ago", [
    ((3 * 3600 + 60, 3 * 3600), 3 * 3600 + 30),  # the drain's gateway never came back
    (tuple(range(5 * 3600, 89, -60)), 5 * 3600),  # a live hold (external drain) ticking for hours
], ids=["gateway-never-returned", "hours-long-live-hold"])
def test_stale_oneshot_is_retired_despite_a_hold(cron_store, monkeypatch, hold_ticks_ago, due_ago):
    t0 = time.time()
    for ago in hold_ticks_ago:
        note_held(now=t0 - ago)
    start = datetime.fromtimestamp(t0, UTC)
    save_jobs([_oneshot("reminder", start - timedelta(seconds=due_ago))])

    assert _scan_at(monkeypatch, start) == []
    assert load_jobs() == []


# --- the stamp must not outlive the hold (t_895cf539) -------------------------------------------
# A frozen {"since": X, "last": X} file reads exactly like a dispatcher that stopped ticking, and
# it was read that way: financially-stl concluded dispatch "had NOT ticked since 00:29 (10.3h)"
# from a file left behind by a drain that had resumed precisely BECAUSE the ticker was dispatching.

def _stamp(cron_dir, since, last):
    (cron_dir / "dispatch_held.json").write_text(
        json.dumps({"since": since, "last": last}), encoding="utf-8"
    )


def test_a_resumed_dispatch_retires_its_own_hold_stamp(cron_store):
    from cron.dispatch_hold import note_dispatching, read

    cron_dir = cron_store / "cron"
    note_held(now=time.time() - 100)
    note_held(now=time.time())
    assert read(cron_dir) is not None  # a held tick leaves the stamp

    note_dispatching()  # the first dispatching tick still feeds grace_starts_at ...
    assert read(cron_dir) is not None
    note_dispatching()  # ... the next one retires it: no file = no hold
    assert read(cron_dir) is None
    assert not (cron_dir / "dispatch_held.json").exists()


def test_a_stamp_left_by_a_previous_gateway_is_retired_once_dispatch_resumes(cron_store):
    from cron.dispatch_hold import note_dispatching, read

    cron_dir = cron_store / "cron"
    _stamp(cron_dir, since=time.time() - 200, last=time.time() - 200)  # that gateway never came back

    # The restarting gateway's first read must still see it, or a one-shot due mid-drain loses
    # the grace extension this module exists for (#93526, the test above).
    note_dispatching()
    assert read(cron_dir) is not None
    note_dispatching()
    assert read(cron_dir) is None


def test_a_live_hold_is_not_retired_by_a_reading_process(cron_store):
    from cron.dispatch_hold import note_dispatching, read

    cron_dir = cron_store / "cron"
    note_held(now=time.time() - 100)
    note_dispatching()  # a process that observed dispatch but never held this store
    note_held(now=time.time())  # the hold is still being stamped every tick
    note_dispatching()
    assert read(cron_dir) is not None, "a hold that is still ticking must keep its stamp"


def test_grace_still_extends_across_the_retiring_tick(cron_store, monkeypatch):
    """The extension must survive the tick that resumes dispatch -- the retiring tick is the next one."""
    from cron.dispatch_hold import grace_starts_at, note_dispatching

    cron_dir = cron_store / "cron"
    now = time.time()
    note_held(now=now - 100)
    due = now - 90
    note_dispatching()
    assert grace_starts_at(cron_dir, due, grace=120) > due
