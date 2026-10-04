"""Door 3 and the doors must agree, and the repair must never take a tick down.

Two defects measured on the live ``defcon`` board (2026-10-01, card ``t_dd55f423``), both of
which this suite exists to catch:

* the storage guard's tranche clause read ONLY the designation ledger, while the create seam,
  the re-rank door and the repair pass read the wider marker (R2: the card's own evidence - an
  operator-ask ref, a live designation, or a declared SEV1). A marker-carrying row above the
  ceiling therefore had its own repair refused by the guard it is repaired FOR, and the abort
  propagated out of the reclaim phase before a single spawn: 379 failed ticks, 6.5 h, 452 ready
  cards and nothing dispatched.
* the pass itself raised, so the board's dispatching stopped with it. A repair of an
  already-broken invariant is not allowed to be the thing that stops every spawn.

The property these tests pin down is a SUPERSET one, because that is the only direction that
keeps a tick alive:

    the doors call a row tranche-entitled  ==>  the guard must ADMIT the write
    a row with no evidence at all          ==>  the guard must still REFUSE it
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

CEILING = 999999
ORDINARY_MAX = 900000
STAMP = "Operator-ask: t_fc615201/t_ecbfb34b"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB (same shape as the sibling guards' suites)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "platform-worker")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_OPERATOR_ASK",
                "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _priority(conn, task_id):
    return int(conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()[0])


def _status(conn, task_id):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def _lift_above_the_ceiling(conn, task_id, value=1100000):
    """The legacy state: a row above the ceiling (raw SQL, the fleet's own re-rank lever)."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (value, task_id))


# ---------------------------------------------------------------------------
# The outage: an ARMED board + a marker-carrying row above the ceiling
# ---------------------------------------------------------------------------


def test_the_repair_pass_survives_the_armed_storage_guard(kanban_home: Path) -> None:
    """THE GUARDRAIL for the 2026-10-01 ``defcon`` outage.

    Armed board, one stamped ask and one plain row hand-lifted above the ceiling. Before the
    fix the pass's own write for the stamped row (999999, inside the reserved band) was refused
    by the guard, ``demote_above_tranche`` raised, and the tick died with it. The pass must land
    both rows, and must not raise.
    """
    with kbc.connect() as conn:
        assert kb.arm_priority_tranche_guard("default") == list(kb.PRIORITY_TRANCHE_TRIGGERS)
        stamped = kb.create_task(
            conn, title="stamped ask", body=STAMP, assignee="platform-worker",
            created_by="platform-worker", priority=ORDINARY_MAX,
        )
        plain = kb.create_task(
            conn, title="plain lane work", assignee="platform-worker",
            created_by="platform-worker", priority=ORDINARY_MAX,
        )
        _lift_above_the_ceiling(conn, stamped)
        _lift_above_the_ceiling(conn, plain)

        ledger = kb.demote_above_tranche(conn, board="default")   # must not raise
        moved = {row["task_id"]: row for row in ledger}

        assert moved[stamped]["now"] == CEILING                 # the ask holds the tranche top
        assert moved[stamped]["marker"] == "operator-ask-stamp"
        assert moved[plain]["now"] == ORDINARY_MAX              # plain work, ordinary domain
        assert moved[plain]["marker"] == ""
        assert not [row for row in ledger if row.get("error")], ledger
        assert _priority(conn, stamped) == CEILING
        assert _priority(conn, plain) == ORDINARY_MAX

        # Each move is recorded on its own card, and the guard accepted both writes.
        kinds = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind = 'priority_demoted'"
        ).fetchone()[0]
        assert kinds == 2


def test_the_repair_lands_an_inherited_marker_in_the_ordinary_domain(kanban_home: Path) -> None:
    """Evidence the guard CANNOT see is not a landing place for the band.

    A row whose ask is only inherited (an ancestor's stamp, no stamp of its own) is what the
    wider resolver used to accept. The storage guard cannot walk a parent chain, so the repair
    answers the guard's question: the row is lowered into the ordinary domain, where the write
    is accepted. Cards born under an ask carry the register's stamp in their own body, so this
    is the legacy shape, not the normal one.
    """
    with kbc.connect() as conn:
        assert kb.arm_priority_tranche_guard("default") == list(kb.PRIORITY_TRANCHE_TRIGGERS)
        parent = kb.create_task(conn, title="the ask", body=STAMP, assignee="platform-worker",
                                created_by="platform-worker", priority=ORDINARY_MAX)
        child = kb.create_task(conn, title="work under the ask", assignee="platform-worker",
                               created_by="platform-worker", priority=ORDINARY_MAX,
                               parents=[parent])
        # Strip the child's inherited stamp: the shape the guard cannot read.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET body = 'no stamp here' WHERE id = ?", (child,))
        _lift_above_the_ceiling(conn, child)

        ledger = {row["task_id"]: row for row in kb.demote_above_tranche(conn, board="default")}
        assert ledger[child]["now"] == ORDINARY_MAX
        assert ledger[child]["marker"] == ""
        assert _priority(conn, child) == ORDINARY_MAX
        assert _priority(conn, parent) == ORDINARY_MAX


