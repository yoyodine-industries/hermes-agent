"""A job-DECLARED stand-down exit code is neither a pass nor a failure.

The defect this pins: Hermes cron counted every non-zero exit as "script failed", so a healthy
stood-down carrier tick (diddler exits 2 when its seats are held, in backoff, or there is no work
that pass) printed exactly like a real clock-node failure. ``benign_exit_codes`` is the job's own
declaration of which non-zero codes MEAN "nothing owed"; the run is then booked ``stood_down`` — its
own status, stdout/stderr kept, no failure stamp, no streak, no alert.

Covers:
* the scheduler's classification (declared benign -> stood down; undeclared -> failed; 0 unchanged);
* the declaration is EXACT (a declared 2 does not bless a 3) and only for ``no_agent`` jobs;
* the booking through ``_run_one_job_body`` against a real store: ``last_status == 'stood_down'``,
  no ``failure_streak``/``last_failure``, and the output document says stood down;
* the control case: an undeclared non-zero still books a failure and still goes red.
"""

from __future__ import annotations

import pytest

from cron import executions, jobs, scheduler
from cron.scheduler_script import ScriptRunResult, declared_benign_exit_codes


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME so jobs/scripts/outputs do not leak between tests."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    return home


def _write_script(home, name: str, code: int, stdout: str = "", stderr: str = "") -> str:
    """A shell script that prints its departure line and exits with *code*."""
    path = home / "scripts" / name
    lines = ["#!/bin/bash"]
    if stdout:
        lines.append(f'echo "{stdout}"')
    if stderr:
        lines.append(f'echo "{stderr}" >&2')
    lines.append(f"exit {code}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)
    return name


# ---------------------------------------------------------------------------
# The declaration itself: parsed exactly, never widened
# ---------------------------------------------------------------------------


def test_declared_codes_are_exact_and_never_include_zero():
    assert declared_benign_exit_codes({"benign_exit_codes": [2]}) == frozenset({2})
    # Accepts the loose shapes a hand-written jobs.json may carry...
    assert declared_benign_exit_codes({"benign_exit_codes": 2}) == frozenset({2})
    assert declared_benign_exit_codes({"benign_exit_codes": ["2"]}) == frozenset({2})
    # ...but 0 is success already, and junk on READ must not crash a tick.
    assert declared_benign_exit_codes({"benign_exit_codes": [0, "x", 999]}) == frozenset()
    assert declared_benign_exit_codes({}) == frozenset()
    assert declared_benign_exit_codes(None) == frozenset()


def test_create_and_update_refuse_a_widening_typo_and_agent_jobs(hermes_env):
    from cron.jobs import create_job, update_job

    script = _write_script(hermes_env, "tick.sh", 2)
    for bad in ([0], [256], ["two"], [-1]):
        with pytest.raises(ValueError, match="benign_exit_codes"):
            create_job(prompt=None, schedule="every 20m", no_agent=True, script=script,
                       benign_exit_codes=bad)
    # An agent job's script is a wake gate, not the run's verdict: the knob would be inert, so it is
    # refused rather than stored.
    with pytest.raises(ValueError, match="benign_exit_codes only applies to no_agent"):
        create_job(prompt="report", schedule="every 20m", script=script, benign_exit_codes=[2])

    job = create_job(prompt=None, schedule="every 20m", no_agent=True, script=script,
                     benign_exit_codes=[2])
    assert job["benign_exit_codes"] == [2]
    job = update_job(job["id"], {"benign_exit_codes": []})
    assert not job.get("benign_exit_codes")


# ---------------------------------------------------------------------------
# Classification at the scheduler's no-agent seam
# ---------------------------------------------------------------------------


def _no_agent_job(**overrides):
    job = {"id": "tick", "name": "carrier", "no_agent": True, "script": "tick.sh",
           "deliver": "local", "schedule": {"kind": "interval", "minutes": 20}, "repeat": {}}
    job.update(overrides)
    return job


def _patch_runner(monkeypatch, result):
    monkeypatch.setattr(
        scheduler, "_run_job_script_with_claim_heartbeat", lambda job, path, **kw: result)
    monkeypatch.setattr(scheduler, "_resolve_job_workdir", lambda job, job_id: None)


def test_declared_benign_exit_is_booked_as_stood_down_not_a_failure(hermes_env, monkeypatch):
    _patch_runner(monkeypatch, ScriptRunResult(
        False, "Script exited with code 2\ndeparture: seats held", exit_code=2))
    job = _no_agent_job(benign_exit_codes=[2])

    success, doc, final_response, error = scheduler._run_no_agent_job(
        job, "tick", "carrier", None)

    assert success is True and error is None
    assert "**Status:** stood down (exit 2" in doc
    # The tick's own output is kept in the document that lands in the cron output dir.
    assert "departure: seats held" in doc
    # Silent: a stood-down tick must not page anyone.
    assert final_response == scheduler.SILENT_MARKER
    # The marker the bookkeeping tail turns into last_status='stood_down'.
    assert job["_stood_down"] == {"exit_code": 2}


def test_an_undeclared_non_zero_is_still_a_failure(hermes_env, monkeypatch):
    _patch_runner(monkeypatch, ScriptRunResult(
        False, "Script exited with code 2\nclock refused the write", exit_code=2))
    job = _no_agent_job()

    success, doc, alert, error = scheduler._run_no_agent_job(job, "tick", "carrier", None)

    assert success is False
    assert "**Status:** script failed" in doc
    assert "script failed" in alert  # operator alert preserved
    assert "_stood_down" not in job


def test_the_declaration_is_exact_not_any_non_zero(hermes_env, monkeypatch):
    """Declaring 2 does not bless a 3: a different non-zero code is still a failure."""
    _patch_runner(monkeypatch, ScriptRunResult(False, "Script exited with code 3", exit_code=3))
    job = _no_agent_job(benign_exit_codes=[2])

    success, doc, _, _ = scheduler._run_no_agent_job(job, "tick", "carrier", None)

    assert success is False and "script failed" in doc


def test_exit_zero_is_unchanged_by_a_declaration(hermes_env, monkeypatch):
    _patch_runner(monkeypatch, ScriptRunResult(True, "nothing to report", exit_code=0))
    job = _no_agent_job(benign_exit_codes=[2])

    success, doc, final_response, error = scheduler._run_no_agent_job(job, "tick", "carrier", None)

    assert success is True and error is None
    assert "stood down" not in doc and "_stood_down" not in job


def test_a_run_that_never_produced_a_code_is_never_a_stand_down(hermes_env, monkeypatch):
    """Timeout/cancel/exception carry no exit code; a declaration must not swallow them."""
    _patch_runner(monkeypatch, ScriptRunResult(False, "Script timed out after 60s", exit_code=None))
    job = _no_agent_job(benign_exit_codes=[2])

    success, doc, _, _ = scheduler._run_no_agent_job(job, "tick", "carrier", None)

    assert success is False and "script failed" in doc


# ---------------------------------------------------------------------------
# Booking: the run lands in the record and the output dir as STOOD DOWN
# ---------------------------------------------------------------------------


def _run_body(home, job) -> str:
    fire = executions.create_execution(job["id"], source="test")["id"]
    # Let the body win the claimed->running CAS itself (calling mark_execution_running here would
    # make the body's CAS a second attempt, which returns None and skips the run).
    assert scheduler._run_one_job_body(dict(jobs.get_job(job["id"]), execution_id=fire)) is True
    return fire


def test_a_stood_down_tick_books_its_own_status_and_keeps_its_output(hermes_env):
    script = _write_script(hermes_env, "carrier.sh", 2,
                           stdout="departure: platform 3 seats held, 2 in backoff",
                           stderr="tick: nothing actionable")
    job = jobs.create_job(prompt=None, schedule="every 20m", no_agent=True, script=script,
                          benign_exit_codes=[2], deliver="local")

    fire = _run_body(hermes_env, job)

    booked = jobs.get_job(job["id"])
    assert booked["last_status"] == "stood_down"
    assert not booked.get("failure_streak")
    assert booked.get("last_failure") is None
    assert booked.get("last_error") is None
    assert "_stood_down" not in booked
    # Not a failed ledger row either: the run itself completed.
    assert executions.get_execution(fire)["status"] == "completed"

    docs = list(jobs._job_output_dir(job["id"]).glob("*.md"))
    assert docs, "the tick's output document must be kept"
    text = docs[0].read_text()
    assert "**Status:** stood down (exit 2" in text
    assert "departure: platform 3 seats held" in text
    assert "tick: nothing actionable" in text


def test_an_undeclared_failure_still_books_red(hermes_env):
    """Control: the same script shape with NO declaration must stay a failure."""
    script = _write_script(hermes_env, "broken.sh", 2, stdout="clock refused the board write")
    job = jobs.create_job(prompt=None, schedule="every 20m", no_agent=True, script=script,
                          deliver="local")

    _run_body(hermes_env, job)

    booked = jobs.get_job(job["id"])
    assert booked["last_status"] not in ("ok", "stood_down")
    assert int(booked.get("failure_streak") or 0) >= 1
    assert booked.get("last_failure") is not None
    docs = list(jobs._job_output_dir(job["id"]).glob("*.md"))
    assert docs and "**Status:** script failed" in docs[0].read_text()


def test_exit_zero_still_books_ok(hermes_env):
    script = _write_script(hermes_env, "fine.sh", 0, stdout="all seats closed")
    job = jobs.create_job(prompt=None, schedule="every 20m", no_agent=True, script=script,
                          benign_exit_codes=[2], deliver="local")

    _run_body(hermes_env, job)

    assert jobs.get_job(job["id"])["last_status"] == "ok"
