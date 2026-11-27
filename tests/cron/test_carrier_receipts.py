"""The carrier's receipt settles the attempt, and settlement ACTS (card ``t_9fbcfe35`` U1).

Every test runs against a temp cron store: the jobs file, the executions ledger and the receipts
directory all live under ``tmp_path``.  Nothing here reads or writes the host's live store.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from cron import carrier_receipts, executions, jobs

RUN_ID = "20261127-0215"
PUBLISHED = "/opt/hermes_prod/yaan-platform/REPORTS/carrier/report.md"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Temp cron store + temp receipts dir; the host's live ledger is never touched."""
    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")
    monkeypatch.setattr(carrier_receipts, "RECEIPTS_DIR", home / "cron" / "receipts")
    with jobs.use_cron_store(home):
        yield home


class Deliveries:
    """The delivery seam: records what WOULD have been sent, sends nothing."""

    def __init__(self, error=None, raises=None):
        self.calls: list[dict] = []
        self.error = error
        self.raises = raises

    def __call__(self, job, text, *, for_failure, adapters=None, loop=None):
        self.calls.append({"job": job, "text": text, "for_failure": for_failure})
        if self.raises is not None:
            raise self.raises
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
    fields = dict(name="carrier-job", prompt="carrier report",
                  schedule="every 1h", deliver="local")
    fields.update(overrides)
    return jobs.create_job(**fields)


def _attempt(job_id, *, pid=999999):
    """A ledger row whose owner is a DEAD scheduler - the 02:43 shape."""
    execution = executions.create_execution(job_id, source="test")
    executions.mark_execution_running(execution["id"])
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=? WHERE id=?",
            ("dead-gateway", pid, execution["id"]),
        )
    return execution["id"]


def _receipt(home, execution_id, **fields):
    """A schema-complete receipt written the way the carrier's supervisor writes one."""
    stderr = home / "run.err"
    stdout = home / "run.out"
    if not stdout.exists():
        stdout.write_text("", encoding="utf-8")
    if not stderr.exists():
        stderr.write_text("", encoding="utf-8")
    record = carrier_receipts.new_receipt(
        job_id=fields.pop("job_id"),
        execution_id=execution_id,
        status=fields.pop("status", carrier_receipts.STATUS_EXITED),
        exit_code=fields.pop("exit_code", 0),
        carrier_pid=fields.pop("carrier_pid", 999999),
        stdout_path=str(stdout),
        stderr_path=str(stderr),
        **fields,
    )
    carrier_receipts.write_receipt(record)
    return record, stdout, stderr


def _last_run_at(job_id):
    return jobs.get_job(job_id).get("last_run_at")


def test_terminal_receipt_settles_running_row_and_advances_last_run_at(store, monkeypatch):
    job = _job()
    before = _last_run_at(job["id"])
    execution_id = _attempt(job["id"])
    claimed_at = executions.get_execution(execution_id)["claimed_at"]
    record, stdout, _ = _receipt(store, execution_id, job_id=job["id"])
    stdout.write_text("the carrier's own report", encoding="utf-8")
    # Private from creation: a receipt can name a run id, a stderr tail and artifact paths.
    receipt_file = carrier_receipts.receipt_path(execution_id)
    assert (receipt_file.stat().st_mode & 0o777) == 0o600
    assert record["schema"] == carrier_receipts.SCHEMA
    deliveries = Deliveries()
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)

    assert carrier_receipts.reconcile_receipts() == 1

    assert executions.get_execution(execution_id)["status"] == "completed"
    after = _last_run_at(job["id"])
    assert after, "mark_job_run must advance last_run_at"
    assert after != before
    assert datetime.fromisoformat(after) > datetime.fromisoformat(claimed_at)
    assert deliveries.texts == ["the carrier's own report"]
    assert deliveries.calls[0]["for_failure"] is False
    assert not carrier_receipts.receipt_path(execution_id).exists()
    # The receipt is private from creation: it can name a run id, a stderr tail and artifacts.
    assert oct(carrier_receipts.receipt_path(execution_id).parent.stat().st_mode & 0o777)


