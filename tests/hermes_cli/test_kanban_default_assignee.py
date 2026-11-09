"""Regression tests for #27145 — kanban.default_assignee for unassigned ready tasks.

When the dispatcher hits an unassigned ready task and ``kanban.default_assignee``
is set, the dispatcher applies the assignment and spawns. Without the config,
the task is skipped (existing behavior preserved).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_kanban_home(tmp_path, monkeypatch):
    """Fresh HERMES_HOME with a clean kanban DB."""
    test_home = tmp_path / ".hermes"
    test_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(test_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db
    yield kanban_db, test_home


def _fake_spawn(*args, **kwargs):
    """Stand-in for the real worker spawn — returns a fake PID."""
    return 12345




def test_unassigned_task_auto_assigned_with_default_assignee(isolated_kanban_home):
    """Core #27145 contract: with default_assignee set, an unassigned ready
    task gets the assignment applied and dispatched on the same tick. The
    DB row is mutated (assignee column + an 'assigned' event)."""
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee=None)
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="default",
        )
    assert res.auto_assigned_default == [task_id]
    assert not res.skipped_unassigned
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == task_id
    assert res.spawned[0][1] == "default"

    with kbc.connect_closing() as conn:
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["assignee"] == "default"

    # 'assigned' event emitted for the audit trail
    with kbc.connect_closing() as conn:
        evs = list(conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = 'assigned'",
            (task_id,),
        ))
    assert len(evs) == 1
    payload = json.loads(evs[0][1])
    assert payload["assignee"] == "default"
    assert payload["source"] == "kanban.default_assignee"






def test_explicitly_assigned_task_untouched_by_default_assignee(isolated_kanban_home):
    """A task with an explicit assignee must NOT be touched by the
    default_assignee logic — that fallback only applies to genuinely
    unassigned rows."""
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee="default")
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="someother",
        )
    assert task_id not in res.auto_assigned_default
    assert any(s[0] == task_id and s[1] == "default" for s in res.spawned)


# --- the reserved operator tranche is not the fallback's to adopt (card t_5ffd29bc) --------
#
# Measured defect: an operator ask filed through ``admin_operator_card`` at priority 999999 with
# no assignee was auto-claimed by ``kanban.default_assignee`` four minutes later and dispatched
# back to the lane that filed it — which could neither grant the consent it asked for nor
# complete the card, so the ask cost a session and a half to re-route. The rung IS the operator
# surface; the fallback reads it as an orphan. These pin the seam: the tranche is held, the
# ordinary domain is unchanged, and an explicit owner always wins.

_TRANCHE = 999999
_ORDINARY = 900000


def _rank(kb, conn, task_id, priority):
    """Force a card's priority (the test board has no storage guard armed)."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (priority, task_id))


def _assigned_events(conn, task_id, kind):
    return [
        json.loads(row["payload"])
        for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        )
    ]


def test_operator_rung_card_is_not_adopted_by_default_assignee(isolated_kanban_home):
    """THE guardrail: an unassigned card in the reserved tranche stays unassigned."""
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="operator ask: approve X", assignee=None)
        _rank(kb, conn, task_id, _TRANCHE)
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False, default_assignee="default",
        )
    # The defect first: at base the card IS adopted and spawned. The new bucket is read
    # defensively so a missing field can never mask the behaviour assertion above it.
    assert res.auto_assigned_default == []
    assert res.spawned == []
    assert getattr(res, "skipped_operator_rung", []) == [task_id]
    assert res.skipped_unassigned == []
    assert kbd.tick_deferral_reason(res) == "operator_rung_unowned"
    with kbc.connect_closing() as conn:
        row = conn.execute("SELECT assignee, status, priority FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row["assignee"] is None
        assert row["status"] == "ready"
        assert row["priority"] == _TRANCHE
        # No 'assigned' event; ONE durable 'assign_refused' line naming the reason.
        assert _assigned_events(conn, task_id, "assigned") == []
        refused = _assigned_events(conn, task_id, "assign_refused")
        assert len(refused) == 1
        assert refused[0]["source"] == "kanban.default_assignee"
        assert str(_TRANCHE) in refused[0]["reason"]


def test_operator_rung_hold_records_one_line_across_ticks(isolated_kanban_home):
    """Repeated ticks do not spam the card's history: the refusal is recorded once."""
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="operator ask", assignee=None)
        _rank(kb, conn, task_id, _TRANCHE)
    for _ in range(3):
        with kbc.connect_closing() as conn:
            res = kbd.dispatch_once(
                conn, spawn_fn=_fake_spawn, dry_run=False, default_assignee="default",
            )
        assert res.spawned == []
        assert getattr(res, "skipped_operator_rung", []) == [task_id]
    with kbc.connect_closing() as conn:
        assert len(_assigned_events(conn, task_id, "assign_refused")) == 1
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row["assignee"] is None


