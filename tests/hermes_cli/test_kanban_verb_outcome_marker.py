"""A verb may not record a transition it refused (card t_0b5d5988).

``block``/``schedule``/``unblock`` share :func:`hermes_cli.kanban._commented`, which used to
write ``PREFIX: reason`` to ``task_comments`` BEFORE attempting the transition. A refused verb
therefore left a durable board row reading "BLOCKED: ..." in the caller's name while the task
never moved -- and because ``_bulk_apply`` (``hermes_cli/kanban_output.py``) keeps the refusal
only in stdout/stderr, the lie was the durable half and the truth the transient one.

The assertions below run ON THE BOARD (``task_comments`` / ``task_events``), never on the
handler's return code: a return-value test passes against the old code, which is how this
shipped.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

REASON = "dead claim released"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


def _task(conn, status: str) -> str:
    """A card parked in ``status`` -- the state a verb will either move or refuse."""
    tid = kb.create_task(conn, title=f"card-{status}", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    return tid


def _claim_comments(conn, tid: str) -> list[str]:
    """Durable success-prefix claims on this card's thread."""
    prefixes = ("BLOCKED", "SCHEDULED", "UNBLOCK")
    return [c.body for c in kb.list_comments(conn, tid) if c.body.startswith(prefixes)]


def _outcomes(conn, tid: str) -> list[dict]:
    """The writer's own markers for the verb invocations that named this card."""
    return [e.payload for e in kb.list_events(conn, tid)
            if e.kind == kanban_cli.VERB_OUTCOME_EVENT]


def _block(tid: str, ids=None) -> int:
    return kanban_cli._cmd_block(
        argparse.Namespace(task_id=tid, ids=ids, reason=[REASON], kind=None))


def _schedule(tid: str, ids=None) -> int:
    return kanban_cli._cmd_schedule(
        argparse.Namespace(task_id=tid, ids=ids, reason=[REASON],
                           due=None, clear_due=False, window_policy=None))


def _unblock(tid: str, ids=None) -> int:
    return kanban_cli._cmd_unblock(
        argparse.Namespace(task_ids=[tid, *(ids or [])], reason=REASON))


# ---------------------------------------------------------------------------
# A refused verb: no claim, an honest refusal marker, and the row untouched
# ---------------------------------------------------------------------------


def test_refused_block_writes_no_success_comment_and_one_refused_marker(
        kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _task(conn, "done")

        assert _block(tid) == 1

        assert _claim_comments(conn, tid) == []
        assert kb.get_task(conn, tid).status == "done"
        markers = _outcomes(conn, tid)
        assert len(markers) == 1
        assert (markers[0]["verb"], markers[0]["applied"]) == ("block", False)
        assert markers[0]["reason"] == REASON


def test_refused_schedule_writes_no_success_comment_and_one_refused_marker(
        kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _task(conn, "done")

        assert _schedule(tid) == 1

        assert _claim_comments(conn, tid) == []
        assert kb.get_task(conn, tid).status == "done"
        markers = _outcomes(conn, tid)
        assert len(markers) == 1
        assert (markers[0]["verb"], markers[0]["applied"]) == ("schedule", False)


def test_refused_unblock_writes_no_success_comment_and_one_refused_marker(
        kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _task(conn, "ready")

        assert _unblock(tid) == 1

        assert _claim_comments(conn, tid) == []
        assert kb.get_task(conn, tid).status == "ready"
        markers = _outcomes(conn, tid)
        assert len(markers) == 1
        assert (markers[0]["verb"], markers[0]["applied"]) == ("unblock", False)


# ---------------------------------------------------------------------------
# An applied verb: exactly one claim, exactly one applied marker
# ---------------------------------------------------------------------------


def test_applied_block_records_its_reason_and_one_applied_marker(
        kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _task(conn, "ready")

        assert _block(tid) == 0

        assert _claim_comments(conn, tid) == [f"BLOCKED: {REASON}"]
        assert kb.get_task(conn, tid).status == "blocked"
        markers = _outcomes(conn, tid)
        assert len(markers) == 1
        assert (markers[0]["verb"], markers[0]["applied"]) == ("block", True)


def test_applied_schedule_records_its_reason_and_one_applied_marker(
        kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _task(conn, "ready")

        assert _schedule(tid) == 0

        assert _claim_comments(conn, tid) == [f"SCHEDULED: {REASON}"]
        assert kb.get_task(conn, tid).status == "scheduled"
        markers = _outcomes(conn, tid)
        assert len(markers) == 1
        assert (markers[0]["verb"], markers[0]["applied"]) == ("schedule", True)


def test_applied_unblock_records_its_reason_and_one_applied_marker(
        kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _task(conn, "blocked")

        assert _unblock(tid) == 0

        assert _claim_comments(conn, tid) == [f"UNBLOCK: {REASON}"]
        assert kb.get_task(conn, tid).status == "ready"
        markers = _outcomes(conn, tid)
        assert len(markers) == 1
        assert (markers[0]["verb"], markers[0]["applied"]) == ("unblock", True)


# ---------------------------------------------------------------------------
# A mixed batch is decided PER ID, not narrated as a whole
# ---------------------------------------------------------------------------


def test_mixed_batch_claims_only_the_card_that_moved(kanban_home: Path) -> None:
    """The wrapper used to serve the whole batch, so one comment could speak for every id."""
    with kbc.connect_closing() as conn:
        moved = _task(conn, "ready")
        refused = _task(conn, "done")

        assert _block(moved, ids=[refused]) == 1

        assert _claim_comments(conn, moved) == [f"BLOCKED: {REASON}"]
        assert _claim_comments(conn, refused) == []
        assert kb.get_task(conn, moved).status == "blocked"
        assert kb.get_task(conn, refused).status == "done"
        assert [m["applied"] for m in _outcomes(conn, moved)] == [True]
        assert [m["applied"] for m in _outcomes(conn, refused)] == [False]


def test_verb_without_a_reason_still_marks_the_outcome(kanban_home: Path) -> None:
    """No reason -> no claim comment, but the writer's record of what it did is still total."""
    with kbc.connect_closing() as conn:
        tid = _task(conn, "ready")

        rc = kanban_cli._cmd_unblock(argparse.Namespace(task_ids=[tid], reason=None))

        assert rc == 1  # a ready card is not blocked: refused
        assert _claim_comments(conn, tid) == []
        markers = _outcomes(conn, tid)
        assert len(markers) == 1
        assert (markers[0]["verb"], markers[0]["applied"], markers[0]["reason"]) == (
            "unblock", False, None)