def test_exited_failure_settles_failed_advances_last_run_at_and_names_the_run(store, monkeypatch):
    job = _job()
    execution_id = _attempt(job["id"])
    claimed_at = executions.get_execution(execution_id)["claimed_at"]
    _, _, stderr = _receipt(store, execution_id, job_id=job["id"], exit_code=2,
                            run_id=RUN_ID, published_path=PUBLISHED)
    stderr.write_text("boom: the carrier blew up", encoding="utf-8")
    deliveries = Deliveries()
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)

    assert carrier_receipts.reconcile_receipts() == 1

    row = executions.get_execution(execution_id)
    assert row["status"] == "failed"
    after = _last_run_at(job["id"])
    assert after and datetime.fromisoformat(after) > datetime.fromisoformat(claimed_at)
    assert deliveries.calls and deliveries.calls[0]["for_failure"] is True
    assert RUN_ID in deliveries.texts[0]
    assert PUBLISHED in deliveries.texts[0]
    assert "boom: the carrier blew up" in deliveries.texts[0]


def test_running_receipt_with_a_live_carrier_is_never_settled(store, monkeypatch):
    """The R3 control: this is the 02:43 premature-``unknown`` bug, reproduced."""
    from gateway.status import get_process_start_time

    job = _job()
    execution_id = _attempt(job["id"], pid=999999)
    before = _last_run_at(job["id"])
    carrier_receipts.write_receipt(carrier_receipts.new_receipt(
        job_id=job["id"],
        execution_id=execution_id,
        status=carrier_receipts.STATUS_RUNNING,
        carrier_pid=os.getpid(),
        carrier_started_at=get_process_start_time(os.getpid()),
    ))
    deliveries = Deliveries()
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)

    assert carrier_receipts.reconcile_receipts() == 0

    assert executions.get_execution(execution_id)["status"] == "running"
    assert _last_run_at(job["id"]) == before
    assert deliveries.calls == []
    assert carrier_receipts.receipt_path(execution_id).exists()


def test_running_receipt_with_a_dead_carrier_settles_failed_without_silence(store, monkeypatch):
    job = _job()
    execution_id = _attempt(job["id"])
    _receipt(store, execution_id, job_id=job["id"], status=carrier_receipts.STATUS_RUNNING,
             exit_code=None, carrier_pid=999999, carrier_started_at=179571814546, run_id=RUN_ID)
    deliveries = Deliveries()
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)

    assert carrier_receipts.reconcile_receipts() == 1

    row = executions.get_execution(execution_id)
    assert row["status"] == "failed"
    assert row["error"].startswith("carrier_exited_without_receipt")
    assert RUN_ID in row["error"]
    assert deliveries.calls[0]["for_failure"] is True
    assert RUN_ID in deliveries.texts[0]


def test_undeliverable_failure_files_exactly_one_card_even_when_reconciled_twice(
        store, monkeypatch):
    job = _job()
    execution_id = _attempt(job["id"])
    record, _, stderr = _receipt(store, execution_id, job_id=job["id"], exit_code=1,
                                 run_id=RUN_ID)
    stderr.write_text("error: no artifact was published", encoding="utf-8")
    monkeypatch.setattr(
        carrier_receipts, "_deliver_notice",
        Deliveries(raises=RuntimeError("no lane resolved")))

    board = Board()
    assert carrier_receipts.reconcile_receipts(sink=board) == 1
    assert board.file_calls == 1
    assert len(board.cards) == 1
    (key, card), = board.cards.items()
    assert key == f"cron-carrier-orphan:{job['id']}:{execution_id}"
    assert card["assignee"] == "platform-stl"
    assert RUN_ID in card["body"]
    assert "this run's outcome never reached a consumer; reconcile it" in card["body"]

    # A crash between the card write and the delete leaves the receipt behind: the next tick
    # resumes, and the board's own key keeps it to ONE card.
    record["settlement"] = {
        "ledger": True, "success": False, "error": record.get("settlement", {}).get("error"),
        "job": "marked", "notice": "failed", "notice_error": "no lane resolved",
    }
    carrier_receipts.write_receipt(record)
    assert carrier_receipts.reconcile_receipts(sink=board) == 1
    assert board.file_calls == 2
    assert len(board.cards) == 1
    assert not carrier_receipts.receipt_path(execution_id).exists()


def test_failure_without_a_published_path_says_publication_withheld(store, monkeypatch):
    job = _job()
    execution_id = _attempt(job["id"])
    _receipt(store, execution_id, job_id=job["id"], exit_code=1, run_id=RUN_ID,
             published_path=None)
    deliveries = Deliveries()
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)

    carrier_receipts.reconcile_receipts()

    text = deliveries.texts[0]
    assert "publication withheld" in text
    assert "the carrier declared no published artifact" in text


