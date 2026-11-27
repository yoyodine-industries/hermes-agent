"""A claim left on a SETTLED row: released by ``release_orphaned_claims``, by nothing else.

Regression for the platform board's stranded population measured 2026-11-15: 8 cards (7
``ready``, 1 ``review``) held an expired claim whose worker pid was gone -- the oldest
expired 3h13m earlier -- and no verb on the host could release them. ``release_stale_claims``
is scoped ``status = 'running'``, the crash sweep scans only ``running`` rows, and dispatch
claims only ``status = 'ready' AND claim_lock IS NULL``, so each of those cards was invisible
to dispatch forever while the board read it as ordinary work.

These tests pin both halves of the line: a settled row's expired residue is released without
touching its status or its failure accounting, and a ``running`` row (which belongs to the
reclaim path, with its termination and breaker semantics) is left alone.
"""

from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

from pathlib import Path


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _settled_with_expired_claim(conn, *, status: str, expired_for: int = 11_580) -> str:
    """A card that was claimed, then settled to ``status`` with the claim left behind."""
    task_id = kb.create_task(conn, title=f"settled {status}", assignee="a")
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, task_id, claimer=f"{host}:worker")
    kbd._set_worker_pid(conn, task_id, 424242)
    conn.execute(
        "UPDATE tasks SET status = ?, claim_expires = ?, started_at = ? WHERE id = ?",
        (status, int(time.time()) - expired_for, int(time.time()) - expired_for - 60, task_id),
    )
    conn.commit()
    return task_id


def _row(conn, task_id: str):
    return conn.execute(
        "SELECT status, claim_lock, claim_expires, worker_pid, worker_started_at, "
        "       consecutive_failures, last_failure_error "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def test_a_ready_card_holding_an_expired_claim_is_released(kanban_home):
    with kbc.connect() as conn:
        task_id = _settled_with_expired_claim(conn, status="ready")

        assert kb.release_orphaned_claims(conn) == 1

        row = _row(conn, task_id)
        assert row["claim_lock"] is None
        assert row["claim_expires"] is None
        assert row["worker_pid"] is None
        assert row["worker_started_at"] is None
        # The card keeps the status it had settled to -- this is a release, not a requeue.
        assert row["status"] == "ready"
        # Residue is not a failed attempt: no failure is booked against the card.
        assert row["consecutive_failures"] == 0
        assert row["last_failure_error"] is None

        events = kb.list_events(conn, task_id)
        kinds = [e.kind for e in events]
        assert "claim_released_orphaned" in kinds
        assert "reclaimed" not in kinds
        assert "gave_up" not in kinds
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                (task_id, "claim_released_orphaned"),
            ).fetchone()["payload"]
        )
        assert payload["status_kept"] == "ready"
        assert payload["worker_pid"] == 424242
        assert payload["expired_for_seconds"] >= 11_580
        assert payload["stale_lock"].startswith(kb._claimer_id().split(":", 1)[0] + ":")


def test_a_review_card_keeps_its_review_status(kanban_home):
    """A review card must not be bounced back to ``ready`` by a claim release."""
    with kbc.connect() as conn:
        task_id = _settled_with_expired_claim(conn, status="review")

        assert kb.release_orphaned_claims(conn) == 1

        row = _row(conn, task_id)
        assert row["status"] == "review"
        assert row["claim_lock"] is None


def test_a_blocked_card_is_released_and_stays_blocked(kanban_home):
    with kbc.connect() as conn:
        task_id = _settled_with_expired_claim(conn, status="blocked")

        assert kb.release_orphaned_claims(conn) == 1

        row = _row(conn, task_id)
        assert row["status"] == "blocked"
        assert row["claim_lock"] is None


def test_a_running_row_is_left_to_the_reclaim_path(kanban_home):
    """``running`` rows carry termination + breaker semantics; they are not this function's."""
    with kbc.connect() as conn:
        task_id = _settled_with_expired_claim(conn, status="running")

        assert kb.release_orphaned_claims(conn) == 0
        assert _row(conn, task_id)["claim_lock"] is not None


