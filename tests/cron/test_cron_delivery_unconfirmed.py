"""A delivery whose confirmation never came back is UNCONFIRMED, not failed.

A ``no_agent`` sweep job exited 0 with an ALERT line, its Bot Chat delivery was handed to
the delivery runner, and the reply wait expired — the job row then recorded
``last_status=error``/``delivery_failed`` plus a growing ``failure_streak`` for a message
that had already reached its target. The operator reads that as "the delivery failed" and
re-sends a message that *was* delivered.

These tests pin the three-way split the delivery status now needs:

* a script that exits 0 with an ALERT on stdout stays ``ok`` and keeps the alert;
* a delivery the runner accepted but never confirmed is ``delivery_unconfirmed`` — never
  ``delivery_failed`` (which invites a duplicate re-send) and never ``ok`` (the completion
  is genuinely unknown), with ``failure_streak`` untouched;
* a delivery that genuinely did not go out is still ``delivery_failed``.
"""

from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

ALERT = "ALERT: card wt/stale is sitting in review past its SLA"


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME so jobs/scripts/output don't leak into the live store."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Reload modules that cached get_hermes_home() at import time (same harness as
    # tests/cron/test_cron_no_agent.py).
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    # An ALERT line on stdout, exit 0: the alert must survive as the run output.
    (home / "scripts" / "sweep.sh").write_text(
        "#!/bin/bash\n" f"echo '{ALERT}'\n" "exit 0\n")
    return home


def _sweep_job(*, deliver="local"):
    from cron.jobs import create_job

    return create_job(
        prompt=None, schedule="every 5m", script="sweep.sh", no_agent=True,
        deliver=deliver, name="kanban-disposition",
    )


def _row(job_id):
    from cron.jobs import load_jobs

    return next(job for job in load_jobs() if job["id"] == job_id)


def _saved_output(job_id):
    from cron.jobs import _job_output_dir

    files = sorted(_job_output_dir(job_id).glob("*.md"))
    assert files, "the run saved no output"
    return files[-1].read_text()


def _block_bot_chat_cli(monkeypatch, run):
    """Route bot-chat deliveries through the legacy CLI lane with a stubbed transport.

    ``run`` stands in for the ``hermes chat`` subprocess: no live owner is discoverable in
    a tmp home, so the delivery falls back to the unowned CLI lane the live incident hit.
    """
    from cron import scheduler_delivery as delivery
    from gateway import config as gateway_config
    from tools import bot_live_delivery as live

    monkeypatch.setattr(live, "find_canonical_live_owner", lambda home: None)
    monkeypatch.setattr(gateway_config, "load_gateway_config", lambda *a, **k: None)
    monkeypatch.setattr(delivery.subprocess, "run", run)


def _tick(scheduler, job):
    """Run one job the way the scheduler does, without the tick loop."""
    assert scheduler.run_one_job(job) is True


# ---------------------------------------------------------------------------
# The script-exit contract: exit 0 + ALERT on stdout is ok
# ---------------------------------------------------------------------------

def test_alert_script_exiting_zero_stays_ok_and_keeps_the_alert(hermes_env):
    import cron.scheduler as scheduler

    job = _sweep_job()
    _tick(scheduler, job)

    row = _row(job["id"])
    assert row["last_status"] == "ok"
    assert not row.get("last_error")
    assert int(row.get("failure_streak") or 0) == 0
    assert ALERT in _saved_output(job["id"])


# ---------------------------------------------------------------------------
# The reply-wait timeout: admitted, unconfirmed
# ---------------------------------------------------------------------------

def test_reply_wait_timeout_books_delivery_unconfirmed_not_failed(hermes_env, monkeypatch):
    """End-to-end (the live incident): exit 0 + Bot Chat reply-wait timeout."""
    import cron.scheduler as scheduler

    job = _sweep_job(deliver="bot-chat")
    _block_bot_chat_cli(
        monkeypatch,
        Mock(side_effect=subprocess.TimeoutExpired(cmd="hermes chat", timeout=1800)),
    )
    _tick(scheduler, job)

    row = _row(job["id"])
    assert row["last_status"] == "delivery_unconfirmed", row
    assert row["last_status"] != "delivery_failed"
    assert int(row.get("failure_streak") or 0) == 0
    # The diagnostic is kept, and it tells the operator not to re-send.
    delivery_error = row["last_delivery_error"]
    assert "timed out" in delivery_error
    assert "do NOT resend" in delivery_error
    assert not row.get("last_error")


def test_timeout_arm_records_the_admission_evidence(hermes_env, monkeypatch):
    """The timeout arm leaves the evidence the status decision reads."""
    from cron import scheduler_delivery as delivery

    _block_bot_chat_cli(
        monkeypatch,
        Mock(side_effect=subprocess.TimeoutExpired(cmd="hermes chat", timeout=1800)),
    )
    job = {"id": "sweep", "name": "kanban-disposition", "execution_id": "run-1"}

    error = delivery._deliver_to_bot_chat(job, "payload", "")

    assert error and "timed out" in error
    assert "do NOT resend" in error
    assert "handed to" in error
    assert job[delivery.BOT_CHAT_UNCONFIRMED_KEY]["bot-chat:(own)"] == error
    # The CLI lane is not a receipt: nothing here may claim a durable receipt exists.
    assert job.get("_bot_chat_delivery_receipts", {}) == {}


def test_a_genuine_send_failure_is_still_delivery_failed(hermes_env, monkeypatch):
    """Regression guard: only an expired CONFIRMATION is unconfirmed."""
    import cron.scheduler as scheduler

    job = _sweep_job(deliver="bot-chat")
    _block_bot_chat_cli(
        monkeypatch,
        Mock(return_value=Mock(returncode=1, stdout="", stderr="no such session")),
    )
    _tick(scheduler, job)

    row = _row(job["id"])
    assert row["last_status"] == "delivery_failed"
    assert row["last_status"] != "delivery_unconfirmed"
    assert "failed (exit 1)" in row["last_delivery_error"]


# ---------------------------------------------------------------------------
# The classifier: the new outcome, and the ordering that protects it
# ---------------------------------------------------------------------------

def test_classifier_needs_admission_evidence_to_soften_a_delivery_error():
    from cron.scheduler import _classify_delivery_outcome

    def _call(**over):
        return _classify_delivery_outcome(
            should_deliver=True, unresolved_origin=False, normalized_deliver="bot-chat",
            incident_acked=False, success=True, **over)

    assert _call(
        delivery_error="bot-chat ... timed out",
        delivery_unconfirmed="bot-chat ... timed out") == "unconfirmed"
    # No admission evidence: unchanged, still a failure.
    assert _call(delivery_error="bot-chat ... timed out") == "failed"
    # A successful run with nothing to report is still delivered/suppressed, never unconfirmed.
    assert _call(delivery_error=None) == "delivered"
