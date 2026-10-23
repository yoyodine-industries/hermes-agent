"""A row stamped to THIS process is not live merely because its pid is.

``alive(owner_pid, owner_started_at)`` is trivially true when the pid IS the current process, so a
row whose in-memory work is gone latched forever: the abandoned delegation was never classified and
its pending completion was never re-offered — the same defect ``cron/delivery_queue.py`` closes with
its ``_ACTIVE_DELIVERIES`` live set (a dead thread's row held by a live pid). The live set here is
``_records`` for in-flight work and ``_offered`` for a completion copy this process still holds.

These drive the real ledger against a temp home and the real process fingerprint, never a fake
``os.getpid()``.
"""

from __future__ import annotations

import json
import os
import queue

import pytest

from gateway import status
from tools import async_delegation as ad
from tools.process_registry import process_registry

SELF_PID = os.getpid()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    ad._reset_for_tests()
    # Isolate the process-wide completion queue: a dispatched delegation publishes onto it, and a
    # leftover copy would be drained by whichever test runs next.
    isolated = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", isolated)
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    yield
    ad._reset_for_tests()
    while not isolated.empty():
        isolated.get_nowait()


def _self_fingerprint() -> int:
    stamp = status.get_process_start_time(SELF_PID)
    assert stamp is not None, "the test needs a real start-time fingerprint for this process"
    return int(stamp)


def _seed(
    *, delegation_id: str, state: str = "running", pid: int = SELF_PID,
    started=None, event_json: str | None = None, updated_at: float = 1.0,
    attempts: int = 0,
) -> None:
    started = _self_fingerprint() if started is None else started
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "INSERT INTO async_delegations (delegation_id, origin_session, state, dispatched_at,"
            " updated_at, delivery_state, delivery_attempts, owner_pid, owner_started_at,"
            " task_json, event_json) VALUES (?, 's', ?, 1, ?, 'pending', ?, ?, ?, '{}', ?)",
            (delegation_id, state, updated_at, attempts, pid, started, event_json),
        )


def _row(delegation_id: str) -> dict:
    with ad._DB_LOCK, ad._transaction() as conn:
        row = conn.execute(
            "SELECT state, delivery_state, owner_pid, owner_started_at FROM async_delegations"
            " WHERE delegation_id=?", (delegation_id,)).fetchone()
    return {"state": row[0], "delivery_state": row[1], "owner_pid": row[2], "owner_started_at": row[3]}


def _hold_record(delegation_id: str, status_: str = "running") -> None:
    with ad._records_lock:
        ad._records[delegation_id] = {"delegation_id": delegation_id, "status": status_}


# ── abandoned-delegation recovery ───────────────────────────────────────────


def test_self_owned_running_row_with_no_in_memory_record_is_recovered():
    """The latch: our own pid holds the row, but nothing here is running it."""
    _seed(delegation_id="d-owned-elsewhere")
    assert ad.recover_abandoned_delegations() == 1
    assert _row("d-owned-elsewhere")["state"] == "unknown"


def test_self_owned_running_row_with_a_live_record_is_held():
    _seed(delegation_id="d-running")
    _hold_record("d-running", "running")
    assert ad.recover_abandoned_delegations() == 0
    assert _row("d-running")["state"] == "running"


def test_self_owned_row_finalized_in_memory_but_never_persisted_is_held():
    """The durable write is best-effort: a lost one must not fabricate an 'unknown' turn for a
    delegation this process already finalized and delivered in memory."""
    _seed(delegation_id="d-finalized")
    _hold_record("d-finalized", "ok")
    assert ad.recover_abandoned_delegations() == 0
    assert _row("d-finalized")["state"] == "running"


def test_another_processes_live_row_is_still_held(monkeypatch):
    recorded = 178864182760
    monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: recorded)
    _seed(delegation_id="d-foreign", pid=4242, started=recorded)
    assert ad.recover_abandoned_delegations() == 0
    assert _row("d-foreign")["state"] == "running"


