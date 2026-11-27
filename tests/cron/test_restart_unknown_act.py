"""The restart sweep never terminalizes a live carrier, and never acts silently (t_9fbcfe35 U3).

Two defects measured on the deployed tree, both reproduced here against a temp cron store:

1. ``recover_interrupted_executions`` terminalized ANY row whose owner process was gone. At
   02:43:23 it wrote ``status='unknown'`` on an attempt whose carrier was still running (the run
   completed at 03:25:19) — it had no way to know the carrier was alive. A receipt that says
   ``running`` with a live ``carrier_pid`` now makes the sweep skip the row (R3).
2. Rows it does mark ``unknown`` went through no act at all: one warning in the log, no notice, no
   card. A row with NO receipt now runs the notice+card act, deduped by execution id on the board.

Nothing here reads or writes the host's live store: the jobs file, the ledger and the receipts
directory all live under ``tmp_path``.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import timedelta

import pytest

import cron.jobs as jobs_mod
import cron.scheduler as scheduler_mod
from cron import carrier_receipts, executions, jobs
from cron.scheduler_provider import InProcessCronScheduler

#: A pid that cannot be running: ``_owner_is_live`` reads it as provably dead.
DEAD_PID = 999999


# ── Harness ──────────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Temp cron store + temp receipts dir; the host's live ledger is never touched."""
    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")
    monkeypatch.setattr(carrier_receipts, "RECEIPTS_DIR", home / "cron" / "receipts")
    # Metrics mirror: never part of what these tests assert, and it must not reach a real sink.
    monkeypatch.setattr(executions, "record_cron_finish", lambda *a, **kw: None)
    with jobs.use_cron_store(home):
        yield home


class Deliveries:
    """The delivery seam: records what WOULD have been sent; sends nothing.

    ``error`` is what ``_deliver_result`` would return for a notice that reached no lane, which is
    the condition that must file the card.
    """

    def __init__(self, error=None):
        self.calls: list[dict] = []
        self.error = error

    def __call__(self, job, text, *, for_failure, adapters=None, loop=None):
        self.calls.append({"job": job, "text": text, "for_failure": for_failure})
        return self.error

    @property
    def texts(self) -> list[str]:
        return [call["text"] for call in self.calls]


class Board:
    """An idempotent board: the same key answers the same card, exactly like the kernel."""

    def __init__(self, raises=None):
        self.cards: dict[str, dict] = {}
        self.file_calls = 0
        self.raises = raises

    def file(self, *, board, idempotency_key, title, body, assignee):
        self.file_calls += 1
        if self.raises is not None:
            raise self.raises
        card = self.cards.get(idempotency_key)
        if card is None:
            card = {
                "id": f"card-{len(self.cards) + 1}",
                "title": title,
                "body": body,
                "assignee": assignee,
                "board": board,
            }
            self.cards[idempotency_key] = card
        return card["id"]


def _job(**overrides):
    fields = dict(name="restart-job", prompt="restart report", schedule="every 1h", deliver="local")
    fields.update(overrides)
    return jobs.create_job(**fields)


def _dead_owner_attempt(job_id, *, status="running"):
    """A ledger row whose owner is a DEAD scheduler — the 02:43 shape."""
    execution = executions.create_execution(job_id, source="test")
    if status == "running":
        executions.mark_execution_running(execution["id"])
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=? WHERE id=?",
            ("dead-gateway", DEAD_PID, execution["id"]),
        )
    return execution["id"]


def _receipt(execution_id, job_id, *, status, carrier_pid=None, **fields):
    """A receipt written the way the carrier's supervisor (U2) writes one."""
    record = carrier_receipts.new_receipt(
        job_id=job_id,
        execution_id=execution_id,
        status=status,
        carrier_pid=carrier_pid,
        **fields,
    )
    carrier_receipts.write_receipt(record)
    return record


def _status(execution_id):
    with executions._transaction() as conn:
        return conn.execute(
            "SELECT status FROM executions WHERE id=?", (execution_id,)
        ).fetchone()["status"]


# ── 1. The gate: a live carrier's row survives the restart sweep ─────────────────────────────


