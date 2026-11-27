"""The client's reading of the gateway's published cold-start lease.

The host these rules were measured on (2026-11-27): a 565 s cold start against a client whose
base budget is 60 s, and a LaunchDaemon owner. Every number here is one the owner's writer
guarantees (``gateway/startup_progress.py``), so the tests name the invariants, not the
implementation.
"""
from __future__ import annotations

from datetime import UTC, datetime
import json

from hermes_cli.gateway_runtime_lease import (
    CLIENT_CEILING_S,
    LEGACY,
    LIVE,
    READY_BY_GRACE_S,
    STALE_AFTER_S,
    STALLED,
    StartupLease,
    assess,
    horizon_s,
    read_startup_lease,
)

NOW = 1_800_000_000.0


def _lease(*, ready_by=NOW + 300.0, renewals=0, updated_at=NOW, pid=4242,
           phase="store_open", boot_started_at=NOW - 20.0, phase_started_at=NOW - 10.0):
    return StartupLease(pid=pid, phase=phase, ready_by=ready_by, renewals=renewals,
                        updated_at=updated_at, boot_started_at=boot_started_at,
                        phase_started_at=phase_started_at)


def test_a_fresh_lease_with_a_future_ready_by_is_live():
    wait = assess(_lease(), owner_pid=4242, renewals_seen=None, now=NOW)
    assert wait.kind == LIVE
    assert wait.renewals == 0


def test_a_renewal_since_the_previous_poll_is_live_even_after_ready_by_lapses():
    """The owner renews before its own estimate lapses; the bump is the liveness evidence."""
    wait = assess(_lease(ready_by=NOW - 5.0, renewals=3), owner_pid=4242, renewals_seen=2, now=NOW)
    assert wait.kind == LIVE


def test_ready_by_lapsed_inside_the_grace_without_a_renewal_is_still_live():
    wait = assess(_lease(ready_by=NOW - READY_BY_GRACE_S + 1.0), owner_pid=4242,
                  renewals_seen=0, now=NOW)
    assert wait.kind == LIVE


def test_ready_by_lapsed_past_the_grace_without_a_renewal_is_owner_start_stalled():
    wait = assess(_lease(ready_by=NOW - READY_BY_GRACE_S - 1.0), owner_pid=4242,
                  renewals_seen=0, now=NOW)
    assert wait.kind == STALLED
    assert wait.detail == "store_open, 20s, last renewal 0s"


def test_updated_at_older_than_three_missed_heartbeats_is_owner_start_stalled():
    """The heartbeat ticks every 15 s: 91 s of silence is a dead writer, not a slow phase."""
    wait = assess(_lease(updated_at=NOW - 91.0), owner_pid=4242, renewals_seen=0, now=NOW)
    assert wait.kind == STALLED
    assert wait.detail == "store_open, 20s, last renewal 91s"


def test_the_freshness_floor_is_the_documented_one():
    wait = assess(_lease(updated_at=NOW - STALE_AFTER_S), owner_pid=4242, renewals_seen=0, now=NOW)
    assert wait.kind == LIVE
    wait = assess(_lease(updated_at=NOW - STALE_AFTER_S - 0.1), owner_pid=4242,
                  renewals_seen=0, now=NOW)
    assert wait.kind == STALLED


def test_no_live_owner_is_owner_start_stalled_whatever_the_record_claims():
    wait = assess(_lease(), owner_pid=None, renewals_seen=0, now=NOW)
    assert wait.kind == STALLED
    assert wait.detail is not None and wait.detail.startswith("store_open, ")


def test_an_older_owners_record_keeps_the_callers_own_budget():
    assert assess(None, owner_pid=4242, renewals_seen=None, now=NOW).kind == LEGACY


def test_another_lifes_record_cannot_move_this_calls_deadline():
    """A crashed owner's record names a pid this call is not waiting on: not this start."""
    assert assess(_lease(pid=999), owner_pid=4242, renewals_seen=0, now=NOW).kind == LEGACY


def test_the_horizon_spends_ready_by_plus_the_grace():
    lease = _lease(ready_by=NOW + 580.0)
    assert horizon_s(lease, NOW) == 580.0 + READY_BY_GRACE_S


def test_the_horizon_never_passes_the_startup_watchdog_ceiling():
    """CLIENT_CEILING_S is the watchdog's own _MAX_LEASE_S: a client never gives up first."""
    from hermes_startup_watchdog import _MAX_LEASE_S

    assert CLIENT_CEILING_S == _MAX_LEASE_S
    assert horizon_s(_lease(ready_by=NOW + 5_000.0), NOW) == CLIENT_CEILING_S


def test_the_horizon_of_a_lapsed_lease_is_zero_not_negative():
    assert horizon_s(_lease(ready_by=NOW - 60.0), NOW) == 0.0


def _publish(home, startup, *, updated_at=NOW):
    record = {"gateway_state": "starting", "pid": startup.get("pid"),
              "updated_at": datetime.fromtimestamp(updated_at, tz=UTC).isoformat(),
              "startup": startup}
    (home / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")


def _startup(**overrides):
    startup = {"pid": 4242, "boot_started_at": NOW - 40.0, "phase": "store_open",
               "phase_started_at": NOW - 30.0, "phase_expected_s": 300.0,
               "ready_by": NOW + 260.0, "renewals": 1, "ready_at": None}
    startup.update(overrides)
    return startup


def test_read_startup_lease_reads_the_published_record(tmp_path):
    _publish(tmp_path, _startup())
    lease = read_startup_lease(tmp_path)
    assert lease is not None
    assert (lease.pid, lease.phase, lease.renewals) == (4242, "store_open", 1)
    assert lease.ready_by == NOW + 260.0
    assert abs(lease.updated_at - NOW) < 1.0


def test_a_ready_start_is_not_a_lease(tmp_path):
    """The startup window is closed: the control socket, not this record, is then the authority."""
    _publish(tmp_path, _startup(phase="ready", ready_at=NOW - 5.0))
    assert read_startup_lease(tmp_path) is None


def test_an_undateable_record_is_not_a_lease(tmp_path):
    """A record we cannot date is not proof of life: the caller keeps its own deadline."""
    record = {"gateway_state": "starting", "updated_at": "not-a-timestamp", "startup": _startup()}
    (tmp_path / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")
    assert read_startup_lease(tmp_path) is None


def test_an_older_owners_status_file_has_no_startup_object(tmp_path):
    record = {"gateway_state": "running", "pid": 4242,
              "updated_at": datetime.fromtimestamp(NOW, tz=UTC).isoformat()}
    (tmp_path / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")
    assert read_startup_lease(tmp_path) is None


def test_a_malformed_startup_object_is_not_a_lease(tmp_path):
    for bad in ({"pid": "4242"}, {"pid": 4242, "phase": ""}, {"pid": 4242, "phase": "store_open"}):
        _publish(tmp_path, bad)
        assert read_startup_lease(tmp_path) is None, bad
