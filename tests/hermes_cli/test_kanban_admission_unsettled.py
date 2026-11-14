"""ADMISSION_UNSETTLED: an owner that never settles an attempt is not a worker crash.

The run table booked every ``unknown`` receipt as ``crashed / managed worker lost``, so a
reader was sent into the worker's code for what the board had already recorded: on this host
407 of the 440 ``unknown`` crashes in the seven days to 2026-11-13 carried a managed worker's
OWN ``worker_result`` (a lost worker that HAD run), and the remaining ones carried the owner's
``worker_lost`` note. The submitter's last words were the only thing that said otherwise, and
they said "Kanban owner refused".

What the two classes must do instead:

  U1  an admission that settles after the answer budget expired is ADOPTED — the worker ends on
      the attempt's own exit code, never on ``1`` for an owner that was merely slow;
  U2  a worker that ran and was lost ends on ITS OWN result (exit code and last words), so the
      run's recorded cause is the worker's real cause;
  U3  an attempt with no worker evidence at all — the admission never settled and nothing ever
      bound — is recorded ``ADMISSION_UNSETTLED`` naming the owner and the budget, is never
      booked ``crashed``, and is not charged to the card's failure budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import sqlite3
import time

import pytest

from hermes_cli import gateway_client
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_client as kwc
from tests.hermes_cli.test_kanban_managed_worker_loss import (  # noqa: F401  (pytest fixture)
    CLAIM_LAPSED, _absent_pid, board,
)

PARAMS = {"board": "default", "task_id": "t_u", "run_id": 7, "claim_lock": "yoyodine:4242"}


def _sync(coro_fn):
    """Run one async body without pytest-asyncio: the door's shared venv does not carry it."""
    @functools.wraps(coro_fn)
    def _test(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return _test


# ---------------------------------------------------------------------------------------------
# U1/U2: the submitter reads the attempt's own record instead of calling the receipt a refusal
# ---------------------------------------------------------------------------------------------

def _owner(*answers, first_raises=None):
    """A fake owner: each rpc answer is consumed in order (the last one repeats).

    ``first_raises`` fails the FIRST ``kanban.run`` — the owner that outlasted the answer
    budget — so the retry on the next connection gets the admission that committed afterwards.
    """
    state = {"index": 0, "raised": False, "dials": 0}

    def connect():
        @contextlib.asynccontextmanager
        async def _connect(*_a, **_k):
            state["dials"] += 1

            class _Client:
                events = asyncio.Queue()

                async def rpc(self, method, **_params):
                    if method == "kanban.run":
                        if first_raises is not None and not state["raised"]:
                            state["raised"] = True
                            raise first_raises
                    answer = answers[min(state["index"], len(answers) - 1)]
                    state["index"] += 1
                    if method == "kanban.run":
                        return {"session_id": "s1", "receipt": answer}
                    return answer

            yield _Client()

        return _connect

    return connect


async def _fast(monkeypatch):
    clock = [0.0]

    async def _sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(kwc.asyncio, "sleep", _sleep)
    return clock


@_sync
async def test_an_admission_that_commits_after_the_answer_budget_is_adopted(monkeypatch, capsys):
    """The owner's answer budget expires, the retry's admission commits, and the worker completes."""
    await _fast(monkeypatch)
    monkeypatch.setattr(gateway_client, "connect_gateway",
                        _owner({"status": "terminal", "admission_id": "a1"},
                               first_raises=TimeoutError("answer budget expired"))())
    monkeypatch.setattr("gateway.session_kanban.worker_result",
                        lambda path, params: {"exit_code": 0, "last_output": "card finished"})
    assert await kwc.run(PARAMS, "/tmp/board.db") == 0
    out = capsys.readouterr().out
    assert "card finished" in out
    assert "owner refused" not in out


@_sync
async def test_an_unknown_receipt_that_settles_on_a_reread_is_adopted(monkeypatch, capsys):
    """``unknown`` is re-read: the owner resolves it, and the attempt's own verdict stands."""
    await _fast(monkeypatch)
    monkeypatch.setattr(gateway_client, "connect_gateway",
                        _owner({"status": "unknown", "admission_id": "a1"},
                               {"status": "terminal", "admission_id": "a1"})())
    monkeypatch.setattr("gateway.session_kanban.worker_result",
                        lambda path, params: {"exit_code": 42, "last_output": "the late result"})
    assert await kwc.run(PARAMS, "/tmp/board.db") == 42
    assert "the late result" in capsys.readouterr().out


@_sync
async def test_an_unknown_receipt_ends_on_the_workers_own_result_not_a_refusal(monkeypatch, capsys):
    """A lost worker that RAN: its own exit code and last words end the log, not "owner refused"."""
    await _fast(monkeypatch)
    monkeypatch.setattr(gateway_client, "connect_gateway",
                        _owner({"status": "unknown", "admission_id": "a1"})())
    monkeypatch.setattr("gateway.session_kanban.worker_result",
                        lambda path, params: {"exit_code": 1, "last_output": "No reply: out of room"})
    assert await kwc.run(PARAMS, "/tmp/board.db") == 1
    out = capsys.readouterr().out
    assert out.rstrip().endswith("No reply: out of room")
    assert "owner refused" not in out


@_sync
async def test_an_unknown_receipt_with_only_a_loss_note_names_the_loss(monkeypatch, capsys):
    """No result, but the owner recorded WHAT it lost: that is the sentence the run must carry."""
    await _fast(monkeypatch)
    monkeypatch.setattr(gateway_client, "connect_gateway",
                        _owner({"status": "unknown", "admission_id": "a1"})())
    monkeypatch.setattr("gateway.session_kanban.worker_result", lambda path, params: {})
    monkeypatch.setattr("gateway.session_kanban.worker_loss",
                        lambda path, params: {"reason": "managed_worker_failed"})
    assert await kwc.run(PARAMS, "/tmp/board.db") == 1
    out = capsys.readouterr().out
    assert "managed worker lost: managed_worker_failed" in out
    assert "owner refused" not in out


@_sync
async def test_an_admission_nothing_ever_bound_names_itself_and_the_owner(monkeypatch):
    """Nothing ran and nothing was lost: the class is the OWNER's, and it says so."""
    await _fast(monkeypatch)
    monkeypatch.setattr("gateway.session_kanban.worker_result", lambda path, params: {})
    monkeypatch.setattr("gateway.session_kanban.worker_loss", lambda path, params: {})
    monkeypatch.setattr("gateway.session_kanban.owner_marker",
                        lambda path, params: {"pid": 4242, "db": "/x/state.db"})
    monkeypatch.setattr(kwc, "admission_answer_budget", lambda: 120.0)
    with pytest.raises(kwc._AdmissionUnsettled) as raised:
        kwc._exit_code({"status": "unknown", "admission_id": "a1"}, PARAMS, "/tmp/board.db")
    message = str(raised.value)
    assert message.startswith("ADMISSION_UNSETTLED")
    assert "owner pid 4242" in message
    assert "120s" in message
    assert "not a worker crash" in message


def test_submit_prints_an_unsettled_admission_without_calling_it_a_refusal(monkeypatch, capsys):
    """End to end through ``_submit``: the run's log line is the class, not a refusal claim.

    ``_submit`` deliberately SCRUBS the worker's identity out of ``os.environ`` before it hands
    the owner a startup (task, run, claim lock, session source), so the whole environment is
    restored here: a leak would disarm the dispatch identity of every later test in the process.
    """
    awaitables = []

    def _run(coro):
        awaitables.append(coro)
        coro.close()
        raise kwc._AdmissionUnsettled("ADMISSION_UNSETTLED owner pid 4242: nothing ran")

    before = dict(os.environ)
    try:
        monkeypatch.setattr(kwc.asyncio, "run", _run)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_u")
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "yoyodine:4242")
        monkeypatch.setenv("HERMES_KANBAN_DB", "/tmp/board.db")
        assert kwc._submit() == 1
    finally:
        os.environ.clear()
        os.environ.update(before)
    assert capsys.readouterr().err.startswith("ADMISSION_UNSETTLED")