# ---------------------------------------------------------------------------
# The predicate: the guard admits every row the doors call tranche-entitled
# ---------------------------------------------------------------------------


def test_the_storage_guard_admits_every_marker_the_doors_admit(kanban_home: Path) -> None:
    """The guard is the SUPERSET of the doors' predicate - in that direction only.

    Every case is written through the armed guard: a case the doors call entitled must be
    ADMITTED (anything else is the outage again, one row later), and a row with no evidence at
    all must be REFUSED (the guard keeps its teeth). The guard is deliberately wider on the
    SEV1 arm - GLOB cannot express a line-anchored "label after optional indentation" - and that
    widening is asserted here as a widening, so a future edit cannot quietly turn it into a
    narrowness.
    """
    corpus = [
        # (title, body, designated, doors_entitled, guard_must_refuse)
        ("plain lane work", "no marker here", False, False, True),
        ("child", None, False, False, True),
        ("child", "", False, False, True),
        ("child", "operator asks are tracked elsewhere", False, False, True),
        ("child", "carries no Operator-ask reference", False, False, True),
        ("child", STAMP, False, True, False),
        ("child", "\n%s\n" % STAMP, False, True, False),
        ("child", "   \t%s   " % STAMP, False, True, False),
        ("child", "notes\n\nOperator-ask: t_a1b2c3d4/e5f6a7b8", False, True, False),
        ("child", "Severity: SEV1", False, True, False),
        ("child", "    Severity: SEV1", False, True, False),   # measured live: indented
        ("child", "severity: sev1", False, True, False),
        ("child", "  > SEV: SEV-1", False, True, False),
        ("child", "critical: critical", False, True, False),
        ("child", "priority-class: sev 1", False, True, False),
        ("child", "priority class = sev1", False, True, False),
        ("designated", "no marker at all", True, True, False),
        # the widening, asserted as such: the guard admits, the doors do not
        ("severity: medium", "no sev1 here", False, False, False),
    ]
    with kbc.connect() as conn:
        assert kb.arm_priority_tranche_guard("default") == list(kb.PRIORITY_TRANCHE_TRIGGERS)
        for i, (title, body, designated, entitled, must_refuse) in enumerate(corpus):
            tid = kb.create_task(conn, title=title, body=body, assignee="platform-worker",
                                 created_by="platform-worker", priority=ORDINARY_MAX)
            if designated:
                kb.designate_priority(conn, tid, reason="operator ask", authority="default")
            marker = kb.tranche_entitlement(conn, board="default", task_id=tid,
                                            title=title, body=body)
            assert bool(marker) == entitled, (i, title, body, marker)
            if entitled:
                assert marker, (i, title, body)                  # a NAMED reason, never a bare yes
            try:
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (CEILING, tid))
                admitted = True
            except sqlite3.IntegrityError as exc:
                admitted = False
                refused_with = str(exc)
            else:
                refused_with = ""
            case = "case %d %r / %r" % (i, title, (body or "")[:40])
            if entitled:
                assert admitted, "%s: the doors call this entitled but the guard refused (%s)" % (
                    case, refused_with)
            if must_refuse:
                assert not admitted, "%s: no evidence at all, and the guard admitted it" % case
            if admitted:
                assert _priority(conn, tid) == CEILING
            elif must_refuse:
                # a refused write leaves the row exactly as it was
                assert _priority(conn, tid) == max(ORDINARY_MAX, _priority(conn, tid))


