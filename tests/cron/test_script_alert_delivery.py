"""A script's alert rides the durable lane, so the run closes at the script's exit (t_e9d06d80).

Measured 2026-10-23 on job 50b61ee711ae (``no_agent`` script ``heavy-pass-cadence``): the
script exited 04:36:20; its executions.db row stayed ``status=running`` until 04:46:20 because
the alert delivery ran a full bot-chat agent turn inline under a 600 s bound. That row is the
lull gate's LEDGER signal, so a job that had finished blocked a sanctioned reload for ten
minutes — and the turn's own timeout cost the operator the diagnosis.

Invariants:
1. A ``no_agent`` job's bot-chat delivery is QUEUED (durable, retryable) and the turn is not run
   in the run's path; the job carries ``last_delivery_queued`` so the run books
   ``last_status=delivery_queued`` / ``delivery_outcome=queued`` at the script's exit.
2. An agent job keeps the inline lane: its delivery turn IS the review the operator asked for.
3. A drained record still runs the turn — queueing is once per delivery, not a hop.
4. The delivery bound is well below the 600 s it replaces, and config still wins.
"""
import subprocess
from unittest.mock import Mock

import pytest

from cron import scheduler_delivery as delivery


@pytest.fixture()
def cli_lane(tmp_path, monkeypatch):
    """Route _deliver_to_bot_chat into the CLI fallback lane with a turn that would succeed."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    turn = Mock(return_value=subprocess.CompletedProcess(["hermes"], 0, "posted", ""))
    monkeypatch.setattr(delivery, "_run_bot_chat_turn", turn)
    return tmp_path, turn


def _capture_defer(monkeypatch):
    from cron import bot_chat_delivery as queue
    calls = []
    monkeypatch.setattr(queue, "defer", Mock(
        side_effect=lambda key, job, content, profile, home, **kw:
        calls.append(dict(key=key, content=content, kw=kw)) or {"id": key, "status": "queued"}))
    return calls


def test_script_job_queues_instead_of_running_the_turn(cli_lane, monkeypatch):
    _home, turn = cli_lane
    calls = _capture_defer(monkeypatch)
    from cron import jobs as jobs_mod
    persisted = []
    monkeypatch.setattr(jobs_mod, "update_job", Mock(
        side_effect=lambda job_id, fields: persisted.append((job_id, fields)) or True))

    job = {"id": "job-script", "name": "heavy-pass-cadence",
           "execution_id": "exec-script", "no_agent": True}

    result = delivery._deliver_to_bot_chat(job, "script exited rc=1: tests/test_sdlc_nodes.py", "")

    assert result is None                       # completed: the run must not wait on a turn
    turn.assert_not_called()
    assert len(calls) == 1
    assert calls[0]["content"] == "script exited rc=1: tests/test_sdlc_nodes.py"
    assert calls[0]["kw"].get("degraded") is not True  # a queued result, not a degraded fallback
    target = "bot-chat:(own)"
    assert job["_bot_chat_delivery_receipts"][target] == {
        "status": "queued", "delivery_id": calls[0]["key"]}

    # The run tail reads `last_delivery_queued` to book status delivery_queued / outcome queued;
    # `_deliver_result` derives it from these receipts, so prove that derivation here.
    assert delivery._record_delivery_verification(job, []) is None
    assert job["last_delivery_queued"] == {
        target: {"status": "queued", "delivery_id": calls[0]["key"]}}
    assert persisted[-1] == ("job-script", {"last_delivery_queued": job["last_delivery_queued"]})
    from cron.scheduler import _classify_delivery_outcome
    assert _classify_delivery_outcome(
        delivery_error=None, should_deliver=True, unresolved_origin=False,
        normalized_deliver="bot-chat", incident_acked=False, success=True,
        delivery_queued=job["last_delivery_queued"]) == "queued"


def test_agent_job_keeps_the_inline_turn(cli_lane, monkeypatch):
    _home, turn = cli_lane
    calls = _capture_defer(monkeypatch)

    job = {"id": "job-agent", "name": "local-model-upgrade-research", "execution_id": "exec-a"}

    result = delivery._deliver_to_bot_chat(job, "research finished", "")

    assert result is None
    turn.assert_called_once()
    assert calls == []                          # an agent job is not diverted to the queue
    assert "last_delivery_queued" not in job


def test_drained_record_for_a_script_job_still_runs_the_turn(cli_lane, monkeypatch):
    home, turn = cli_lane
    calls = _capture_defer(monkeypatch)
    from hermes_state import SessionDB
    SessionDB(db_path=home / "state.db").close()

    record = {"id": "b" * 64, "home": str(home), "profile": "", "degraded": False}
    job = {"id": "job-script", "name": "heavy-pass-cadence",
           "execution_id": "exec-script", "no_agent": True}

    result = delivery._deliver_to_bot_chat(job, "queued output", "", deferred=record)

    assert result is None
    turn.assert_called_once()
    assert calls == []                          # no second hop


def test_a_queued_failure_ping_counts_as_alerted():
    """Otherwise a script job failing on every run re-pings on every run.

    The run hands the alert to the durable queue and cannot take it further, so the incident
    cooldown (`cron.failure_repeat_alert_hours`) is what dedupes from then on.
    """
    from cron.scheduler import _failure_ping_alerted

    assert _failure_ping_alerted("queued") is True
    assert _failure_ping_alerted("delivered") is True
    assert _failure_ping_alerted("not_configured") is True
    assert _failure_ping_alerted("failed") is False
    assert _failure_ping_alerted("suppressed") is False


def test_delivery_bound_is_well_below_the_600s_it_replaces(monkeypatch):
    monkeypatch.setattr(delivery._sched, "load_config", dict)
    assert delivery._get_bot_chat_delivery_timeout() == 180

    monkeypatch.setattr(
        delivery._sched, "load_config",
        lambda: {"cron": {"bot_chat_delivery_timeout_seconds": 900}})
    assert delivery._get_bot_chat_delivery_timeout() == 900  # config still wins