def test_live_carrier_receipt_saves_the_row_from_the_restart_sweep(store):
    """The 02:43 bug: owner gone, carrier still running, sweep wrote ``unknown`` anyway."""
    job = _job()
    execution_id = _dead_owner_attempt(job["id"])
    # The carrier outlived the scheduler: this process is alive, so its pid is a live carrier.
    _receipt(execution_id, job["id"], status=carrier_receipts.STATUS_RUNNING, carrier_pid=os.getpid())

    recovered = executions.recover_interrupted_executions()
    assert recovered == 0
    # Nothing was marked, so nothing needs the startup ACT either.
    assert recovered.receiptless == ()

    row = executions.get_execution(execution_id)
    assert row["status"] == "running"
    assert row["finished_at"] is None
    assert row["error"] is None


def test_a_running_receipt_with_a_dead_carrier_does_not_save_the_row(store):
    """Only a LIVE carrier is proof of life; a dead one is still abandoned (and settled by U1)."""
    job = _job()
    execution_id = _dead_owner_attempt(job["id"])
    _receipt(execution_id, job["id"], status=carrier_receipts.STATUS_RUNNING,
             carrier_pid=DEAD_PID)

    assert executions.recover_interrupted_executions() == 1
    assert _status(execution_id) == "unknown"


# ── 2. A receiptless unknown row acts, exactly once ──────────────────────────────────────────


def test_receiptless_dead_owner_is_unknown_and_files_exactly_one_card(store, monkeypatch):
    deliveries = Deliveries(error="no delivery lane resolved")
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)
    board = Board()
    monkeypatch.setattr("cron.unreachable_path.KanbanSink", lambda: board)

    job = _job()
    execution_id = _dead_owner_attempt(job["id"])
    scheduler = InProcessCronScheduler()

    assert scheduler.recover_interrupted() == 1
    assert _status(execution_id) == "unknown"

    # The outcome is not silent: the notice was attempted...
    assert len(deliveries.calls) == 1
    assert deliveries.calls[0]["for_failure"] is True
    # ...and, having no lane, exactly one durable card names the attempt.
    assert board.file_calls == 1
    assert list(board.cards) == [f"cron-carrier-orphan:{job['id']}:{execution_id}"]

    # A repeated restart cannot file twice: the row is terminal, so the sweep marks nothing, and
    # the board's idempotency key would answer the same card even if it did.
    assert scheduler.recover_interrupted() == 0
    assert board.file_calls == 1
    assert len(board.cards) == 1


def test_a_delivered_notice_does_not_file_a_card(store, monkeypatch):
    deliveries = Deliveries()  # None == delivery settled
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)
    board = Board()
    monkeypatch.setattr("cron.unreachable_path.KanbanSink", lambda: board)

    job = _job()
    execution_id = _dead_owner_attempt(job["id"])
    assert InProcessCronScheduler().recover_interrupted() == 1

    assert len(deliveries.calls) == 1
    assert board.file_calls == 0
    assert _status(execution_id) == "unknown"


def test_a_row_with_a_receipt_is_settled_by_the_reconciler_not_by_the_restart_act(store, monkeypatch):
    """An ``exited`` receipt is a verdict: U1 settles it, and the sweep adds no second notice."""
    deliveries = Deliveries(error="no delivery lane resolved")
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)
    board = Board()
    monkeypatch.setattr("cron.unreachable_path.KanbanSink", lambda: board)

    job = _job()
    execution_id = _dead_owner_attempt(job["id"])
    _receipt(execution_id, job["id"], status=carrier_receipts.STATUS_EXITED,
             carrier_pid=DEAD_PID, exit_code=3)

    assert InProcessCronScheduler().recover_interrupted() == 1
    # Settled from the carrier's own exit code by the reconciler (R2), not left `unknown`.
    assert _status(execution_id) == "failed"
    # Exactly ONE notice: the reconciler's. The restart act does not double up on a receipted row.
    assert len(deliveries.calls) == 1
    assert deliveries.calls[0]["for_failure"] is True
    assert len(board.cards) == 1


# ── 3. settle_carrier_receipt: one door, fenced ──────────────────────────────────────────────


def test_settle_refuses_live_owner_and_terminal_rows(store):
    job = _job()

    # A live owner's row (this process's own attempt) is the firer's to settle.
    live = executions.create_execution(job["id"], source="test")
    executions.mark_execution_running(live["id"])
    assert executions.settle_carrier_receipt(live["id"], job["id"], success=True) is None
    assert _status(live["id"]) == "running"

    # A terminal row is immutable.
    done = executions.create_execution(job["id"], source="test")
    assert executions.finish_execution(done["id"], success=True) is not None
    assert _status(done["id"]) == "completed"
    assert executions.settle_carrier_receipt(
        done["id"], job["id"], success=False, error="late verdict") is None
    assert _status(done["id"]) == "completed"


