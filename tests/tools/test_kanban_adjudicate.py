"""The seat-scoped exit door (``kanban_adjudicate``, card ``t_012fb74e``).

The kernel half: :func:`hermes_cli.kanban_adjudicate.adjudicate` driven directly on a private
board — the same machinery ``tests/hermes_cli/test_kanban_cross_close.py`` uses — so the
sequences, the release-then-act ordering and every refusal are pinned without a language model
in the loop. The tool-schema half (the door is on the worker's toolset and the handler is
reachable) is covered by the rail in ``tests/hermes_cli/test_kanban_exits.py``.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

REASON = "the ruling on this class is decided and the card is superseded by the door itself"


@pytest.fixture
def board(monkeypatch, tmp_path):
    """A private board (temp HERMES_HOME), no tenant/board pins inherited."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-adjudicate")
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT",
                "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        yield kb, conn
    finally:
        conn.close()


def _card(kb, conn, title="a card", **kw):
    return kb.create_task(conn, title=title, assignee="platform-coder", **kw)


def _caller(kb, conn, title="the adjudicating lead", **kw):
    """A caller card with a claimed run — the provenance side of every door write."""
    caller = _card(kb, conn, title, **kw)
    kb.claim_task(conn, caller)
    return caller, kb._current_run_id(conn, caller)


def _triage(kb, conn, title="a triage card", **kw):
    return _card(kb, conn, title, triage=True, **kw)


def _events(conn, task_id, kind):
    return [json.loads(r["payload"] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind))]


def _state(conn, task_id):
    """Everything a refusal must leave untouched about the target."""
    row = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    row["_runs"] = conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()[0]
    row["_events"] = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)).fetchone()[0]
    return row


# --------------------------------------------------------------------------- the two doors


def test_close_takes_a_triage_card_to_done_with_provenance(board):
    kb, conn = board
    from hermes_cli.kanban_adjudicate import adjudicate

    caller, run_id = _caller(kb, conn)
    target = _triage(kb, conn)

    receipt = adjudicate(
        conn, target, disposition="close", expect_status="triage", reason=REASON,
        caller_task_id=caller, caller_run_id=run_id,
    )

    assert receipt["to_status"] == "done"
    assert receipt["from_status"] == "triage"
    assert kb.get_task(conn, target).status == "done"

    # The act is on the TARGET as one event carrying both ends of the audit.
    (event,) = _events(conn, target, "adjudicated")
    assert event["disposition"] == "close"
    assert event["from_status"] == "triage"
    assert event["to_status"] == "done"
    assert event["caller_task_id"] == caller
    assert event["caller_run_id"] == run_id
    assert event["reason"] == REASON

    # …and the closing run is the door's own claim, so the summary is the reason given.
    run = kb.latest_run(conn, target)
    assert run.outcome == "completed"
    assert run.summary == REASON

    comments = [r["body"] for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (target,))]
    assert any(caller in body and "close" in body for body in comments)


def test_promote_makes_a_triage_card_dispatchable_without_claiming_it(board):
    kb, conn = board
    from hermes_cli.kanban_adjudicate import adjudicate

    caller, run_id = _caller(kb, conn)
    target = _triage(kb, conn)

    receipt = adjudicate(
        conn, target, disposition="promote", expect_status="triage", reason=REASON,
        caller_task_id=caller, caller_run_id=run_id,
    )

    assert (receipt["to_status"], receipt["gated"], receipt["run_id"]) == ("ready", False, None)
    task = kb.get_task(conn, target)
    assert task.status == "ready"
    # The promoting seat must NOT own the card: it goes back to the dispatch queue.
    assert task.current_run_id is None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (target,)).fetchone()[0] == 0