def test_failure_with_a_published_path_names_it(store, monkeypatch):
    job = _job()
    execution_id = _attempt(job["id"])
    _receipt(store, execution_id, job_id=job["id"], exit_code=1, run_id=RUN_ID,
             published_path=PUBLISHED)
    deliveries = Deliveries()
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)

    carrier_receipts.reconcile_receipts()

    text = deliveries.texts[0]
    assert PUBLISHED in text
    assert "publication withheld" not in text


def test_already_terminal_execution_is_a_no_op_and_the_receipt_goes(store, monkeypatch):
    job = _job()
    execution_id = _attempt(job["id"])
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET status='completed', finished_at=? WHERE id=?",
            ("2026-11-27T03:25:19-05:00", execution_id),
        )
    before = _last_run_at(job["id"])
    _receipt(store, execution_id, job_id=job["id"], exit_code=0, run_id=RUN_ID)
    deliveries = Deliveries()
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)

    assert carrier_receipts.reconcile_receipts() == 0

    assert executions.get_execution(execution_id)["status"] == "completed"
    assert _last_run_at(job["id"]) == before
    assert deliveries.calls == []
    assert not carrier_receipts.receipt_path(execution_id).exists()


def test_empty_receipts_directory_touches_no_row(store, monkeypatch):
    """No behaviour change when ``cron/receipts/`` is empty - the every-tick cost is a listdir."""
    job = _job()
    execution_id = _attempt(job["id"])
    before = _last_run_at(job["id"])
    deliveries = Deliveries()
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)

    assert carrier_receipts.reconcile_receipts() == 0

    assert executions.get_execution(execution_id)["status"] == "running"
    assert _last_run_at(job["id"]) == before
    assert deliveries.calls == []


def test_an_attempt_still_owned_by_its_firer_is_left_alone(store, monkeypatch):
    """The normal in-flight run: the scheduler is alive and still delivering/settling its own
    attempt, so the reconciler must not race it into a duplicate notice."""
    job = _job()
    execution = executions.create_execution(job["id"], source="test")
    execution_id = execution["id"]
    executions.mark_execution_running(execution_id)
    before = _last_run_at(job["id"])
    _receipt(store, execution_id, job_id=job["id"], exit_code=0, run_id=RUN_ID)
    deliveries = Deliveries()
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)

    assert carrier_receipts.reconcile_receipts() == 0

    assert executions.get_execution(execution_id)["status"] == "running"
    assert _last_run_at(job["id"]) == before
    assert deliveries.calls == []
    assert carrier_receipts.receipt_path(execution_id).exists()


def test_a_newer_live_fire_keeps_the_job_row(store, monkeypatch):
    """Ledger + notice settle, but the job row belongs to the fire that holds the claim now."""
    job = _job()
    execution_id = _attempt(job["id"])
    assert jobs.claim_job_for_fire(job["id"]) is True
    before = _last_run_at(job["id"])
    _receipt(store, execution_id, job_id=job["id"], exit_code=1, run_id=RUN_ID)
    deliveries = Deliveries()
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)

    assert carrier_receipts.reconcile_receipts() == 1

    assert executions.get_execution(execution_id)["status"] == "failed"
    assert _last_run_at(job["id"]) == before
    assert deliveries.calls, "the notice still leaves - only the job-row projection is fenced"


def test_a_failure_to_file_the_card_is_itself_observable(store, monkeypatch):
    """A failure to act must be recorded where the operator reads the failure (#silent-failure)."""
    from cron import incidents

    job = _job()
    execution_id = _attempt(job["id"])
    _receipt(store, execution_id, job_id=job["id"], exit_code=1, run_id=RUN_ID)
    monkeypatch.setattr(
        carrier_receipts, "_deliver_notice",
        Deliveries(raises=RuntimeError("no lane resolved")))
    board = Board(raises=RuntimeError("board unreachable"))

    assert carrier_receipts.reconcile_receipts(sink=board) == 0

    rows = incidents.list_incidents()
    assert any("board unreachable" in (row.get("unreachable_card_error") or "")
               for row in rows), rows
    # The act did not happen, so the receipt stays for the next tick to retry.
    assert carrier_receipts.receipt_path(execution_id).exists()