def test_settle_accepts_unknown_and_running_rows_of_dead_owners(store):
    job = _job()

    # ``unknown`` is exactly what a departed owner leaves behind.
    unknown = _dead_owner_attempt(job["id"])
    assert executions.recover_interrupted_executions() == 1
    assert _status(unknown) == "unknown"
    settled = executions.settle_carrier_receipt(unknown, job["id"], success=True)
    assert settled is not None and settled["status"] == "completed"

    # A ``running`` row whose owner is gone settles too.
    running = _dead_owner_attempt(job["id"])
    failed = executions.settle_carrier_receipt(
        running, job["id"], success=False, error="carrier exited with code 3")
    assert failed is not None
    assert failed["status"] == "failed"
    assert "exited with code 3" in failed["error"]

    # A row of another job is not this job's to settle.
    other = _job(name="other-job")
    assert executions.settle_carrier_receipt(running, other["id"], success=True) is None


# ── 4. delivery_outcome and exactly one state emission ────────────────────────────────────────


def test_settle_writes_delivery_outcome_and_emits_state_exactly_once(store, monkeypatch):
    job = _job()
    execution_id = _dead_owner_attempt(job["id"])

    emitted: list[dict] = []
    monkeypatch.setattr(
        executions, "_emit_execution_state",
        lambda record, **kwargs: emitted.append({"record": record, **kwargs}),
    )

    settled = executions.settle_carrier_receipt(
        execution_id, job["id"], success=True, delivery_outcome="delivered")

    assert settled["delivery_outcome"] == "delivered"
    assert executions.get_execution(execution_id)["delivery_outcome"] == "delivered"
    assert len(emitted) == 1
    assert emitted[0]["record"]["id"] == execution_id
    assert emitted[0]["delivery_outcome"] == "delivered"


# ── 5. A broken board never stops startup or the ticker ────────────────────────────────────


def test_a_throwing_board_does_not_propagate_out_of_startup_recovery(store, monkeypatch):
    monkeypatch.setattr(carrier_receipts, "_deliver_notice",
                        lambda *a, **kw: "no delivery lane resolved")
    monkeypatch.setattr("cron.unreachable_path.KanbanSink", lambda: Board(raises=RuntimeError("board down")))

    job = _job()
    execution_id = _dead_owner_attempt(job["id"])

    # The act's own catch-all: recovery returns the row it settled, and does not raise.
    assert InProcessCronScheduler().recover_interrupted() == 1
    assert _status(execution_id) == "unknown"

    # The seam itself raising is caught per row by the sweep.
    def exploding_act(_record):
        raise RuntimeError("act exploded")

    monkeypatch.setattr("cron.scheduler_provider._act_on_restart_unknown", exploding_act)
    second = _dead_owner_attempt(job["id"])
    assert InProcessCronScheduler().recover_interrupted() == 1
    assert _status(second) == "unknown"


def test_the_ticker_starts_even_when_the_startup_act_raises(store, monkeypatch):
    monkeypatch.setattr(carrier_receipts, "_deliver_notice",
                        lambda *a, **kw: "no delivery lane resolved")
    monkeypatch.setattr("cron.unreachable_path.KanbanSink",
                        lambda: Board(raises=RuntimeError("board down")))
    _dead_owner_attempt(_job()["id"])

    ticks: list[dict] = []
    heartbeats: list[bool] = []
    stop_event = threading.Event()

    def fake_tick(**kwargs):
        ticks.append(kwargs)
        stop_event.set()  # one cycle is enough: the ticker reached its loop

    monkeypatch.setattr(scheduler_mod, "tick", fake_tick)
    monkeypatch.setattr(jobs_mod, "record_ticker_heartbeat",
                        lambda *a, **kw: heartbeats.append(kw.get("success")))
    monkeypatch.setattr(jobs_mod, "record_ticker_error", lambda *a, **kw: None)
    monkeypatch.setattr(jobs_mod, "clear_ticker_error", lambda *a, **kw: None)

    InProcessCronScheduler().start(stop_event, interval=0)

    assert ticks, "the ticker never reached its loop after a broken board"
    assert heartbeats, "no liveness was recorded"