def test_promote_reports_the_gate_instead_of_refusing(board):
    """An open parent is not a refusal: the card takes the release and says what it waits on."""
    kb, conn = board
    from hermes_cli.kanban_adjudicate import adjudicate

    caller, run_id = _caller(kb, conn)
    parent = _card(kb, conn, "an unfinished parent")          # ready, not done
    target = _triage(kb, conn, "a gated triage card", parents=[parent])

    receipt = adjudicate(
        conn, target, disposition="promote", expect_status="triage", reason=REASON,
        caller_task_id=caller, caller_run_id=run_id,
    )

    assert receipt["gated"] is True
    assert receipt["to_status"] == "todo"
    assert receipt["parents"] == [parent]
    assert kb.get_task(conn, target).status == "todo"
    (event,) = _events(conn, target, "adjudicated")
    assert event["blocking_parents"] == [parent]


def test_promote_writes_the_spec_fields_it_is_given(board):
    kb, conn = board
    from hermes_cli.kanban_adjudicate import adjudicate

    caller, run_id = _caller(kb, conn)
    target = _triage(kb, conn, "untitled")

    adjudicate(
        conn, target, disposition="promote", expect_status="triage", reason=REASON,
        title="the specified title", body="the specified body",
        caller_task_id=caller, caller_run_id=run_id,
    )

    task = kb.get_task(conn, target)
    assert (task.title, task.body) == ("the specified title", "the specified body")
    assert task.status == "ready"


def test_scheduled_cards_are_released_before_they_act(board):
    """``scheduled`` is fenced: a close that skipped the release would be a card in two states."""
    kb, conn = board
    from hermes_cli.kanban_adjudicate import adjudicate

    caller, run_id = _caller(kb, conn)

    promoted = _card(kb, conn, "a scheduled card")
    kb.claim_task(conn, promoted)
    assert kb.schedule_task(conn, promoted, reason="waiting on a window")
    assert kb.get_task(conn, promoted).status == "scheduled"
    receipt = adjudicate(
        conn, promoted, disposition="promote", expect_status="scheduled", reason=REASON,
        caller_task_id=caller, caller_run_id=run_id,
    )
    assert receipt["to_status"] == "ready"
    assert kb.get_task(conn, promoted).status == "ready"

    closed = _card(kb, conn, "another scheduled card")
    kb.claim_task(conn, closed)
    assert kb.schedule_task(conn, closed, reason="waiting on a window")
    receipt = adjudicate(
        conn, closed, disposition="close", expect_status="scheduled", reason=REASON,
        caller_task_id=caller, caller_run_id=run_id,
    )
    assert receipt["to_status"] == "done"
    assert kb.get_task(conn, closed).status == "done"


# --------------------------------------------------------------------------- refusals


def _attempt(conn, target, **overrides):
    from hermes_cli.kanban_adjudicate import adjudicate

    kwargs = {
        "disposition": "close",
        "expect_status": "triage",
        "reason": REASON,
        "caller_task_id": None,
        "caller_run_id": None,
    }
    kwargs.update(overrides)
    adjudicate(conn, target, **kwargs)


def _base(kb, conn):
    caller, run_id = _caller(kb, conn)
    return caller, run_id, _triage(kb, conn)


def _case_no_caller(kb, conn):
    _, _, target = _base(kb, conn)
    return target, {"caller_task_id": None, "caller_run_id": None}


def _case_own_card(kb, conn):
    caller, run_id, _ = _base(kb, conn)
    return caller, {"caller_task_id": caller, "caller_run_id": run_id,
                    "expect_status": "running"}


def _case_caller_run_not_current(kb, conn):
    caller, run_id, target = _base(kb, conn)
    return target, {"caller_task_id": caller, "caller_run_id": (run_id or 0) + 1000}


def _case_target_unknown(kb, conn):
    caller, run_id, _ = _base(kb, conn)
    return "t_deadbeef", {"caller_task_id": caller, "caller_run_id": run_id}


def _case_not_a_door_source(kb, conn):
    caller, run_id, target = _base(kb, conn)
    return target, {"caller_task_id": caller, "caller_run_id": run_id,
                    "expect_status": "ready"}


def _case_status_moved(kb, conn):
    caller, run_id, target = _base(kb, conn)
    return target, {"caller_task_id": caller, "caller_run_id": run_id,
                    "expect_status": "todo"}


