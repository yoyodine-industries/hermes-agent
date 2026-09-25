"""What a failure notice says about a run that has already been replaced.

A notification rides a per-subscription cursor, so it can be delivered long after
its event: a closed desktop session, a gateway restart, a chat that was offline.
Delivered then, "worker crashed (pid gone); dispatcher will retry" describes a run
the board has moved past, and a recovered board reads as down. The contract these
tests pin: the notice carries the failed run's end time AND the card's status at
delivery, so the reader can never mistake history for the current state.
"""

import re
import time
from types import SimpleNamespace

import pytest

from hermes_cli.kanban_notice import _clock, failure_notice_text

CLOCK_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")

# Two runs of one card: the crashed one, then the retry that is (or is not) still live.
CRASH_TS = int(time.time()) - 3600
RETRY_TS = CRASH_TS + 61
TASK_ID = "t_abc123"


def _hhmmss(ts):
    """The wall clock the notice must print, read at call time (the test runner's TZ is its own)."""
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _task(status, current_run_id=None):
    return SimpleNamespace(status=status, current_run_id=current_run_id, title="build it", assignee="worker")


def _run(run_id, started_at):
    return SimpleNamespace(id=run_id, started_at=started_at, ended_at=None, status="running")


def _crashed(task, latest_run=None):
    return failure_notice_text(
        "crashed", {"retry_status": "ready", "pid": 4242},
        task=task, task_id=TASK_ID, event_ts=CRASH_TS, event_run_id=3997, latest_run=latest_run,
    )


def test_the_notice_clock_is_the_local_wall_clock():
    """HH:MM:SS, in the delivering host's own zone — never a bare epoch, never UTC."""
    assert CLOCK_RE.match(_clock(CRASH_TS))
    assert _clock(CRASH_TS) == _hhmmss(CRASH_TS)
    assert _clock(None) == "" and _clock("") == "" and _clock("nonsense") == ""


def test_crash_superseded_by_a_newer_run_reports_the_live_card():
    """The escaped case: the notice arrives while the card is already running again."""
    text = _crashed(_task("running", 3998), latest_run=_run(3998, RETRY_TS))

    assert f"run 3997 ended {_hhmmss(CRASH_TS)}" in text
    assert f"card: running since {_hhmmss(RETRY_TS)} (run 3998)" in text
    # Nothing in the notice may be read as the card being down now.
    assert "will retry" not in text.lower()
    assert "gave up" not in text.lower()


def test_crash_on_a_card_waiting_for_its_retry_says_so():
    text = _crashed(_task("ready"))

    assert f"run 3997 ended {_hhmmss(CRASH_TS)}" in text
    assert "card: ready (will be retried)" in text


def test_crash_on_a_card_the_breaker_blocked_names_the_action():
    text = _crashed(_task("blocked"))

    assert f"run 3997 ended {_hhmmss(CRASH_TS)}" in text
    assert f"card: blocked (fix the cause, then `hermes kanban unblock {TASK_ID}`" in text
    assert f"logs: `hermes kanban log {TASK_ID}`" in text


def test_crash_without_a_run_row_still_carries_the_time():
    """A crash event that names no run: the time is stated, no run is invented."""
    text = failure_notice_text(
        "crashed", {}, task=_task("ready"), task_id=TASK_ID, event_ts=CRASH_TS,
    )

    assert f"at {_hhmmss(CRASH_TS)}" in text
    assert "card: ready (will be retried)" in text
    assert "the run ended" not in text


def test_gave_up_carries_the_count_the_error_and_the_blocked_card():
    text = failure_notice_text(
        "gave_up", {"failures": 2, "error": "pid 4242 not alive\nsecond line"},
        task=_task("blocked"), task_id=TASK_ID, event_ts=CRASH_TS, latest_run=_run(3997, CRASH_TS - 61),
    )

    assert "gave up after 2 failed runs" in text
    assert "last: pid 4242 not alive" in text
    assert "second line" not in text  # first line only
    assert f"run 3997 ended {_hhmmss(CRASH_TS)}" in text
    assert "card: blocked" in text


def test_gave_up_on_a_card_already_running_again_reports_the_live_card():
    """The give-up notice can outlive the unblock: then it must say the card is running."""
    text = failure_notice_text(
        "gave_up", {"failures": 2, "error": "pid 4242 not alive"},
        task=_task("running", 4002), task_id=TASK_ID, event_ts=CRASH_TS, latest_run=_run(4002, RETRY_TS),
    )

    assert f"at {_hhmmss(CRASH_TS)}" in text
    assert f"card: running since {_hhmmss(RETRY_TS)} (run 4002)" in text
    assert "unblock" not in text


def test_timed_out_keeps_its_name_and_states_the_card():
    text = failure_notice_text(
        "timed_out", {"limit_seconds": 3600}, task=_task("ready"), task_id=TASK_ID,
        event_ts=CRASH_TS, event_run_id=3997,
    )

    assert "timed out past the 60-minute limit" in text
    assert f"run 3997 ended {_hhmmss(CRASH_TS)}" in text
    assert "card: ready (will be retried)" in text


def test_timed_out_tolerates_a_non_numeric_limit():
    text = failure_notice_text(
        "timed_out", {"limit_seconds": "not-a-number"}, task=_task("ready"), task_id=TASK_ID,
    )

    assert "timed out past its time limit" in text


def test_missing_task_row_claims_no_current_state():
    """A deleted card: state the failure, claim nothing about a status we cannot read."""
    text = failure_notice_text(
        "crashed", {}, task=None, task_id=TASK_ID, event_ts=CRASH_TS, event_run_id=3997,
    )

    assert f"run 3997 ended {_hhmmss(CRASH_TS)}" in text
    assert "card:" not in text


def test_a_non_failure_kind_is_a_caller_bug():
    with pytest.raises(ValueError):
        failure_notice_text("completed", {}, task=_task("done"), task_id=TASK_ID)
