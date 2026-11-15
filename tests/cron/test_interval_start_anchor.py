"""An interval is a cadence of DEPARTURES, so it is anchored on the run's START stamp.

Measured on the estate this was written for (2026-11-02, one job declaring "every 90m"): the row
computed ``next_run_at = last_run_at + 90m`` where ``last_run_at`` is the COMPLETION stamp, so a
55-85 minute pass delivered 145-175 minute gaps - 09:32 -> 11:05 (92m), then 142m, 175m, 145m -
while the row still displayed "every 90m".

The rule is ``next = max(completion, start + interval)``, taken as the first point of a lattice
anchored on the start that is STRICTLY after the completion. Two failure modes it keeps apart:

* a pass SHORTER than its interval still leaves one interval between STARTS, and
* a pass LONGER than its interval must not fire again the instant it finishes (that is a
  continuous loop wearing a cadence's clothes).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from cron.jobs import compute_next_run

UTC = ZoneInfo("UTC")
EST = ZoneInfo("America/New_York")


def _dt(zone, hour, minute):
    return datetime(2026, 11, 2, hour, minute, tzinfo=zone)


def _next(schedule_minutes, last_run_at, started_at=None):
    got = compute_next_run({"kind": "interval", "minutes": schedule_minutes},
                           last_run_at=last_run_at, started_at=started_at)
    assert got is not None
    return datetime.fromisoformat(got)


def test_a_pass_shorter_than_its_interval_leaves_one_interval_between_starts():
    """09:32 start, 83-minute pass -> the next DEPARTURE is 11:02, exactly 90 minutes later.

    The completion-anchored answer is 12:25 (10:55 + 90m): the gap between departures would be
    173 minutes while the schedule row says 90.
    """
    got = _next(90, _dt(EST, 10, 55).isoformat(), _dt(EST, 9, 32).isoformat())
    assert got == _dt(EST, 11, 2), got
    assert got - _dt(EST, 9, 32) == _dt(EST, 11, 2) - _dt(EST, 9, 32)


def test_a_pass_longer_than_its_interval_skips_the_slots_it_overran():
    """60-minute interval, 2h30 pass: the lattice's first point after the completion is 03:00.

    The anti-busy-loop floor: whatever the run's duration, the next fire is STRICTLY after the
    completion stamp, so an interval shorter than the runtime can never re-fire in the same
    instant and spin the dispatcher.
    """
    completion = _dt(UTC, 2, 30)
    got = _next(60, completion.isoformat(), _dt(UTC, 0, 0).isoformat())
    assert got == _dt(UTC, 3, 0), got
    assert got > completion


def test_a_completion_exactly_on_the_lattice_still_moves_forward():
    """The equality edge: start 00:00 + 60m, completion exactly 02:00. `max(completion, ...)`
    would answer 02:00 - an instant that is NOT after now - and the next tick would fire again
    immediately. The first lattice point STRICTLY after the completion is 03:00."""
    got = _next(60, _dt(UTC, 2, 0).isoformat(), _dt(UTC, 0, 0).isoformat())
    assert got == _dt(UTC, 3, 0), got


def test_a_caller_that_does_not_know_the_start_keeps_the_old_arithmetic():
    """Every non-dispatch caller (a re-arm, a migration, a CLI edit) keeps completion + interval:
    the anchor is opt-in, so this change cannot move a schedule nobody handed a start stamp."""
    got = _next(90, _dt(EST, 10, 55).isoformat())
    assert got == _dt(EST, 12, 25), got


def test_the_cadence_is_kept_across_a_zone_boundary():
    """A start stamp recorded in UTC and a completion in the profile's zone still count 90
    wall-clock-agnostic minutes: the lattice is arithmetic in UTC and re-rendered."""
    got = _next(90, _dt(EST, 10, 55).isoformat(), _dt(UTC, 14, 32).isoformat())  # 09:32 EST
    assert got == _dt(EST, 11, 2), got


def test_a_non_positive_interval_is_not_turned_into_a_loop():
    """`minutes: 0` is a malformed schedule, not a cadence: it must compute something finite
    rather than a next fire at (or before) now, which would spin."""
    completion = _dt(UTC, 10, 0)
    got = _next(0, completion.isoformat(), _dt(UTC, 9, 0).isoformat())
    assert got >= completion, got


def test_advance_after_run_hands_the_dispatch_stamp_to_the_interval_anchor(monkeypatch):
    """The wiring, not just the arithmetic: the job record's own `last_dispatch.dispatched_at`
    (written by the due scan when the occurrence left) IS the start stamp the advance uses."""
    from cron import jobs as jobs_mod

    start = _dt(EST, 9, 32)
    completion = _dt(EST, 10, 55)
    monkeypatch.setattr(jobs_mod, "_hermes_now", lambda: completion)
    job = {
        "id": "interval-row",
        "schedule": {"kind": "interval", "minutes": 90},
        "next_run_at": start.isoformat(),
        "last_dispatch": {"scheduled_at": start.isoformat(),
                          "dispatched_at": start.isoformat()},
        "repeat": {"times": None, "completed": 0},
        "state": "scheduled",
    }
    jobs_mod._advance_after_run(job, completion.isoformat())

    assert job["next_run_at"] is not None
    assert datetime.fromisoformat(job["next_run_at"]) == _dt(EST, 11, 2), job
    assert job["repeat"]["completed"] == 1, job
    assert job["state"] == "scheduled"
