"""The gateway's published cold-start lease (``gateway_state.json`` ``startup``).

What these tests pin down, in the order the card states it:

* the lease exists within the first write of a boot, names THIS pid and a phase from the one
  vocabulary, and cycles the lifecycle sentinel to ``starting``;
* phases only move forward, ``ready_by`` never goes backwards, and a heartbeat renews before
  a phase's own estimate lapses (bumping ``renewals``) — so a client can wait out a 565 s
  cold start instead of giving up at its 300 s cap;
* a store already at ``SCHEMA_VERSION`` claims NO ``data_migrations`` lease (the no-op chain
  that used to buy 600 s of watchdog time), while a legacy store still does;
* the existing runtime-status writer owns the field, so the loop's own writes keep it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import hermes_startup_watchdog
from gateway import lifecycle_ledger
from gateway import startup_progress as sp
from gateway.status import read_runtime_status, write_runtime_status

_STARTUP_FIELDS = {
    "pid", "boot_started_at", "phase", "phase_started_at", "phase_expected_s",
    "ready_by", "renewals", "ready_at",
}


@pytest.fixture(autouse=True)
def _fresh_startup_state(monkeypatch):
    """No module state (or stashed unclean-exit evidence) leaks between tests."""
    sp._reset_for_tests()
    monkeypatch.setattr(lifecycle_ledger, "_pending_unclean_evidence", None, raising=False)
    yield
    sp._reset_for_tests()


@pytest.fixture()
def clock(monkeypatch):
    """Drive ``startup_progress``'s clock; ``clock[0]`` is 'now' in epoch seconds."""
    now = [1_000_000.0]
    monkeypatch.setattr(sp, "_now", lambda: now[0])
    return now


def _watchdog_calls(monkeypatch) -> list[tuple[float, str]]:
    calls: list[tuple[float, str]] = []
    monkeypatch.setattr(
        hermes_startup_watchdog, "report_startup_progress",
        lambda expected_s, phase="": calls.append((expected_s, phase)))
    return calls


def _startup_from_disk() -> dict:
    state = read_runtime_status()
    assert isinstance(state, dict), "gateway_state.json was not written"
    return state


