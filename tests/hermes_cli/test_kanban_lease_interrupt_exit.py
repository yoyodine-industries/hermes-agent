"""A turn the LEASE REFRESHER killed is a state-store condition, not a card failure.

Measured defect (card t_8f8215e7): a Kanban worker died with
``Session turn lease could not be refreshed`` inside one long terminal call, exited 130 twice,
and the dispatcher booked an unexplained crash, spent ``kanban.failure_limit`` and parked a
healthy card as ``blocked``. The cause is the state.db write lock (renewal path), the report is
this exit code: 130 says "the card failed", which is a runtime death reported as a dependency.
"""
import pytest

from hermes_cli.cli_single_query import _single_query_exit_code

# Importing the Kanban exit constants probes this checkout's own git plumbing, which resolves
# under the real home when the suite runs from a worktree inside the packaged repo: no test here
# writes outside a temp dir, so allow the probe rather than moving the constant out of reach.
pytestmark = pytest.mark.allow_real_home_io


LEASE_MSG = "Session turn lease could not be refreshed; stopping to protect the transcript."


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fixture")


@pytest.fixture
def not_worker(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)


def test_lease_interrupt_maps_to_storage_locked_for_a_worker(worker):
    from hermes_cli.kanban_db import KANBAN_STORAGE_LOCKED_EXIT_CODE

    result = {
        "interrupted": True,
        "interrupt_reason": "session turn lease lost",
        "interrupt_message": LEASE_MSG,
    }
    assert _single_query_exit_code(result) == KANBAN_STORAGE_LOCKED_EXIT_CODE


def test_lease_interrupt_maps_by_message_for_an_older_worker_build(worker):
    from hermes_cli.kanban_db import KANBAN_STORAGE_LOCKED_EXIT_CODE

    # No machine-readable reason: the message prefix alone must still classify it.
    assert _single_query_exit_code({"interrupted": True, "interrupt_message": LEASE_MSG}) == \
        KANBAN_STORAGE_LOCKED_EXIT_CODE


def test_user_interrupt_still_exits_130(worker):
    # A genuine cancel (no lease cause) keeps its old code so the dispatcher books it as before.
    assert _single_query_exit_code({"interrupted": True, "interrupt_message": "Stopped by user."}) == 130
    assert _single_query_exit_code({"interrupted": True}) == 130


def test_lease_interrupt_outside_a_worker_still_exits_130(not_worker):
    # The storage exit contract is the dispatcher's; a human CLI run keeps 130.
    assert _single_query_exit_code({"interrupted": True, "interrupt_reason": "session turn lease lost"}) == 130


def test_lease_interrupt_does_not_need_a_failure_reason(worker):
    """The crash shape is ``interrupted`` with NO ``failure_reason``: the reason branch further
    down never ran, which is exactly why the card died as an unexplained 130."""
    from hermes_cli.kanban_db import KANBAN_STORAGE_LOCKED_EXIT_CODE

    result = {"interrupted": True, "interrupt_reason": "session turn lease lost",
              "failure_reason": None, "failed": False}
    assert _single_query_exit_code(result) == KANBAN_STORAGE_LOCKED_EXIT_CODE
