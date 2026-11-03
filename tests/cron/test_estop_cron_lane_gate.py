"""The lane-scoped ESTOP gate at the CRON seam (card t_7f6ccfc6).

``check_paused`` is the TOTAL-halt gate: it stops the whole tick. A scoped ``lockdown`` is not
that — the tick must keep running and every due item is admitted or held by its LANE. These
tests pin the four load-bearing properties of the cron door:

1. the lane of a job is the profile store that holds it (``…/profiles/<name>``), never the
   ambient ``HERMES_PROFILE`` of whoever is ticking, and an explicit ``profile`` wins;
2. an allowlisted lane's job FIRES, a held lane's job is HELD, in the same tick;
3. a held job is left due — the hold lands before the advance, so its catch-up is never eaten;
4. a job whose lane cannot be resolved is HELD (fail-closed), and a legacy total hold holds
   everything.

Measured against the bytes under test in a private HERMES_HOME; no live sentinel is touched.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from agent import estop
from cron import scheduler_tick
from cron.jobs import job_lane, use_cron_store


@pytest.fixture(autouse=True)
def operator_context(monkeypatch):
    """This suite arms holds, so it must not inherit a dispatched worker's fence env."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "operator")
    estop._logged_components.clear()
    estop._lockdown_logged.clear()
    scheduler_tick._ESTOP_HELD_LOGGED.clear()
    return home


class _Sched:
    """The tick's logger surface, nothing else (the split reads no scheduler state)."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("cron.test.estop-lane")


def _job(job_id: str, **fields) -> dict:
    job = {"id": job_id, "name": f"job-{job_id}", "next_run_at": "2026-11-03T00:00:00+00:00"}
    job.update(fields)
    return job


def _lockdown(lanes) -> str:
    return estop.acquire(
        owner="ops-head", actor="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
        allow={"profiles": list(lanes)})


def _legacy_total(reason: str = "thundering herd") -> None:
    """Seed a PRE-RULING total body — no verb may arm one any more (ruling 2026-10-10)."""
    body = {"reason": reason, "engaged_at": datetime.now(UTC).isoformat()}
    estop.sentinel_path().write_text(json.dumps(body) + "\n", encoding="utf-8")


# ── 1. the lane of a job ────────────────────────────────────────────────────


def test_the_store_lane_is_the_profile_the_store_belongs_to(hermes_home, tmp_path):
    with use_cron_store(hermes_home / "profiles" / "platform-stl"):
        assert job_lane({}) == "platform-stl", "a per-profile store names its own lane"

    with use_cron_store(hermes_home):
        assert job_lane({}) == "default", "the host home is the default lane"


def test_an_explicit_profile_wins_over_the_store(hermes_home):
    assert job_lane({"profile": "research-coder"}) == "research-coder"
    assert job_lane({"profile": "  research-coder  "}) == "research-coder"


def test_the_lane_never_depends_on_the_ambient_profile_env(hermes_home, monkeypatch):
    """A multiplex tick reads every profile's store; the answer comes from the STORE path."""
    monkeypatch.setenv("HERMES_PROFILE", "some-other-lane")
    with use_cron_store(hermes_home / "profiles" / "platform-coder"):
        assert job_lane({}) == "platform-coder"


# ── 2. the gate itself ──────────────────────────────────────────────────────


def test_an_admitted_lane_fires_while_a_held_lane_is_held_in_the_same_tick(hermes_home):
    _lockdown(["platform-stl"])
    admitted = _job("j-admitted", profile="platform-stl")
    held = _job("j-held", profile="platform-coder")

    kept, held_back = scheduler_tick._split_jobs_by_estop_lane(_Sched(), [admitted, held])

    assert kept == [admitted], "the allowlisted lane's job runs"
    assert [(job["id"], lane) for job, lane in held_back] == [("j-held", "platform-coder")]
    assert held["next_run_at"] == "2026-11-03T00:00:00+00:00", (
        "a held job keeps its due time: the hold lands BEFORE the advance, so the catch-up is "
        "not eaten by a fire that never happened")


def test_the_store_lane_decides_for_a_job_that_names_none(hermes_home):
    _lockdown(["platform-stl"])
    with use_cron_store(hermes_home / "profiles" / "platform-coder"):
        kept, held_back = scheduler_tick._split_jobs_by_estop_lane(_Sched(), [_job("j1")])
    assert kept == [] and [lane for _job_, lane in held_back] == ["platform-coder"]


def test_a_job_with_no_resolvable_lane_is_held(hermes_home, monkeypatch):
    """Fail-closed: an unassignable item is never silently run."""
    _lockdown(["platform-stl"])

    def _boom():
        raise RuntimeError("store unreadable")

    monkeypatch.setattr("cron.jobs._current_cron_store", _boom)
    kept, held_back = scheduler_tick._split_jobs_by_estop_lane(_Sched(), [_job("j-nolane")])
    assert kept == [] and [lane for _job_, lane in held_back] == [""]


def test_a_legacy_total_hold_holds_every_job(hermes_home):
    _legacy_total()
    jobs = [_job("j1", profile="default"), _job("j2", profile="platform-stl")]
    kept, held_back = scheduler_tick._split_jobs_by_estop_lane(_Sched(), jobs)
    assert kept == [] and len(held_back) == 2, "a total halt names no exemptions"


def test_no_hold_admits_every_job_and_reads_no_ledger_write(hermes_home):
    jobs = [_job("j1", profile="platform-coder"), _job("j2", profile="default")]
    kept, held_back = scheduler_tick._split_jobs_by_estop_lane(_Sched(), jobs)
    assert kept == jobs and held_back == []


def test_the_hold_is_reported_once_per_engagement(hermes_home, caplog):
    """Starvation is visible but not noisy: one line per (engagement, job)."""
    _lockdown(["platform-stl"])
    sched = _Sched()
    with caplog.at_level(logging.INFO, logger="cron.test.estop-lane"):
        scheduler_tick._split_jobs_by_estop_lane(sched, [_job("j-held", profile="held-lane")])
        scheduler_tick._split_jobs_by_estop_lane(sched, [_job("j-held", profile="held-lane")])
    lines = [row.message for row in caplog.records if "lockdown holds cron job" in row.message]
    assert len(lines) == 1, f"one report per engagement, got {lines}"
    assert "held-lane" in lines[0]
