"""Harness losses spend a SURVIVAL budget, never the card's WORK budget (t_5800fe41 steps 4-5).

The measured defect: a run the harness lost (busy store, lost receipt, unreachable owner) was
booked ``crashed`` against the card, so ``failure_limit`` (2) retired cards for being unlucky,
and the row said only "worker exited". The ruling splits the two budgets and makes the row name
what was lost:

  S1  a lost receipt/owner run leaves ``consecutive_failures`` UNCHANGED and increments
      ``survival_failures``;
  S2  the run row carries ``metadata.loss_class`` taken from the WORKER's own trailer, not
      inferred from the exit code;
  S3  after ``SURVIVAL_ATTEMPT_LIMIT`` losses the card is ``blocked`` with the loss class named,
      never ``gave_up`` -- and the renderer says the work was not judged.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_output as kbo


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def fast_sweep(monkeypatch):
    """Reclaim the claim at once (prod's crash grace would hold a fresh claim)."""
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8


def _write_worker_log(task_id: str, exit_code: int, loss_class: str | None) -> None:
    path = kb.worker_log_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"[kanban-worker-exit] rc={exit_code}\n"
    if loss_class:
        body += f"[kanban-worker-loss-class] {loss_class}\n"
    path.write_text(body, encoding="utf-8")


def _survive_one_loss(conn, tid: str, host: str, i: int, loss_class: str | None) -> int:
    pid = 81000 + i
    kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    conn.commit()
    _write_worker_log(tid, kb.KANBAN_STORAGE_LOCKED_EXIT_CODE, loss_class)
    kbd._record_worker_exit(pid, _exited_status(kb.KANBAN_STORAGE_LOCKED_EXIT_CODE))
    crashed = kbd.detect_crashed_workers(conn)
    assert tid not in crashed, "a harness loss is never booked as a card crash"
    return pid


def test_receipt_timeout_is_named_and_never_spends_the_work_budget(kanban_home, fast_sweep):
    import sqlite3

    with sqlite3.connect(kb.kanban_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="receipt", assignee="a")

        _survive_one_loss(conn, tid, host, 0, "receipt_timeout")

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0, "the WORK budget must not be spent"
        assert conn.execute(
            "SELECT survival_failures FROM tasks WHERE id=?", (tid,)
        ).fetchone()[0] == 1

        # S2: the run row carries what the WORKER reported, not what rc=76 implies.
        meta = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,),
        ).fetchone()[0])
        assert meta["loss_class"] == "receipt_timeout"
        assert kbo.loss_class_of(_Namespace(meta)) == "receipt_timeout"


class _Namespace:
    """Minimal stand-in for a run row in the renderer (it only reads ``metadata``)."""

    def __init__(self, metadata):
        self.metadata = metadata


def test_owner_unavailable_maps_to_its_own_loss_class(kanban_home, fast_sweep):
    import sqlite3

    with sqlite3.connect(kb.kanban_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="owner", assignee="a")
        _survive_one_loss(conn, tid, host, 0, "owner_admission_unavailable")
        meta = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,),
        ).fetchone()[0])
        assert meta["loss_class"] == "owner_admission_unavailable"
        assert kb.get_task(conn, tid).consecutive_failures == 0


def test_the_dispatcher_does_not_invent_a_loss_class_from_the_exit_code(kanban_home, fast_sweep):
    """rc=76 with NO worker trailer: the run is requeued, but the row says only
    ``storage_locked`` (what the code alone supports) -- never ``receipt_timeout``."""
    import sqlite3

    with sqlite3.connect(kb.kanban_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="silent", assignee="a")
        _survive_one_loss(conn, tid, host, 0, None)
        meta = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,),
        ).fetchone()[0])
        assert meta["loss_class"] == "storage_locked"


def test_six_harness_losses_park_the_card_blocked_never_gave_up(kanban_home, fast_sweep, monkeypatch):
    import sqlite3

    monkeypatch.setenv("HERMES_KANBAN_STORAGE_LOCKED_COOLDOWN_SECONDS", "0")
    with sqlite3.connect(kb.kanban_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="unlucky", assignee="a")

        for i in range(kbd.SURVIVAL_ATTEMPT_LIMIT):
            _survive_one_loss(conn, tid, host, i, "receipt_timeout")

        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "the card is parked, not retried forever"
        assert task.consecutive_failures == 0, "the work was never judged"
        assert task.last_failure_error and "receipt_timeout" in task.last_failure_error
        assert "NOT JUDGED" in task.last_failure_error
        assert conn.execute(
            "SELECT survival_failures FROM tasks WHERE id=?", (tid,)
        ).fetchone()[0] == kbd.SURVIVAL_ATTEMPT_LIMIT

        kinds = [row["kind"] for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (tid,),
        ).fetchall()]
        assert "survival_exhausted" in kinds
        assert "gave_up" not in kinds, "a card is never given up on the work for harness loss"
        assert tid in getattr(
            kbd.detect_crashed_workers, "_last_survival_blocked", []
        )

        # A completed run proves the work and resets BOTH budgets.
        kbd._clear_failure_counter(conn, tid)
        assert conn.execute(
            "SELECT survival_failures FROM tasks WHERE id=?", (tid,)
        ).fetchone()[0] == 0


def test_renderer_distinguishes_harness_loss_from_work_failure():
    loss = _Namespace({"loss_class": "receipt_timeout"})
    line = kbo.fmt_loss_line(loss)
    assert line and "harness loss (receipt_timeout)" in line
    assert "WORK WAS NOT JUDGED" in line
    # A normal failed/review run carries no loss_class and renders no such line.
    assert kbo.fmt_loss_line(_Namespace({})) is None
    assert kbo.fmt_loss_line(_Namespace({"loss_class": "made_up"})) is None
