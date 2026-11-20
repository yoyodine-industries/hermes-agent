"""The CRON SEAM of the fleet hold (card t_2f60a701).

Measured 2026-11-03: fourteen departures in a row died in ``drain_gate`` classified ``cadence``.
The gate's own ordering was half the defect (it armed its hold only after it had finished
measuring); the other half is these cases. ``agent.estop.work_admitted`` is the ONE predicate for
"may new work start in this lane" and cron never consulted it, so a window that parked the fleet
kept finding every job still firing -- the fleet-wide hold was, for scheduling purposes, a no-op.

Two behaviours are pinned here:

  * a due job whose lane is HELD does not dispatch;
  * its slot is COLLAPSED to the next live slot -- never fired late, never replayed on release.
    A hold lifted after nine jobs missed an occurrence must not fire those nine at one instant.

Fail-open is pinned too: a scheduler that stops because the estop helper could not be READ is a
fleet-wide silent stop with no owner, which is worse than one late job.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

_NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated HERMES_HOME whose ESTOP is this case's own file."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent import estop

    sentinel = tmp_path / "ESTOP"
    monkeypatch.setattr(estop, "_candidate_sentinel_paths", lambda: [sentinel])
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: _NOW)
    return tmp_path


def _hold(allow_lanes=("alpha",), ttl="30m"):
    """Arm a real, SIGNED lockdown hold through the module's own arming path."""
    from agent import estop

    result = estop.arm(
        owner="departure", mode="lockdown", reason="window hold", ttl=ttl,
        allow={"profiles": list(allow_lanes)})
    assert result.verified, result          # D4 may rename the holder; the hold still lands
    state = estop.read_state()
    assert state.engaged and not state.total, state


def _job(next_run_at: datetime, *, name="probe", schedule="every 5m") -> str:
    """Persist one job whose stored instant is the one the case needs."""
    from cron.jobs import create_job, load_jobs, save_jobs

    job = create_job(prompt="x", schedule=schedule, name=name)
    jobs = load_jobs()
    for stored in jobs:
        if stored["id"] == job["id"]:
            stored["next_run_at"] = next_run_at.isoformat()
    save_jobs(jobs)
    return job["id"]


def _due_next_day() -> datetime:
    return _NOW - timedelta(minutes=1)


def test_a_held_lane_does_not_fire_and_its_slot_is_collapsed(home):
    from cron.jobs import get_due_jobs, get_job

    _hold(allow_lanes=("alpha",))          # this store's lane is `default`
    jid = _job(_due_next_day())

    assert jid not in [job["id"] for job in get_due_jobs()]

    stored = get_job(jid)
    collapsed = datetime.fromisoformat(stored["next_run_at"])
    assert collapsed > _NOW, "the held slot was left overdue, so release would fire it (%s)" % (
        stored["next_run_at"],)
    assert "last_dispatch" not in stored, "a held job must not carry a dispatch record"


def test_an_admitted_lane_still_fires_under_the_same_hold(home):
    from cron.jobs import get_due_jobs

    _hold(allow_lanes=("default",))
    jid = _job(_due_next_day())

    assert jid in [job["id"] for job in get_due_jobs()], \
        "the hold admitted this lane, so its work must keep running"


def test_the_release_fires_no_missed_slot(home):
    """The collapse is durable: lifting the hold finds nothing overdue to replay."""
    from agent import estop
    from cron.jobs import get_due_jobs

    _hold(allow_lanes=("alpha",))
    jid = _job(_due_next_day())
    get_due_jobs()

    estop.release()
    assert jid not in [job["id"] for job in get_due_jobs()], \
        "release re-fired a slot that elapsed while the fleet was held"


def test_a_held_one_shot_is_neither_fired_nor_retired(home):
    from cron.jobs import get_due_jobs, get_job

    _hold(allow_lanes=("alpha",))
    jid = _job(_NOW - timedelta(hours=2))

    assert jid not in [job["id"] for job in get_due_jobs()]
    stored = get_job(jid)
    assert stored is not None, "the hold retired a one-shot instead of holding its place"
    assert not stored.get("state") == "completed", stored.get("state")


def test_no_hold_changes_nothing(home):
    from cron.jobs import get_due_jobs

    jid = _job(_due_next_day())

    assert jid in [job["id"] for job in get_due_jobs()]


def test_an_unreadable_hold_fails_OPEN(home, monkeypatch):
    """A broken estop read must not stop the fleet's scheduler: one late job beats a silent stop."""
    from agent import estop
    from cron.jobs import get_due_jobs

    def _boom(*_a, **_k):
        raise RuntimeError("estop is unreadable")

    monkeypatch.setattr(estop, "read_state", _boom)
    jid = _job(_due_next_day())

    assert jid in [job["id"] for job in get_due_jobs()], \
        "an unreadable hold must not be read as a hold"


def test_a_total_halt_holds_every_lane_including_default(home):
    """The demoted (unverified) body is a TOTAL halt, and cron must honour that too."""
    from cron.jobs import get_due_jobs
    from agent import estop

    (home / "ESTOP").write_text('{"engaged_at": 1758900000.0, "reason": "bare"}', encoding="utf-8")
    assert estop.read_state().total is True
    jid = _job(_due_next_day())

    assert jid not in [job["id"] for job in get_due_jobs()]
