"""The ARCHIVED-KEY seam: what a create does when its key is held by a tombstone.

WHY (ruling on card ``t_2bc058fc``, leg B of the landing-flow hardening, card
``t_0f97637f``). ``create_task`` deduped an idempotency key with
``status != 'archived'``, so an ARCHIVED card FREED its key for every caller in the
fleet: the same incident was re-filed beside its own tombstone (measured live on the
``platform`` board 2026-10-28: 17,148 keyed rows over 14,591 distinct keys, 2,557
duplicate keyed rows, 1,357 key groups holding at least one archived row). The seam
is an OPT-IN policy on the create door -- the default stays byte-identical, because
a default flip would turn every re-filing caller into a silent no-op.

The three answers are named, and the fourth (an unknown name) is REFUSED rather than
guessed:

  * ``recreate``          -- today's behaviour: a new card under the same key;
  * ``return_tombstone``  -- the archived card's id, nothing inserted;
  * ``reopen``            -- that card, moved back to a live status through
                             ``reopen_task`` in one audited act, nothing inserted.

These are the card's acceptance tests: the default is the regression guard, and each
opt-in is proved by the ROW COUNT (a policy that returns an id and also inserts is
the twin it exists to prevent).
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

KEY = "sdlc-refusal:test-signature:lane-a"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(__import__("pathlib").Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_DB", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _file(conn, key=KEY, title="first"):
    return kb.create_task(conn, title=title, assignee="platform-coder",
                          idempotency_key=key)


def _rows(conn, key=KEY):
    return conn.execute(
        "SELECT id, status FROM tasks WHERE idempotency_key = ? ORDER BY created_at",
        (key,)).fetchall()


def _archive(conn, task_id):
    assert kb.archive_task(conn, task_id) is True
    return _rows(conn)


# --------------------------------------------------------------------------- #
# the default: NOT changed
# --------------------------------------------------------------------------- #

def test_default_recreate_still_mints_a_second_card_for_an_archived_key(conn):
    """THE REGRESSION GUARD: with the argument absent, nothing about a filing changes.

    An archived key still frees itself and the create mints a SECOND row, exactly as
    every caller on the host does today. If this test ever fails, a default flip
    slipped into the seam and every re-filing caller in the fleet is a silent no-op.
    """
    first = _file(conn, title="the one that got archived")
    assert [r["status"] for r in _archive(conn, first)] == ["archived"]

    second = kb.create_task(conn, title="the same incident, re-filed", assignee="platform-coder",
                            idempotency_key=KEY)
    rows = _rows(conn)
    assert second != first
    assert [r["id"] for r in rows] == [first, second]
    assert [r["status"] for r in rows] == ["archived", "ready"]


def test_default_is_named_and_stable(conn):
    """The default is a NAME the fleet can read, not an implicit else-branch."""
    assert kb.DEFAULT_ARCHIVED_KEY_POLICY == "recreate"
    assert "recreate" in kb.ARCHIVED_KEY_POLICIES
    assert set(kb.ARCHIVED_KEY_POLICIES) == {"recreate", "return_tombstone", "reopen"}


# --------------------------------------------------------------------------- #
# the opt-ins
# --------------------------------------------------------------------------- #

def test_return_tombstone_returns_the_archived_id_and_inserts_nothing(conn):
    first = _file(conn, title="the one that got archived")
    _archive(conn, first)

    answer = kb.create_task(conn, title="never written", assignee="platform-coder",
                            idempotency_key=KEY,
                            archived_key_policy="return_tombstone")
    rows = _rows(conn)
    assert answer == first
    assert len(rows) == 1 and rows[0]["status"] == "archived"


def test_reopen_returns_it_flips_it_live_and_inserts_nothing(conn):
    first = _file(conn, title="the one that got archived")
    _archive(conn, first)

    answer = kb.create_task(conn, title="never written", assignee="platform-coder",
                            idempotency_key=KEY,
                            archived_key_policy="reopen",
                            archived_key_actor="platform-coder",
                            archived_key_reason="landing still owed: carrier identity held by the key")
    rows = _rows(conn)
    assert answer == first
    assert len(rows) == 1
    assert rows[0]["status"] in {"ready", "todo"}
    # ONE audited act: the reopen event names the actor and the reason verbatim.
    events = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY rowid",
        (first,)).fetchall()
    reopened = [e for e in events if e["kind"] == "reopened"]
    assert len(reopened) == 1, [e["kind"] for e in events]
    assert "platform-coder" in reopened[0]["payload"]
    assert "landing still owed" in reopened[0]["payload"]


def test_reopen_refuses_without_an_actor_or_a_reason(conn):
    """``reopen`` may only be used where the caller can NAME an actor and a reason."""
    first = _file(conn, title="the one that got archived")
    _archive(conn, first)

    with pytest.raises(ValueError) as no_actor:
        kb.create_task(conn, title="x", assignee="platform-coder", idempotency_key=KEY,
                       archived_key_policy="reopen", archived_key_reason="because")
    assert "actor" in str(no_actor.value)

    with pytest.raises(ValueError) as no_reason:
        kb.create_task(conn, title="x", assignee="platform-coder", idempotency_key=KEY,
                       archived_key_policy="reopen", archived_key_actor="platform-coder")
    assert "reason" in str(no_reason.value)

    # A refusal writes NOTHING: still one row, still archived.
    assert [(r["id"], r["status"]) for r in _rows(conn)] == [(first, "archived")]


def test_an_unknown_policy_is_refused_by_name(conn):
    with pytest.raises(ValueError) as excinfo:
        kb.create_task(conn, title="x", assignee="platform-coder", idempotency_key=KEY,
                       archived_key_policy="reopens")
    message = str(excinfo.value)
    assert "reopens" in message
    for name in kb.ARCHIVED_KEY_POLICIES:
        assert name in message
    assert _rows(conn) == []


# --------------------------------------------------------------------------- #
# the opt-ins do not disturb the LIVE-card dedupe they sit beside
# --------------------------------------------------------------------------- #

def test_a_live_card_still_wins_over_a_tombstone_under_the_same_key(conn):
    """A live hit is answered FIRST, whatever the policy: the key is not free."""
    tombstoned = _file(conn, title="older, archived")
    _archive(conn, tombstoned)
    live = kb.create_task(conn, title="the live twin", assignee="platform-coder",
                          idempotency_key=KEY)
    for policy in ("recreate", "return_tombstone", "reopen"):
        answer = kb.create_task(conn, title="never written", assignee="platform-coder",
                                idempotency_key=KEY, archived_key_policy=policy,
                                archived_key_actor="platform-coder",
                                archived_key_reason="the landing is still owed")
        assert answer == live, policy
    assert [r["id"] for r in _rows(conn)] == [tombstoned, live]


def test_a_finished_card_is_left_alone_by_the_archived_policy(conn):
    """``done`` is NOT a tombstone: the dedupe has always returned it, and still does."""
    first = _file(conn, title="closed, not archived")
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (first,))
    conn.commit()

    answer = kb.create_task(conn, title="never written", assignee="platform-coder",
                            idempotency_key=KEY, archived_key_policy="return_tombstone")
    assert answer == first
    assert [(r["id"], r["status"]) for r in _rows(conn)] == [(first, "done")]
