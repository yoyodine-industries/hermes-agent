"""Respawn-guard tests: ``active_pr`` honours a deliberate re-queue.

Regression for the 24h deadlock: a card whose review sent it back for changes
sits in ``ready`` with its own PR-URL comment still inside
``_RESPAWN_GUARD_PR_WINDOW``, so ``check_respawn_guard`` keeps returning
``active_pr`` and the implementer is never re-spawned to fix that PR. The
re-queue that a review (or an operator unblock) produces is exactly the
"deliberate re-run" the guard must not refuse; the worker amends the SAME
branch, so no duplicate PR is possible.

Step 3 (``recent_success``) already voids itself when a re-queue event
(``status``, ``promoted``, ``unblocked``, ``reclaimed``) is at least as new as
the state it guards. Step 4 mirrors that predicate: it holds only while no such
event is at least as new as the NEWEST PR-URL comment in the window.
``_RESPAWN_GUARD_PR_WINDOW`` (24h) and the review-lane early return are
unchanged, and a PR comment with no later re-queue still holds.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PR_COMMENT = "Opened https://github.com/example/repo/pull/123 for review."

# The re-queue event kinds step 3 already treats as "run it again".
_REQUEUE_KINDS = ("status", "promoted", "unblocked", "reclaimed")


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _add_pr_comment(conn, task_id: str, created_at: int) -> None:
    """Write the PR-URL comment with an explicit timestamp (no clock races)."""
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'worker', ?, ?)",
            (task_id, PR_COMMENT, created_at),
        )


def _add_event(conn, task_id: str, kind: str, created_at: int) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, NULL, ?)",
            (task_id, kind, created_at),
        )


@pytest.mark.parametrize("kind", _REQUEUE_KINDS)
def test_requeue_event_at_the_pr_comment_second_lifts_active_pr_guard(
    kanban_home: Path, kind: str
) -> None:
    """A re-queue event at (or after) the newest PR-URL comment voids ``active_pr``.

    The same-second case is the tightest one that must still void the guard:
    step 3 reads its re-queue test inclusively (``created_at >=``), and this
    branch mirrors it so the two reasons cannot disagree about one event.
    """
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="changes requested", assignee="worker")
        at = int(time.time())
        _add_pr_comment(conn, task_id, at)
        _add_event(conn, task_id, kind, at)

        assert kbd.check_respawn_guard(conn, task_id) is None


def test_active_pr_guard_holds_without_a_requeue_event(kanban_home: Path) -> None:
    """The general case is unchanged: a fresh PR-URL comment still defers ready."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="already PRed", assignee="worker")
        _add_pr_comment(conn, task_id, int(time.time()))

        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"


def test_active_pr_guard_holds_when_the_requeue_precedes_the_newest_comment(
    kanban_home: Path,
) -> None:
    """The reference is the NEWEST PR-URL comment, not any comment in the window."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="requeued then PRed", assignee="worker")
        at = int(time.time())
        _add_event(conn, task_id, "status", at)
        _add_pr_comment(conn, task_id, at + 5)

        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"


def test_active_pr_guard_is_not_lifted_by_an_unrelated_event(kanban_home: Path) -> None:
    """Only the re-queue kinds count — any other event is not a "run it again"."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="commented", assignee="worker")
        at = int(time.time())
        _add_pr_comment(conn, task_id, at)
        _add_event(conn, task_id, "commented", at + 5)

        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"


def test_active_pr_guard_is_lifted_by_the_review_changes_handoff(
    kanban_home: Path,
) -> None:
    """The real review handoff re-queues with a *changes_requested* event only.

    Driven through the real API rather than a synthetic event, because the kind
    the handoff writes is the whole question: it moves the card review→ready by
    ``UPDATE tasks`` and appends ``changes_requested`` — no ``status`` event goes
    with it, so a lift set built from the ``recent_success`` precedent alone
    leaves the exact shape that deadlocked the card still guarded.
    """
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="sent back", assignee="builder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        kb.add_comment(conn, task_id, author="builder", body=PR_COMMENT)
        assert kb.request_review(
            conn, task_id, summary="v1", reviewer="reviewer",
            expected_run_id=claimed.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id)
        assert review is not None
        assert kb.request_changes(
            conn, task_id, reason="fix the migration",
            expected_run_id=review.current_run_id,
        ) == (True, "builder")
        assert kb.get_task(conn, task_id).status == "ready"

        assert kbd.check_respawn_guard(conn, task_id) is None
