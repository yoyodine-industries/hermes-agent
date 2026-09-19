"""Every operator/agent-facing surface must render ``delivery_unconfirmed`` honestly.

The status means: the agent run succeeded and the payload was handed to its target, but the
confirmation never came back — the message may already be in the chat, so it is NOT a failure
(do not re-send) and NOT ``ok``. A surface that does not know the literal misreports it:

* ``hermes cron list`` colour/reason helper → would print ``delivery_unconfirmed: None`` (it
  reads ``last_error``, which is None for a delivery-only outcome);
* the doctor's "last run failed" allowlist → would report the run itself as failed;
* the delivery-warning block → would say "Delivery failed: ..." over a delivered message;
* ``/cron list`` → would show a bare literal with no reason;
* ``cronjob(action='run')`` → the calling agent relays ``error=None`` as an unexplained
  failure, instead of the do-not-resend diagnostic;
* cron health telemetry → an unknown literal collapses to ``delivery_outcome=None``, i.e. the
  delivery outcome disappears from the export.
"""
from __future__ import annotations

import sys
from unittest.mock import patch

REASON = (
    "bot-chat delivery to profile 'default' timed out after 300s waiting for the completion "
    "confirmation: the payload was already handed to the Bot Chat runner — do NOT resend it"
)

_JOB = {
    "job_id": "job-unconf-1",
    "name": "probe",
    "state": "active",
    "schedule": "every 1h",
    "repeat": "forever",
    "next_run_at": "2026-09-19T10:00:00-04:00",
    "last_run_at": "2026-09-19T09:00:00-04:00",
    "last_status": "delivery_unconfirmed",
    "last_error": None,
    "last_delivery_error": REASON,
}


def test_cron_list_display_names_the_status_and_keeps_the_reason():
    """``hermes cron list``'s per-job display: not a failure, not a bare ``None``."""
    from hermes_cli.cron import _last_run_display

    display = _last_run_display(dict(_JOB))

    assert "delivery_unconfirmed" in display
    assert "do NOT resend" in display
    # The generic fall-through reads last_error (None here) and paints it as a failure.
    assert "None" not in display
    assert "? :" not in display


def test_cron_list_warning_line_says_unconfirmed_not_failed():
    """The warning block must not read "Delivery failed" over an admitted delivery."""
    from hermes_cli.cron import _job_warnings

    lines = _job_warnings(dict(_JOB))

    assert lines, "an unconfirmed delivery must still warn the operator"
    joined = "\n".join(lines)
    assert "do NOT resend" in joined
    assert "Delivery failed" not in joined


def test_doctor_does_not_report_the_run_as_failed():
    """The doctor's allowlist: the RUN succeeded; only its confirmation is missing."""
    from hermes_cli.cron import _cron_doctor_issues_for_job

    issues = _cron_doctor_issues_for_job(dict(_JOB))
    joined = "\n".join(issues)

    assert "last run failed" not in joined
    assert "last delivery failed" not in joined
    assert any("unconfirmed" in issue for issue in issues)
    assert any("do NOT resend" in issue for issue in issues)


def test_cron_list_slash_command_surfaces_the_reason(capsys):
    """``/cron list`` in the chat CLI: the reason rides along, as it does for delivery_failed."""
    from hermes_cli import cli_commands_mixin as mixin

    with patch.object(mixin, "_cron_api", lambda **kwargs: {"success": True, "jobs": [dict(_JOB)]}):
        mixin.CLICommandsMixin._cron_list(
            object.__new__(mixin.CLICommandsMixin), "list", {"all": False})

    out = capsys.readouterr().out
    assert "delivery_unconfirmed" in out
    assert "do NOT resend" in out


def test_cronjob_run_returns_the_do_not_resend_diagnostic():
    """The calling agent relays this result — an unexplained failure is not good enough."""
    from tools.cronjob_tools import _execute_job_now

    refreshed = {"id": "job-unconf-1", "last_status": "delivery_unconfirmed",
                 "last_error": None, "last_delivery_error": REASON}
    claimed = {**_JOB, "id": "job-unconf-1", "prompt": "hi",
               "fire_claim": {"by": "manual-owner"}}

    with patch("tools.cronjob_tools.claim_job_for_fire", return_value=claimed), \
         patch.dict(sys.modules, {"gateway.run": None}), \
         patch("cron.scheduler.run_one_job", return_value=True), \
         patch("tools.cronjob_tools.get_job", return_value=refreshed):
        res = _execute_job_now({**_JOB, "id": "job-unconf-1", "prompt": "hi"})

    assert res["claimed"] is True
    assert res["success"] is False
    assert "do NOT resend" in (res["error"] or "")


def test_health_export_keeps_the_outcome_instead_of_dropping_it():
    """An unknown literal projects to delivery_outcome=None — the outcome vanishes."""
    from agent.monitoring.cron_health import project_execution_event

    event = project_execution_event(
        {
            "id": "execution-1",
            "job_id": "job-unconf-1",
            "source": "builtin",
            "status": "completed",
            "claimed_at": "2026-09-19T09:00:00+00:00",
            "started_at": "2026-09-19T09:00:01+00:00",
            "finished_at": "2026-09-19T09:00:03.250000+00:00",
            "error": None,
        },
        delivery_outcome="unconfirmed",
    ).to_dict()

    assert event["delivery_outcome"] == "unconfirmed"