# ---------------------------------------------------------------------------------------------
# U3: the dispatcher books an attempt nothing ever executed as its own class, uncharged
# ---------------------------------------------------------------------------------------------

def _current_fingerprint() -> str:
    return kbd._process_fingerprint(os.getpid()) or ""


def test_an_admitted_attempt_nothing_ever_bound_is_requeued_as_admission_unsettled(board, monkeypatch):
    """The owner took the claim and never bound a worker: nothing ran, so nothing is charged.

    Whichever reclaim path ticks first must record the same class, so this drives the claim
    through its TTL (the ordinary path for a worker that exited at admission) rather than the
    crash sweep: the class is a property of what happened, not of which sweep noticed it.
    """
    monkeypatch.setattr("hermes_cli.kanban_owner_recovery.answer_budget_seconds", lambda: 120.0)
    run_id = board.running("t_u", claim_expires_in=CLAIM_LAPSED, admission=("queued", None),
                           owner_pid=4242)
    row_before = board.state("t_u")

    board.tick(failure_limit=2)

    row, run = board.state("t_u"), board.run(run_id)
    assert run["outcome"] == "admission_unsettled"
    assert run["status"] == "admission_unsettled"
    assert run["error"].startswith("ADMISSION_UNSETTLED owner pid 4242")
    assert "120s" in run["error"]
    assert "managed worker lost" not in run["error"]
    event = board.conn.execute(
        "SELECT payload FROM task_events WHERE run_id=? AND kind='admission_unsettled'", (run_id,)
    ).fetchone()
    facts = json.loads(event["payload"])["admission_unsettled"]
    assert facts["owner_pid"] == 4242
    assert facts["admission_status"] == "queued"
    assert facts["answer_budget_seconds"] == 120.0
    assert row["status"] == "ready"                       # requeued, not blocked
    assert row["consecutive_failures"] == row_before["consecutive_failures"]   # uncharged
    assert row["last_failure_error"].startswith("ADMISSION_UNSETTLED")


def test_a_lost_worker_that_ran_is_still_a_managed_worker_loss(board):
    """The control: real worker evidence keeps the class it always had, and IS charged."""
    run_id = board.running("t_u", claim_expires_in=CLAIM_LAPSED,
                           worker_lost="managed_worker_failed", admission=("unknown", None))
    board.tick(failure_limit=2)
    run, row = board.run(run_id), board.state("t_u")
    assert run["outcome"] == "reclaimed"
    assert "managed worker lost: managed_worker_failed" in run["error"]
    assert "ADMISSION_UNSETTLED" not in run["error"]
    assert row["consecutive_failures"] == 1
