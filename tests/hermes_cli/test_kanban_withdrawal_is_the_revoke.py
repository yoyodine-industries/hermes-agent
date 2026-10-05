"""Withdrawal IS the revoke (t_c80646d1).

Archiving a card is how an operator ask is withdrawn, and an archived card is not a standing
request - so ``archive_task`` must release the card's live priority designation in the SAME
transaction. The designation door is fenced from every card run
(``_DELEGATED_CHILD_DENIED_ACTIONS``), so if the archive does not release the ledger row nothing
else ever can, and the operator-request register (``priority_designations`` rows marked
``Operator ask`` / ``Operator ruling``) keeps reporting the withdrawn ask as standing until
somebody hand-runs ``hermes kanban defcon revoke``.

The assertions hold the two halves the fix must hold at once: the ledger row is stamped and the
card leaves the tranche (the release), and it happens on the archive call alone (the atomicity).
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


BAND = '''
def band_birth(requested, assignee, board, title, body):
    applied = min(int(requested), 800000)
    return {"asked": requested, "applied": applied, "clamped": applied != requested,
            "reason": "outside the band", "status": "done"}
'''


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HERMES_HOME: its own board, and no board wiring of any kind."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_OPERATOR_ASK",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ADVISORY_SKILLS"):
        monkeypatch.delenv(var, raising=False)
    db_path = kb.kanban_db_path(board="default")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return tmp_path


@pytest.fixture
def conn(home):
    with kbc.connect_closing() as c:
        yield c


def _wire(home, source, *, function="band_birth", board="default"):
    path = home / "band_policy.py"
    path.write_text(source, encoding="utf-8")
    kb.write_board_metadata(board, priority_policy={"module": str(path), "function": function})
    return path


def _priority(conn, task_id):
    return conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()["priority"]


def _events(conn, task_id, kind):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else None for r in rows]


def _operator_ask(conn, task_id):
    """A designation the register reads: the reason carries the ``Operator ask`` marker."""
    return kb.designate_priority(conn, task_id, reason="Operator ask: register hygiene demo",
                                 authority="operator")


def test_archiving_a_designated_card_stamps_the_ledger_and_leaves_the_tranche(conn, home):
    _wire(home, BAND)
    tid = kb.create_task(conn, title="an operator ask", assignee="default", priority=899998)
    _operator_ask(conn, tid)
    assert kb.is_priority_designated(conn, tid, board="default") is True

    assert kb.archive_task(conn, tid) is True

    row = kb.priority_designation(conn, tid)
    assert row["revoked_at"] is not None          # the register no longer reports it standing
    assert kb.is_priority_designated(conn, tid, board="default") is False
    assert _priority(conn, tid) == row["priority"] == 800000
    # The DESIGNATION's own reason/authority stay as the audit trail.
    assert row["reason"] == "Operator ask: register hygiene demo"
    assert row["authority"] == "operator"


def test_the_archive_release_names_its_cause_on_the_event(conn, home):
    _wire(home, BAND)
    tid = kb.create_task(conn, title="an operator ask", assignee="default", priority=899998)
    _operator_ask(conn, tid)
    kb.archive_task(conn, tid)

    release = _events(conn, tid, "reprioritized")[-1]
    assert release["designation"] == "revoked"
    assert release["priority"] == 800000
    assert release["cause"] == "archive"          # distinguishable from the operator door
    # The withdraw and the revoke are ONE act: both events exist after the one archive call.
    assert _events(conn, tid, "archived")
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] == "archived"


def test_archiving_an_undesignated_card_is_untouched(conn, home):
    _wire(home, BAND)
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=12345)
    kb.archive_task(conn, tid)
    assert kb.priority_designation(conn, tid) is None
    assert _priority(conn, tid) == 12345
    assert _events(conn, tid, "reprioritized") == []


def test_archiving_a_card_whose_designation_was_already_revoked_does_not_restamp(conn, home):
    _wire(home, BAND)
    tid = kb.create_task(conn, title="an operator ask", assignee="default", priority=899998)
    _operator_ask(conn, tid)
    back = kb.revoke_priority_designation(conn, tid, reason="the release shipped")
    before = kb.priority_designation(conn, tid)
    kb.archive_task(conn, tid)
    after = kb.priority_designation(conn, tid)
    assert after["revoked_at"] == before["revoked_at"] == back["revoked_at"]
    # One release, one event: the archive adds no second reprioritized row.
    assert len(_events(conn, tid, "reprioritized")) == 2   # designated + revoked


def test_the_door_and_the_archive_release_identically(conn, home):
    """The archive seam and the operator door release by ONE implementation, so they cannot drift."""
    _wire(home, BAND)
    door = kb.create_task(conn, title="released by the door", assignee="default", priority=899998)
    seam = kb.create_task(conn, title="released by the archive", assignee="default", priority=899998)
    _operator_ask(conn, door)
    _operator_ask(conn, seam)

    kb.revoke_priority_designation(conn, door, reason="the release shipped")
    kb.archive_task(conn, seam)

    assert kb.priority_designation(conn, door)["revoked_at"] is not None
    assert kb.priority_designation(conn, seam)["revoked_at"] is not None
    assert _priority(conn, door) == _priority(conn, seam) == 800000
    # The door path keeps the door's event shape (no cause key); only the archive names one.
    assert "cause" not in _events(conn, door, "reprioritized")[-1]
    assert _events(conn, seam, "reprioritized")[-1]["cause"] == "archive"


def test_a_designation_restored_between_the_look_and_the_release_is_not_restamped(conn, home):
    """A second releaser wins the race: the archive's release is a no-op on an already-revoked row.

    ``revoke_priority_designation`` looks at the row before opening its txn (so a card that was
    never designated is a no-op that opens nothing). The archive seam has no such pre-look - it
    runs inside the archive txn - so the guard that makes the release idempotent is the
    ``revoked_at`` test INSIDE ``_release_designation_rows`` itself. Prove it there.
    """
    _wire(home, BAND)
    tid = kb.create_task(conn, title="an operator ask", assignee="default", priority=899998)
    _operator_ask(conn, tid)
    kb.revoke_priority_designation(conn, tid, reason="released elsewhere")
    stamp = kb.priority_designation(conn, tid)["revoked_at"]
    with kb.write_txn(conn):  # the seam's only contract: it never re-stamps a revoked row
        assert kb._release_designation_rows(conn, tid, reason="withdrawn: card archived",
                                            cause="archive") is None
    assert kb.priority_designation(conn, tid)["revoked_at"] == stamp
    assert len(_events(conn, tid, "reprioritized")) == 2
