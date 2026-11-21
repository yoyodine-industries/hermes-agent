"""A /v1/runs task whose own run status is TERMINAL holds no gate slot (t-2fd3bc0b28).

Measured shape the rule answers: 8 of 16 slots held for up to 5.4 hours by runs of ONE
session, every holder past the latch threshold, while the runs themselves had settled.
A finished run can never become work again, so counting its lingering task is a latch.
"""

import logging

from gateway.platforms.api_server import _GATE_TERMINAL_RUN_STATUSES, APIServerAdapter


class _PendingTask:
    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _UnreadableTask:
    def done(self) -> bool:
        raise RuntimeError("task state unreadable")


class _Stub:
    """Only the attributes and the one logging method ``_live_run_task_count`` uses."""

    _note_gate_released_runs = APIServerAdapter._note_gate_released_runs

    def __init__(self, tasks, statuses) -> None:
        self._active_run_tasks = tasks
        self._run_statuses = statuses


def test_terminal_run_task_releases_its_slot(caplog):
    stub = _Stub(
        {"run_a": _PendingTask(), "run_b": _PendingTask()},
        {"run_a": {"status": "completed"}, "run_b": {"status": "running"}},
    )
    with caplog.at_level(logging.WARNING):
        live = APIServerAdapter._live_run_task_count(stub)
    assert live == 1, "only the running run may hold a slot"
    assert "released 1 finished run task(s)" in caplog.text
    assert "live_runs 2 -> 1" in caplog.text, "the reap must log the count before and after"
    assert "run_a" in caplog.text


def test_release_is_logged_once_per_run_id(caplog):
    stub = _Stub({"run_a": _PendingTask()}, {"run_a": {"status": "interrupted"}})
    with caplog.at_level(logging.WARNING):
        APIServerAdapter._live_run_task_count(stub)
        APIServerAdapter._live_run_task_count(stub)
    assert caplog.text.count("released 1 finished run task(s)") == 1, (
        "the heartbeat re-measures every cycle; the release must not grow the log with uptime"
    )


def test_live_and_unreadable_and_done_tasks_keep_their_existing_reading():
    stub = _Stub(
        {
            "run_a": _PendingTask(),          # live, no terminal status
            "run_b": _UnreadableTask(),       # unreadable: not live work
            "run_c": _PendingTask(done=True),  # already done: never counted
        },
        {"run_a": {"status": "running"}},
    )
    assert APIServerAdapter._live_run_task_count(stub) == 1


def test_every_terminal_status_releases():
    for status in sorted(_GATE_TERMINAL_RUN_STATUSES):
        stub = _Stub({"run_x": _PendingTask()}, {"run_x": {"status": status}})
        assert APIServerAdapter._live_run_task_count(stub) == 0, status


def test_a_missing_status_row_keeps_the_slot():
    stub = _Stub({"run_x": _PendingTask()}, {})
    assert APIServerAdapter._live_run_task_count(stub) == 1
