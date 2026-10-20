"""Construction-time state.db maintenance must keep the startup watchdog's progress lease alive (#111092).

Archive, prune, orphan sweep and VACUUM are I/O-bound with near-zero CPU, so the watchdog's
CPU fallback cannot tell them from a parked deadlock; only a phase lease can. Leases are
clamped to ``_MAX_LEASE_S`` per call, so each long step renews rather than one lease at entry.
"""

from __future__ import annotations

import time

import hermes_startup_watchdog as sw
import hermes_state_maintenance
from hermes_state import SessionDB


def test_maintenance_steps_renew_the_armed_watchdog_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sw._reset_for_tests()
    handle = sw.arm_startup_watchdog(timeout_s=300.0)
    db = SessionDB(db_path=tmp_path / "state.db")
    phases: list[str] = []
    real_progress = hermes_state_maintenance.report_startup_progress

    def record(seconds, *, phase, **kwargs):
        phases.append(phase)
        return real_progress(seconds, phase=phase, **kwargs)

    monkeypatch.setattr(hermes_state_maintenance, "report_startup_progress", record)
    try:
        # Maintenance is throttled by state_meta; a fresh DB has never run it.
        monkeypatch.setattr(db, "prune_sessions", lambda **kw: 120)  # VACUUM only runs after a real prune
        monkeypatch.setattr(db, "sweep_orphaned_sessions", lambda **kw: [])
        monkeypatch.setattr(db, "_freelist_ratio", lambda: 1.0)
        monkeypatch.setattr(db, "vacuum", lambda *_a, **_k: None)
        monkeypatch.setattr(db, "archive_stale_sessions", lambda *a, **kw: 0)
        before = handle._lease_count

        db.maybe_auto_archive(idle_days=3, min_interval_hours=0)
        db.maybe_auto_prune_and_vacuum(
            retention_days=90, min_interval_hours=0, vacuum=True, min_vacuum_interval_days=0,
        )

        # One renewal per long step, the last one taken right before VACUUM so a multi-minute
        # rewrite never outlives the clamp. Asserted by PHASE, not by a bare count: the count
        # rotted silently every time a long step was added (it still said 4 after the archive,
        # orphan-reap and FTS-optimize steps landed), which is a test bug that reads as a
        # watchdog regression.
        assert {
            "state_db_auto_prune",
            "state_db_auto_sweep",
            "state_db_auto_reap_orphan_messages",
            "state_db_auto_fts_optimize",
            "state_db_auto_vacuum",
        } <= set(phases)
        assert phases[-1] == "state_db_auto_vacuum"
        assert handle._lease_count - before >= len(phases)  # every long step renewed on its own
        assert handle._lease_phase == "state_db_auto_vacuum"
        assert handle._lease_until > time.monotonic()
    finally:
        db.close()
        sw._reset_for_tests()