def _sentinel() -> dict:
    return json.loads(lifecycle_ledger.get_lifecycle_sentinel_path().read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The first write: pid, phase vocabulary, sentinel
# ---------------------------------------------------------------------------

def test_begin_publishes_this_pid_its_phase_and_the_starting_sentinel(clock):
    started = sp.begin_startup()

    assert started is not None
    assert set(started) == _STARTUP_FIELDS
    assert started["pid"] == os.getpid()
    assert started["phase"] == "runtime_import"
    assert started["renewals"] == 0
    assert started["ready_at"] is None
    assert started["ready_by"] == pytest.approx(
        started["phase_started_at"] + started["phase_expected_s"])

    state = _startup_from_disk()
    assert state["startup"] == started
    assert state["gateway_state"] == "starting"
    assert state["exit_reason"] is None
    # The writer stamps the CURRENT process over the merged record: that is what makes an early
    # write safe when the file still carries the previous life.
    assert state["pid"] == os.getpid()

    sentinel = _sentinel()
    assert sentinel["phase"] == "starting"
    assert sentinel["pid"] == os.getpid()


def test_phase_budgets_are_handed_to_the_watchdog(monkeypatch):
    calls = _watchdog_calls(monkeypatch)
    sp.begin_startup()
    assert calls == [(sp.PHASE_EXPECTED_S["runtime_import"], "runtime_import")]

    calls.clear()
    sp.advance_startup_phase(sp.PHASE_STORE_OPEN)
    # ONE number: the phase's own published budget is the watchdog's lease.
    assert calls == [(sp.PHASE_EXPECTED_S["store_open"], "store_open")]


def test_only_the_phase_vocabulary_is_published(clock):
    sp.begin_startup()
    before = _startup_from_disk()["startup"]

    assert sp.advance_startup_phase("state_db_data_migrations") is None

    assert _startup_from_disk()["startup"] == before


# ---------------------------------------------------------------------------
# Monotonic phases, ready_by, renewals
# ---------------------------------------------------------------------------

def test_phases_move_forward_and_ready_by_advances(clock):
    sp.begin_startup()
    first = sp.advance_startup_phase(sp.PHASE_STORE_OPEN)

    clock[0] += 5
    second = sp.advance_startup_phase(sp.PHASE_DATA_MIGRATIONS)

    assert first["phase"] == "store_open"
    assert second["phase"] == "data_migrations"
    assert first["ready_by"] < second["ready_by"]
    assert second["ready_by"] == pytest.approx(
        second["phase_started_at"] + second["phase_expected_s"])

    # A later store's open cannot pull the phase back.
    clock[0] += 5
    still = sp.advance_startup_phase(sp.PHASE_STORE_OPEN)
    assert still["phase"] == "data_migrations"
    assert _startup_from_disk()["startup"]["phase"] == "data_migrations"


def test_a_cheaper_phase_never_shortens_the_published_deadline(clock):
    """A 120 s phase after a 600 s one keeps the outstanding deadline, not a new, earlier one."""
    sp.begin_startup()
    clock[0] += 1
    long_phase = sp.advance_startup_phase(sp.PHASE_DATA_MIGRATIONS)
    clock[0] += 5
    cheap = sp.advance_startup_phase(sp.PHASE_SERVICES_WIRING)

    assert cheap["ready_by"] == pytest.approx(long_phase["ready_by"])
    assert cheap["phase_expected_s"] == pytest.approx(long_phase["ready_by"] - clock[0])
    assert cheap["ready_by"] == pytest.approx(
        cheap["phase_started_at"] + cheap["phase_expected_s"])


def test_the_heartbeat_thread_renews_on_a_real_clock(monkeypatch):
    """The thread — not just a direct tick — renews and refreshes against the real clock."""
    monkeypatch.setattr(sp, "HEARTBEAT_INTERVAL_S", 0.02)
    # Make every tick a renewal: the renewal ARITHMETIC has its own test; this one proves the
    # thread actually ticks, renews through the same code path, and stands down at ready.
    monkeypatch.setattr(sp, "_renew_margin_s", lambda budget: 1e9)
    started = sp.begin_startup()
    assert started is not None
    sp.advance_startup_phase(sp.PHASE_STORE_OPEN)
    deadline = time.time() + 10.0
    while time.time() < deadline and (sp.startup_snapshot() or {}).get("renewals", 0) < 2:
        time.sleep(0.02)
    snapped = sp.startup_snapshot() or {}
    assert snapped.get("renewals", 0) >= 2, snapped
    assert snapped["ready_by"] > started["ready_by"]   # a renewal pushed the deadline out
    sp.mark_startup_ready()
    assert sp._thread is None  # the heartbeat stands down at ready


def test_heartbeat_renews_before_the_estimate_lapses(clock, monkeypatch):
    calls = _watchdog_calls(monkeypatch)
    sp.begin_startup()
    sp.advance_startup_phase(sp.PHASE_STORE_OPEN)
    calls.clear()

    # Early in the phase: only updated_at moves, no renewal.
    clock[0] += 10
    fresh = sp.heartbeat_tick()
    assert fresh["renewals"] == 0
    assert calls == []

    # Inside the renewal margin: ready_by is rewritten forward and renewals rises.
    clock[0] += sp.PHASE_EXPECTED_S["store_open"]
    renewed_before = _startup_from_disk()["startup"]
    renewed = sp.heartbeat_tick()

    assert renewed["renewals"] == 1
    assert renewed["ready_by"] > renewed_before["ready_by"]
    assert renewed["ready_by"] == pytest.approx(clock[0] + renewed["phase_expected_s"])
    assert calls == [(sp.PHASE_EXPECTED_S["store_open"], "store_open")]
    assert _startup_from_disk()["startup"]["renewals"] == 1


def test_heartbeat_interval_keeps_updated_at_inside_thirty_seconds():
    assert 0 < sp.HEARTBEAT_INTERVAL_S <= 30.0


def test_mark_ready_stamps_ready_at_and_stops_the_heartbeat(clock):
    sp.begin_startup()
    assert sp._thread is not None and sp._thread.is_alive()

    clock[0] += 42
    previous_ready_by = _startup_from_disk()["startup"]["ready_by"]
    ready = sp.mark_startup_ready()

    assert ready["phase"] == "ready"
    assert ready["ready_at"] == pytest.approx(clock[0])
    assert ready["renewals"] == 0
    # ready_by may not move backwards across the transition (a client compares the series), and
    # the published identity still holds exactly.
    assert ready["ready_by"] >= previous_ready_by
    assert ready["ready_by"] == pytest.approx(
        ready["phase_started_at"] + ready["phase_expected_s"])
    assert sp._thread is None
    assert _startup_from_disk()["startup"]["ready_at"] == pytest.approx(clock[0])

    # Nothing renews or rewrites after ready; the loop's writer owns the record.
    clock[0] += 10_000
    assert sp.heartbeat_tick() is None
    assert sp.advance_startup_phase(sp.PHASE_ADAPTERS) is None
    assert _startup_from_disk()["startup"]["phase"] == "ready"


# ---------------------------------------------------------------------------
# The lease is honest: no lease without work
# ---------------------------------------------------------------------------

def _set_schema_version(db_path: Path, version: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE schema_version SET version = ?", (version,))
        conn.commit()
    finally:
        conn.close()


def _open_store(db_path: Path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=db_path)
    close = getattr(db, "close", None)
    if callable(close):
        close()
    return db


def test_current_store_takes_no_data_migration_lease(tmp_path, monkeypatch):
    from hermes_state_schema import SCHEMA_VERSION

    calls = _watchdog_calls(monkeypatch)
    db_path = tmp_path / "state.db"

    _open_store(db_path)  # create at SCHEMA_VERSION
    _set_schema_version(db_path, SCHEMA_VERSION)
    calls.clear()

    _open_store(db_path)  # every boot of a current store

    assert [phase for _s, phase in calls] == [], (
        "a store already at SCHEMA_VERSION bought a startup lease: " + repr(calls))


def test_legacy_store_still_leases_data_migration(tmp_path, monkeypatch):
    calls = _watchdog_calls(monkeypatch)
    db_path = tmp_path / "state.db"

    _open_store(db_path)
    _set_schema_version(db_path, 12)  # older than SCHEMA_VERSION: the chain has work to do
    calls.clear()

    _open_store(db_path)

    assert ("data_migrations" in [phase for _s, phase in calls]) or (
        sp.PHASE_EXPECTED_S["data_migrations"] in [seconds for seconds, _p in calls]), calls


# ---------------------------------------------------------------------------
# Plumbing: the runtime-status writer owns the field
# ---------------------------------------------------------------------------

def test_runtime_status_writer_owns_the_startup_field(clock):
    sp.begin_startup()
    published = _startup_from_disk()["startup"]
    assert set(published) == _STARTUP_FIELDS

    # The loop's own write (no startup kwarg) must not drop the field.
    assert write_runtime_status(gateway_state="running", wait_timeout=2.0) is True
    state = _startup_from_disk()
    assert state["gateway_state"] == "running"
    assert state["startup"] == published


# ---------------------------------------------------------------------------
# The CLI reader
# ---------------------------------------------------------------------------

def test_status_renders_the_starting_phase(clock):
    from hermes_cli.gateway import _runtime_health_lines

    sp.begin_startup()
    lines = _runtime_health_lines()

    assert any("Gateway starting" in line and "runtime_import" in line for line in lines), lines


def test_status_stops_rendering_a_phase_once_ready(clock):
    from hermes_cli.gateway import _runtime_health_lines

    sp.begin_startup()
    sp.mark_startup_ready()
    write_runtime_status(gateway_state="running", wait_timeout=2.0)

    lines = _runtime_health_lines()
    assert not any("Gateway starting" in line for line in lines), lines


# ---------------------------------------------------------------------------
# The starting claim must not erase the previous life's death
# ---------------------------------------------------------------------------

def test_claim_starting_keeps_the_unclean_exit_evidence():
    dead_pid = 2 ** 22 + 12345
    path = lifecycle_ledger.get_lifecycle_sentinel_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "phase": "running", "pid": dead_pid, "start_time": 1.0,
        "started_at": "2026-11-26T18:35:59+00:00",
    }), encoding="utf-8")

    lifecycle_ledger.claim_starting()

    sentinel = _sentinel()
    assert sentinel["phase"] == "starting"
    assert sentinel["pid"] == os.getpid()

    evidence = lifecycle_ledger.detect_unclean_exit()
    assert evidence is not None and evidence["prior_pid"] == dead_pid
    # Single use: the later record_startup reports it once.
    assert lifecycle_ledger.detect_unclean_exit() is None