def test_ordinary_domain_card_still_adopted(isolated_kanban_home):
    """The fallback keeps its whole ordinary domain — the top of it included."""
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="ordinary card", assignee=None)
        _rank(kb, conn, task_id, _ORDINARY)
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False, default_assignee="default",
        )
    assert getattr(res, "skipped_operator_rung", []) == []
    assert res.auto_assigned_default == [task_id]
    assert [s[0] for s in res.spawned] == [task_id]


def test_operator_rung_card_with_explicit_owner_still_dispatches(isolated_kanban_home):
    """The hold is about an UNOWNED tranche card, never about the tranche itself."""
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="designated SEV", assignee="default")
        _rank(kb, conn, task_id, _TRANCHE)
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False, default_assignee="other-lane",
        )
    assert getattr(res, "skipped_operator_rung", []) == []
    assert res.auto_assigned_default == []
    assert any(s[0] == task_id and s[1] == "default" for s in res.spawned)


def test_no_default_assignee_leaves_the_operator_rung_in_the_routing_bucket(isolated_kanban_home):
    """Without the fallback configured, nothing changed: the card is simply unassigned."""
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="operator ask", assignee=None)
        _rank(kb, conn, task_id, _TRANCHE)
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False, default_assignee=None)
    assert res.skipped_unassigned == [task_id]
    assert getattr(res, "skipped_operator_rung", []) == []


def test_the_real_filing_sequence_stays_unowned(isolated_kanban_home):
    """The measured sequence, replayed through the doors the filer actually uses.

    ``admin_operator_card`` does not write the rung directly — a card may not be BORN in the
    reserved tranche — it creates at the board's floor, DESIGNATES through the ledger door, then
    raises to the rung. t_78dca4ba's event log is exactly that (created -> reprioritized 990000
    designated -> reprioritized 999999). Replaying it here is what makes the guard about the
    RUNG rather than about one test's raw UPDATE: the fallback declines the card the real door
    produced, and the card keeps the owner state the filer chose (none).
    """
    kb, _home = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(
            conn, title="APPROVAL OF RECORD needed: converge the live checkout", assignee=None,
        )
        kb.designate_priority(
            conn, task_id, reason="operator card filed through admin_operator_card",
            authority="default",
        )
        _rank(kb, conn, task_id, _TRANCHE)
    with kbc.connect_closing() as conn:
        landed = conn.execute(
            "SELECT priority, assignee, status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        assert landed["priority"] == _TRANCHE
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False, default_assignee="default",
        )
        after = conn.execute(
            "SELECT priority, assignee, status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
    assert res.auto_assigned_default == []
    assert res.spawned == []
    assert getattr(res, "skipped_operator_rung", []) == [task_id]
    # The card is untouched by the tick: same rank, still ownerless, still ready to be owned.
    assert after["priority"] == _TRANCHE
    assert after["assignee"] is None
    assert after["status"] == "ready"