def test_another_processes_dead_row_is_still_recovered(monkeypatch):
    monkeypatch.setattr(status, "_pid_exists", lambda pid: False)
    _seed(delegation_id="d-dead", pid=4242, started=178864182760)
    assert ad.recover_abandoned_delegations() == 1
    assert _row("d-dead")["state"] == "unknown"


# ── orphaned-completion sweep ───────────────────────────────────────────────


def _completion(delegation_id: str) -> str:
    return json.dumps({"type": "async_delegation", "delegation_id": delegation_id,
                       "session_key": "bot-chat", "status": "ok", "summary": "done"})


def _sweep(q, *, stale_after: float = ad._ORPHAN_STALE_S) -> int:
    return ad.sweep_orphaned_completions(q, now=1.0 + stale_after + 1)


def _drain(q) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_self_owned_completion_handed_back_is_reoffered():
    """``return_completion_offer`` promises 'the next sweep may offer the row again' — it must hold
    for a row our OWN process finalized, not only for one owned by another process."""
    _seed(delegation_id="d-offer", state="ok", event_json=_completion("d-offer"))
    q = queue.Queue()
    assert _sweep(q) == 1
    (evt,) = _drain(q)
    assert evt["delegation_id"] == "d-offer" and evt["restored"] is True


def test_self_owned_completion_still_offered_is_not_duplicated():
    _seed(delegation_id="d-held", state="ok", event_json=_completion("d-held"))
    with ad._orphan_lock:
        ad._offered.add((ad.hermes_home_key(ad.get_hermes_home()), "d-held"))
    q = queue.Queue()
    assert _sweep(q) == 0
    assert q.empty()


def test_reoffered_self_owned_completion_keeps_the_atomic_claim_gate():
    """One offer, one delivery: the claim is still what decides, never the presence of a copy."""
    _seed(delegation_id="d-claim", state="ok", event_json=_completion("d-claim"))
    q = queue.Queue()
    assert _sweep(q) == 1
    (evt,) = _drain(q)
    claim = ad.claim_event_delivery(evt, "first")
    assert claim
    assert ad.claim_event_delivery(evt, "second") is None
    assert ad.complete_completion_delivery("d-claim", claim) is True
    assert _row("d-claim")["delivery_state"] == "delivered"


def test_another_processes_live_completion_is_not_offered(monkeypatch):
    recorded = 178864182760
    monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: recorded)
    _seed(delegation_id="d-foreign-ok", state="ok", pid=4242, started=recorded,
          event_json=_completion("d-foreign-ok"))
    q = queue.Queue()
    assert _sweep(q) == 0
    assert q.empty()


# ── the live copy this process just queued ──────────────────────────────────


def _dispatch_in_process() -> str:
    import time as _time

    handle = ad.dispatch_async_delegation(
        goal="g", context=None, toolsets=None, role="leaf", model="m", session_key="live",
        runner=lambda: {"status": "completed", "summary": "done"})
    deadline = _time.monotonic() + 10
    while ad.active_count() and _time.monotonic() < deadline:
        _time.sleep(0.01)
    return handle["delegation_id"]


def test_the_copy_this_process_queued_holds_its_own_row():
    """A fresh in-process completion is a live copy, not an orphan: the sweep must not inject a
    second one just because the owner pid happens to be ours."""
    delegation_id = _dispatch_in_process()
    with ad._orphan_lock:
        held = (ad.hermes_home_key(ad.get_hermes_home()), delegation_id) in ad._offered
    assert held, "pushing a completion must register its offer"
    q = queue.Queue()
    assert _sweep(q) == 0
    assert q.empty()


def test_delivering_the_copy_releases_the_offer():
    delegation_id = _dispatch_in_process()
    assert ad.claim_completion_delivery(delegation_id, "c1") is True
    assert ad.complete_completion_delivery(delegation_id, "c1") is True
    with ad._orphan_lock:
        assert not [key for key in ad._offered if key[1] == delegation_id]