def _case_lane_mismatch(kb, conn):
    caller, run_id = _caller(kb, conn, "lane-a lead", tenant="lane-a")
    target = _triage(kb, conn, "lane-b card", tenant="lane-b")
    return target, {"caller_task_id": caller, "caller_run_id": run_id}


def _case_claim_lost(kb, conn):
    caller, run_id, target = _base(kb, conn)
    conn.execute(
        "UPDATE tasks SET claim_lock = 'some-other-seat', claim_expires = ? WHERE id = ?",
        (int(time.time()) + 600, target),
    )
    conn.commit()
    return target, {"caller_task_id": caller, "caller_run_id": run_id}


#: One board state per ladder cause. A table, not a ladder: the rail's own rule (HX011), and
#: adding a cause means adding its row here, not another branch.
REFUSAL_CASES = {
    "no_caller": _case_no_caller,
    "own_card": _case_own_card,
    "caller_run_not_current": _case_caller_run_not_current,
    "target_unknown": _case_target_unknown,
    "not_a_door_source": _case_not_a_door_source,
    "status_moved": _case_status_moved,
    "lane_mismatch": _case_lane_mismatch,
    "claim_lost": _case_claim_lost,
}


@pytest.mark.parametrize("cause", sorted(REFUSAL_CASES))
def test_every_refusal_cause_writes_nothing(board, cause):
    """Ladder refusals are read-only: the target is byte-identical afterwards."""
    kb, conn = board
    from hermes_cli.kanban_adjudicate import AdjudicationRefused

    target, call = REFUSAL_CASES[cause](kb, conn)

    before = _state(conn, target) if cause != "target_unknown" else None
    with pytest.raises(AdjudicationRefused) as raised:
        _attempt(conn, target, **call)
    assert raised.value.cause == cause
    if before is not None:
        assert _state(conn, target) == before, f"{cause} wrote to the target"


def test_a_refused_gate_returns_the_card_to_ready_with_no_claim(board, monkeypatch):
    """A downstream refusal must not leave the card ``running`` under a dead run."""
    kb, conn = board
    from hermes_cli.kanban_adjudicate import AdjudicationRefused, adjudicate

    caller, run_id = _caller(kb, conn)
    target = _triage(kb, conn)

    refusal = kb.CompletionRefusal("dirty_unit_checkout", "the checkout has uncommitted paths")
    monkeypatch.setattr(kb, "complete_task", lambda *a, **kw: refusal)

    with pytest.raises(AdjudicationRefused) as raised:
        adjudicate(conn, target, disposition="close", expect_status="triage", reason=REASON,
                   caller_task_id=caller, caller_run_id=run_id)

    assert raised.value.cause == "gate_refused"
    assert "dirty_unit_checkout" in raised.value.detail

    task = kb.get_task(conn, target)
    assert task.status == "ready"
    assert task.claim_lock is None
    assert task.current_run_id is None
    assert kb.latest_run(conn, target).outcome == "refused"

    (audit,) = _events(conn, caller, "adjudication_refused")
    assert audit["target_task_id"] == target
    assert audit["cause"] == raised.value.detail
    assert audit["caller_run_id"] == run_id


def test_the_completion_refusal_names_the_status_and_its_door(board):
    """The honesty fix: ``triage`` is not a run that moved, and the refusal says so."""
    kb, conn = board
    target = _triage(kb, conn)

    refusal = kb.complete_task(conn, target, result="closing anyway", summary="closing anyway")

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "status_not_completable"
    assert "triage" in refusal.detail
    assert "kanban_adjudicate" in refusal.detail


def test_the_completion_door_still_accepts_the_statuses_it_owns(board):
    """No widening: the four completable statuses are untouched."""
    kb, conn = board
    from hermes_cli.kanban_pr_acceptance_store import _COMPLETABLE

    assert _COMPLETABLE == frozenset({"running", "ready", "blocked", "review"})
    for status in ("ready", "blocked"):
        card = _card(kb, conn, f"a {status} card")
        if status == "blocked":
            kb.block_task(conn, card, reason="waiting on a lane")
        assert kb.get_task(conn, card).status == status
        assert kb.complete_task(conn, card, result="done", summary="done") is True