def test_the_running_scoped_reclaim_never_saw_these_rows(kanban_home):
    """The gap this fixes: the existing reclaim returns 0 on a settled row."""
    with kbc.connect() as conn:
        task_id = _settled_with_expired_claim(conn, status="ready")

        assert kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None) == 0
        assert _row(conn, task_id)["claim_lock"] is not None

        assert kb.release_orphaned_claims(conn) == 1
        assert _row(conn, task_id)["claim_lock"] is None


def test_an_unexpired_claim_is_not_touched(kanban_home):
    with kbc.connect() as conn:
        task_id = _settled_with_expired_claim(conn, status="ready")
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) + 3600, task_id),
        )
        conn.commit()

        assert kb.release_orphaned_claims(conn) == 0
        assert _row(conn, task_id)["claim_lock"] is not None


def test_an_empty_string_lock_is_not_a_claim(kanban_home):
    """``''`` passes every presence test; the census must not mistake it for a held lock."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="done with empty lock", assignee="a")
        conn.execute(
            "UPDATE tasks SET status = 'done', claim_lock = '', claim_expires = ? WHERE id = ?",
            (int(time.time()) - 999, task_id),
        )
        conn.commit()

        assert kb.release_orphaned_claims(conn) == 0
        assert _row(conn, task_id)["claim_lock"] == ""


def test_a_null_expiry_is_not_releasable(kanban_home):
    """No expiry means no evidence the claim ever lapsed; hold it."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="ready, lock, no expiry", assignee="a")
        host = kb._claimer_id().split(":", 1)[0]
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = ?, claim_expires = NULL WHERE id = ?",
            (f"{host}:999", task_id),
        )
        conn.commit()

        assert kb.release_orphaned_claims(conn) == 0
        assert _row(conn, task_id)["claim_lock"] is not None


def test_the_release_is_idempotent(kanban_home):
    with kbc.connect() as conn:
        _settled_with_expired_claim(conn, status="ready")

        assert kb.release_orphaned_claims(conn) == 1
        assert kb.release_orphaned_claims(conn) == 0


def test_a_released_card_is_dispatchable_again(kanban_home):
    """The point of the release: dispatch's own predicate now selects the card.

    Mirrors ``kanban_db_dispatch``'s claim query -- ``status = 'ready' AND claim_lock IS NULL``
    -- because a ``ready`` row holding a lock is the row dispatch can never reach.
    """
    with kbc.connect() as conn:
        task_id = _settled_with_expired_claim(conn, status="ready")

        def dispatchable() -> set[str]:
            return {
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM tasks WHERE status = 'ready' AND claim_lock IS NULL "
                    "  AND assignee IS NOT NULL"
                ).fetchall()
            }

        assert task_id not in dispatchable()
        kb.release_orphaned_claims(conn)
        assert task_id in dispatchable()


def test_the_measured_board_shape_releases_all_and_only_the_settled(kanban_home):
    """The 2026-11-15 platform shape: 7 ready + 1 review stranded, 1 running left alone."""
    with kbc.connect() as conn:
        settled = [_settled_with_expired_claim(conn, status="ready") for _ in range(7)]
        review = _settled_with_expired_claim(conn, status="review")
        running = _settled_with_expired_claim(conn, status="running")
        live = _settled_with_expired_claim(conn, status="ready")
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) + 600, live),
        )
        conn.commit()

        assert kb.release_orphaned_claims(conn) == 8

        for task_id in [*settled, review]:
            row = _row(conn, task_id)
            assert row["claim_lock"] is None and row["claim_expires"] is None
        assert _row(conn, review)["status"] == "review"
        assert _row(conn, running)["claim_lock"] is not None
        assert _row(conn, live)["claim_lock"] is not None