def test_a_crash_between_ledger_and_job_row_resumes_cleanly(store, monkeypatch):
    job = _job()
    execution_id = _attempt(job["id"])
    claimed_at = executions.get_execution(execution_id)["claimed_at"]
    record, _, _ = _receipt(store, execution_id, job_id=job["id"], exit_code=1, run_id=RUN_ID)
    # A previous tick settled the ledger and died before the job row was written.
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET status='failed', finished_at=?, error=? WHERE id=?",
            ("2026-11-27T03:25:19-05:00", "carrier exited with code 1", execution_id),
        )
    record["settlement"] = {"ledger": True, "success": False,
                            "error": "carrier exited with code 1 (run 20261127-0215)"}
    carrier_receipts.write_receipt(record)
    deliveries = Deliveries()
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)

    assert carrier_receipts.reconcile_receipts() == 1

    after = _last_run_at(job["id"])
    assert after and datetime.fromisoformat(after) > datetime.fromisoformat(claimed_at)
    assert deliveries.calls and RUN_ID in deliveries.texts[0]
    assert executions.get_execution(execution_id)["status"] == "failed"
    assert not carrier_receipts.receipt_path(execution_id).exists()


# ── R5: a scheduler budget kill SAYS SO (t_568873e3) ───────────────────────────────────────


def _budget_receipt(job_id, **overrides):
    fields = dict(exit_code=-9, timeout_s=3600, signal="SIGKILL", run_id=RUN_ID)
    fields.update(overrides)
    return carrier_receipts.new_receipt(
        job_id=job_id, execution_id="exec-budget",
        status=carrier_receipts.STATUS_EXITED, **fields)


def test_a_budget_kill_is_named_in_the_verdict(store):
    job = _job()
    record = _budget_receipt(job["id"])
    assert carrier_receipts._verdict(record) == (
        False,
        "carrier killed by the scheduler's script budget after 3600s (signal SIGKILL); "
        f"exit code -9 (run {RUN_ID})")


def test_the_failure_notice_says_the_budget_killed_the_run(store):
    job = _job()
    record = _budget_receipt(job["id"])
    success, error = carrier_receipts._verdict(record)
    assert success is False
    text = carrier_receipts.failure_notice_text(job, record, error)
    assert "- termination: killed by the scheduler's script budget after 3600s (signal SIGKILL)" in text
    assert "- attempt status: `exited`, exit code -9" in text


def test_a_signal_death_with_no_recorded_timeout_is_not_called_a_budget_kill(store):
    """The control: -9 with no timeout_s is an unattributed death, never a budget claim."""
    job = _job()
    record = _budget_receipt(job["id"], timeout_s=None, signal=None, run_id=None)
    assert record.get("timeout_s") is None
    success, error = carrier_receipts._verdict(record)
    assert success is False
    assert error == "carrier exited with code -9 (run exec-budget)"
    text = carrier_receipts.failure_notice_text(job, record, error)
    assert "script budget" not in text


def test_a_budget_kill_with_no_recorded_signal_omits_the_clause(store):
    job = _job()
    record = _budget_receipt(job["id"], timeout_s=600, signal=None)
    assert carrier_receipts._verdict(record) == (
        False,
        "carrier killed by the scheduler's script budget after 600s; "
        f"exit code -9 (run {RUN_ID})")


def test_reconcile_delivers_a_notice_and_settles_a_cause_that_say_budget(store, monkeypatch):
    """End to end: the consumer-visible text and the ledger row both carry the fact."""
    job = _job()
    execution_id = _attempt(job["id"])
    _receipt(store, execution_id, job_id=job["id"], exit_code=-9,
             timeout_s=3600, signal="SIGKILL", run_id=RUN_ID)
    deliveries = Deliveries()
    monkeypatch.setattr(carrier_receipts, "_deliver_notice", deliveries)

    assert carrier_receipts.reconcile_receipts() == 1

    row = executions.get_execution(execution_id)
    assert row["status"] == "failed"
    assert "scheduler's script budget after 3600s (signal SIGKILL)" in (row.get("error") or "")
    assert deliveries.calls and deliveries.calls[0]["for_failure"] is True
    assert "scheduler's script budget after 3600s (signal SIGKILL)" in deliveries.texts[0]