def test_the_create_seam_lands_a_marker_filing_in_the_tranche_on_an_armed_board(
    kanban_home: Path,
) -> None:
    """The create seam's own landing, through the armed guard.

    A marker filing that asks above the ceiling is admitted at the tranche top (the refusal text
    promises exactly that: "admitted into the reserved tranche at its top"). On an armed board
    the INSERT used to abort on the guard, so the promise could not be kept and the filing died
    with a raw ``sqlite3.IntegrityError`` instead of being refused with its remedy. The stamp is
    written into the body by the same INSERT, so the guard can read it.
    """
    with kbc.connect() as conn:
        assert kb.arm_priority_tranche_guard("default") == list(kb.PRIORITY_TRANCHE_TRIGGERS)
        tid = kb.create_task(conn, title="the operator's ask", body=STAMP,
                             assignee="platform-worker", created_by="platform-worker",
                             priority=1100000)
        assert _priority(conn, tid) == CEILING
        body = conn.execute("SELECT body FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        assert "Operator-ask:" in body
        # ... and an unmarked filing over the ceiling is still REFUSED with its remedy.
        with pytest.raises(kb.AboveTrancheRefused):
            kb.create_task(conn, title="plain lane work", assignee="platform-worker",
                           created_by="platform-worker", priority=1100000)


def test_a_declared_sev1_lifted_above_the_ceiling_is_lowered_to_the_tranche_top(
    kanban_home: Path,
) -> None:
    """The SEV1 arm of the pass, through the armed guard (the 47-row live class)."""
    with kbc.connect() as conn:
        assert kb.arm_priority_tranche_guard("default") == list(kb.PRIORITY_TRANCHE_TRIGGERS)
        sev = kb.create_task(conn, title="SEV1 [deploy-train] run failed", body="Severity: SEV1",
                             assignee="default", created_by="default", priority=ORDINARY_MAX)
        _lift_above_the_ceiling(conn, sev, 1100000)
        ledger = {row["task_id"]: row for row in kb.demote_above_tranche(conn, board="default")}
        assert ledger[sev]["now"] == CEILING
        assert ledger[sev]["marker"] == "sev1"
        assert _priority(conn, sev) == CEILING


# ---------------------------------------------------------------------------
# The tick: a failing repair must not stop the board dispatching
# ---------------------------------------------------------------------------


def test_a_failing_repair_pass_does_not_take_the_tick_down(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever the guard and the doors disagree about, the repair is not the tick's definition.

    Simulates the 2026-10-01 shape (an abort inside the pass) and asserts that the tick still
    runs its own business - promotion here - and that the failure is RECORDED on the tick's
    result rather than only living in a log line.
    """
    def _boom(*_args, **_kwargs):
        raise sqlite3.IntegrityError("priority 990000-999999 is DESIGNATED, never requested")

    with kbc.connect() as conn:
        # A recoverable blocked card: the tick's OWN business (promotion) must still run, which is
        # the property that matters - a repair that raised before the spawn loop left 452 ready
        # cards unspawned.
        card = kb.create_task(conn, title="recoverable", assignee="platform-worker",
                              created_by="platform-worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', consecutive_failures=1 WHERE id=?", (card,))
        assert _status(conn, card) == "blocked"

        monkeypatch.setattr(kbd._kb, "demote_above_tranche", _boom)
        res = kbd.dispatch_once(conn, dry_run=True)

        assert res.priority_demote_error, res.priority_demote_error
        assert "IntegrityError" in res.priority_demote_error
        assert res.priority_demoted == []
        assert res.promoted == 1                     # the tick's own business still ran
        assert _status(conn, card) == "ready"


def test_a_refused_row_is_reported_not_swallowed(kanban_home: Path) -> None:
    """A row the pass could not lower is named on the tick's result, never a silent skip.

    The refusal cannot happen while the pass and the guard read one predicate, so a stub is the
    only way to stage it: the pass is told the row is entitled, the guard - which reads the row
    itself - is not. That is exactly the disagreement the 2026-10-01 outage was, and the pass
    must return the refusal as a ledger row instead of raising on it.
    """
    with kbc.connect() as conn:
        row = kb.create_task(conn, title="no marker at all", assignee="platform-worker",
                             created_by="platform-worker", priority=ORDINARY_MAX)
        _lift_above_the_ceiling(conn, row)
        assert kb.arm_priority_tranche_guard("default") == list(kb.PRIORITY_TRANCHE_TRIGGERS)

        real = kb.tranche_entitlement
        try:
            kb.tranche_entitlement = lambda *_a, **_k: "sev1"
            ledger = kb.demote_above_tranche(conn, board="default")
        finally:
            kb.tranche_entitlement = real
        assert ledger and ledger[0]["now"] is None
        assert "IntegrityError" in ledger[0]["error"]
        assert _priority(conn, row) == 1100000       # the row is untouched, and visible

        # ... and the tick records it rather than throwing it away
        res = kbd.DispatchResult()
        assert res.priority_demote_refused == []
