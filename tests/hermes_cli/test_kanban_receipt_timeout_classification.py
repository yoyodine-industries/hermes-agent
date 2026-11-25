"""Receipt loss is a NAMED cause and a requeue, not ``unknown`` -> exit 1 -> a charged crash.

Measured defect (card t_5800fe41, ruling on t_7c0b285e): a worker whose persistence receipt
never came back from the owning runtime raised ``WorkerPersistenceError('worker_receipt_timeout')``
(or ``'owner_unavailable'`` when the owner could not be reached at all). The coarse classifier
matched none of its phrases, returned ``unknown``, the exit mapping refused ``unknown`` (it is
not a transient storage reason) and the worker exited 1 -- so the run was booked ``crashed``,
the CARD's ``consecutive_failures`` was charged, and the card was re-dispatched into the same
contention on the next tick. The least understood failure paid the card's budget while the
well-understood ones went free.

The three assertions that make the chain un-reachable again:

  R1  every raw string the turn layer actually emits maps to a NAMED cause, never ``unknown``;
  R2  each named cause is in the closed ``_TRANSIENT_STORAGE_REASONS`` whitelist and exits
      ``KANBAN_STORAGE_LOCKED_EXIT_CODE`` (76);
  R3  the exit ROOM of the mapping still rejects a genuinely unclassified cause -- the
      whitelist stays closed, so R2 cannot be satisfied by widening the exit instead.
"""
import pytest

from hermes_state_errors import PERSISTENCE_ERROR_CAUSES, classify_persistence_error

from hermes_cli.cli_single_query import (
    _TRANSIENT_STORAGE_REASONS,
    _single_query_exit_code,
)

# Importing the Kanban exit constants probes this checkout's own git plumbing; no test here
# writes outside a temp dir.
pytestmark = pytest.mark.allow_real_home_io


# R1: the raw strings, exactly as the live tree emits them. ``classify_persistence_error``
# lowercases before matching, so the ACK case difference must not matter either.
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("worker_receipt_timeout", "receipt_timeout"),
        ("WorkerPersistenceError: worker_receipt_timeout after 20s", "receipt_timeout"),
        ("pending_receipt", "receipt_timeout"),
        ("owner did not ACK the receipt", "receipt_timeout"),
        ("owner_unavailable", "owner_unavailable"),
        ("persistence_degraded: owner_unavailable (1 retained)", "owner_unavailable"),
    ],
)
def test_owner_side_loss_has_a_named_cause(raw, expected):
    assert classify_persistence_error(raw) == expected
    assert expected in PERSISTENCE_ERROR_CAUSES


def test_unknown_is_not_reachable_from_a_receipt_timeout():
    # The regression the card measured: 197 crashes/24 h carried
    # ``session_persistence_failed:unknown`` from exactly these two strings.
    assert classify_persistence_error("worker_receipt_timeout") != "unknown"
    assert classify_persistence_error("owner_unavailable") != "unknown"
    # A string that really is unclassifiable still lands in ``unknown`` -- the bucket itself
    # is NOT removed, only these two failure modes leave it.
    assert classify_persistence_error("something nobody has ever seen") == "unknown"


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fixture")


@pytest.fixture
def exit_mapping(monkeypatch):
    """The exit mapping reads its provider policy from the ``cli`` facade at call time (the
    module deliberately late-binds so tests and the facade can patch it). Stub only those two
    names: the full ``cli`` module drags the TUI stack in, which is irrelevant here and not
    installed in every harness."""
    import sys
    import types

    stub = types.ModuleType("cli")
    stub._TERMINAL_PROVIDER_REASONS = frozenset()
    stub._TRANSIENT_PROVIDER_REASONS = frozenset()
    monkeypatch.setitem(sys.modules, "cli", stub)


# R2: cause -> whitelist entry -> exit 76, for both causes the turn layer can report.
@pytest.mark.parametrize("cause", ["receipt_timeout", "owner_unavailable"])
def test_owner_side_loss_exits_as_storage_locked(worker, exit_mapping, cause):
    from hermes_cli.kanban_db import KANBAN_STORAGE_LOCKED_EXIT_CODE

    reason = f"session_persistence_failed:{cause}"
    assert reason in _TRANSIENT_STORAGE_REASONS
    assert _single_query_exit_code({"failed": True, "failure_reason": reason}) == \
        KANBAN_STORAGE_LOCKED_EXIT_CODE
    assert KANBAN_STORAGE_LOCKED_EXIT_CODE == 76


# R3: the whitelist stays closed. ``unknown`` and a plausible-but-unlisted cause must still
# exit 1, so a future phrase that the classifier has no cause for is never quietly requeued
# forever by widening the exit path instead of naming the cause.
@pytest.mark.parametrize(
    "reason",
    ["session_persistence_failed:unknown", "session_persistence_failed:disk"],
)
def test_unlisted_storage_cause_still_exits_one(worker, exit_mapping, reason):
    assert reason not in _TRANSIENT_STORAGE_REASONS
    assert _single_query_exit_code({"failed": True, "failure_reason": reason}) == 1


def test_non_worker_keeps_the_plain_failure_exit(monkeypatch, exit_mapping):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    result = {"failed": True, "failure_reason": "session_persistence_failed:receipt_timeout"}
    assert _single_query_exit_code(result) == 1
