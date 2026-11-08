"""The closure floor: a completing card's DoD-carrying children (card t_b0d16f44).

The operator's prioritisation standard: a card that carries a higher-priority card's
remaining DoD must never sit below it, because priority governs spawn ORDER — a closure
filed low starves the chain it gates (five live violations measured 2026-09-28: a git
alignment closure's five children all born at 0, a refusal carrier's two children at 0,
the currency gate's suite faults at 0, eleven cards of the workstream itself at 0 or 610).

What these tests pin down:

* the enforcement node is the COMPLETION: a child born at priority 0 under a higher-priority
  parent is lifted to the parent's own priority in the SAME transaction as the closing write,
  and the promotion is recorded on the child and on the ``completed`` event;
* the lift is a FLOOR, never a re-rank: a child already at or above the parent is untouched,
  a child is never moved above its parent, and a terminal child is never touched (the
  standard must not inflate ordinary work). The floor is applied against the parent's
  RESOLVED priority, not the one it was planted with: the gate ladder (t_ef547958) has
  already lifted the parent to any card it gates, AT LINK TIME, before the completion runs;
* the reserved tranche is never inherited: a DESIGNATED parent lifts its children to the top
  of the ORDINARY domain, never into the tranche — an inherited tranche value carries no
  designation row, which a wired board's storage guard aborts, taking the completion with it;
* a deferral is the only way a child is held out, and it is EXPLICIT and RECORDED — named on
  the closing card as ``{child: why}``, refused when it names a card that is not a child of
  the closing card, refused when it states no reason, and never inferred from a low priority;
* the lift rides the completion's own write txn, so a failure while recording it rolls the
  completion back rather than leaving a closed parent over an unpromoted child.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB (no live board is touched)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "platform-worker")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # A dispatcher-spawned worker shell pins these; a test must not touch a live board.
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _plant(conn, *, parent_priority: int = 700000, child_priority: int = 0,
           children: int = 1, sibling_priority: int = 0) -> tuple:
    """A closing card plus N children born at ``child_priority`` (the measured failure mode).

    Returns ``(parent, [children], unrelated)``; the unrelated card proves the floor is
    scoped to the completing card's own tree.
    """
    from hermes_cli import kanban_db as kb

    parent = kb.create_task(conn, title="umbrella closed as a phase", priority=parent_priority)
    kids = [
        kb.create_task(conn, title=f"carried DoD {n}", priority=child_priority,
                       parents=[parent])
        for n in range(1, children + 1)
    ]
    unrelated = kb.create_task(conn, title="ordinary work", priority=sibling_priority)
    return parent, kids, unrelated


def _priority(conn, task_id: str) -> int:
    return conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()["priority"]


def _status(conn, task_id: str) -> str:
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]


def _events(conn, task_id: str, kind: str) -> list:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"] or "{}") for row in rows]


def _completed_event(conn, task_id: str) -> dict:
    events = _events(conn, task_id, "completed")
    assert events, f"{task_id} has no completed event"
    return events[-1]


# ---------------------------------------------------------------------------
# The planted case: a child born at 0 is lifted at the parent's completion
# ---------------------------------------------------------------------------


def test_a_child_born_below_the_parent_is_lifted_when_the_parent_completes(
        kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        parent, (child,), unrelated = _plant(conn)
        assert _priority(conn, child) == 0, "the plant must start at the measured failure"

        assert kb.complete_task(conn, parent, result="closed as a phase; DoD carried by children")

        assert _priority(conn, child) == _priority(conn, parent) == 700000
        assert _priority(conn, unrelated) == 0, "the floor is scoped to the closing card's tree"

        # Recorded on the CHILD, so a reader of the child can see why it moved.
        lifted = _events(conn, child, kb.PRIORITY_PROMOTED_EVENT)
        assert len(lifted) == 1
        assert lifted[0]["parent"] == parent
        assert (lifted[0]["from"], lifted[0]["to"]) == (0, 700000)

        # ...and on the parent's completion, so the closure names what it lifted.
        assert _completed_event(conn, parent)["priority_promotions"] == [
            {"child": child, "from": 0, "to": 700000},
        ]


def test_every_child_of_the_closing_card_is_lifted(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        parent, kids, _ = _plant(conn, children=3)
        assert kb.complete_task(conn, parent, summary="phase closed")
        for child in kids:
            assert _priority(conn, child) == _priority(conn, parent)
        promoted = _completed_event(conn, parent)["priority_promotions"]
        # Id order: the moves are listed deterministically, not in whatever order the links
        # happen to come back in.
        assert [move["child"] for move in promoted] == sorted(kids)


def test_the_lift_stops_at_the_parent_and_leaves_a_higher_child_alone(kanban_home: Path) -> None:
    """A floor, not a re-rank: no child is moved above its parent, none is moved down.

    The ladder resolves this test, so the numbers are the LANDED ones, not the pre-ladder ones.
    Filing ``above`` at 800000 through ``create_task(..., parents=[parent])`` routes into
    ``_link``, which lifts the GATING card to the card it gates AT LINK TIME (t_ef547958): the
    umbrella is raised 700000 -> 800000 the moment ``above`` is filed under it, before any
    completion runs. The completion floor then resolves against the umbrella's RESOLVED
    priority -- 800000, not the 700000 it was planted with -- so an assertion of 700000 here
    reads a correct floor move as a regression. ``equal`` is filed at 700000 under an umbrella
    that is already 800000, so it is a below-floor child at completion and is lifted to the
    umbrella's resolved priority too; nothing is moved DOWN, and nothing above the umbrella.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        parent, kids, _ = _plant(conn, parent_priority=700000, children=1)

        # The ladder, at link time: filing a card above the umbrella lifts the umbrella to it.
        above = kb.create_task(conn, title="filed above the umbrella", priority=800000,
                               parents=[parent])
        assert _priority(conn, parent) == 800000, \
            "the ladder lifts the gating card to the child it gates, at link time"
        assert _priority(conn, above) == 800000
        equal = kb.create_task(conn, title="already at the umbrella", priority=700000,
                               parents=[parent])
        # The link does not lower the umbrella to meet this child, so `equal` is below the floor.
        assert _priority(conn, parent) == 800000
        assert _priority(conn, equal) == 700000

        assert kb.complete_task(conn, parent, result="closed")

        assert _priority(conn, parent) == 800000, "the umbrella's resolved priority"
        # The floor lifts every below-floor child TO the umbrella's resolved priority...
        assert _priority(conn, kids[0]) == 800000
        assert _priority(conn, equal) == 800000, "below-floor child, lifted to the parent"
        # ...and never below or above it: `above` sits exactly at the floor already.
        assert _priority(conn, above) == 800000, "never moved down"
        assert _events(conn, above, kb.PRIORITY_PROMOTED_EVENT) == []
        assert _events(conn, equal, kb.PRIORITY_PROMOTED_EVENT) == [
            {"parent": parent, "parent_priority": 800000, "from": 700000, "to": 800000},
        ]
        assert [move["child"] for move in _completed_event(conn, parent)["priority_promotions"]] \
            == sorted([kids[0], equal])
        assert kb.gate_violations(conn) == []


def test_a_terminal_child_is_left_alone(kanban_home: Path) -> None:
    """Only a child still carrying the remaining DoD moves; closed work is not re-ranked."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        parent, (child,), _ = _plant(conn)
        delivered = kb.create_task(conn, title="already delivered", priority=0)
        assert kb.complete_task(conn, delivered, result="delivered before the umbrella closed")
        kb.link_tasks(conn, parent, delivered)

        assert kb.complete_task(conn, parent, result="closed")

        assert _status(conn, delivered) == "done"
        assert _priority(conn, delivered) == 0
        assert _events(conn, delivered, kb.PRIORITY_PROMOTED_EVENT) == []
        assert _priority(conn, child) == 700000


def test_a_card_with_no_children_carries_no_promotion_record(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        lone = kb.create_task(conn, title="no children", priority=700000)
        assert kb.complete_task(conn, lone, result="closed")
        payload = _completed_event(conn, lone)
        assert "priority_promotions" not in payload
        assert "priority_deferrals" not in payload


# ---------------------------------------------------------------------------
# The reserved tranche is designated, never inherited
# ---------------------------------------------------------------------------


def test_a_designated_parent_lifts_children_to_the_ordinary_ceiling(kanban_home: Path) -> None:
    """990000 is a DESIGNATION, not a priority to inherit.

    An inherited tranche value carries no designation row; on a wired board the storage
    guard ABORTS that write, so inheriting would take the completion itself down. The child
    of a P0 umbrella lands at the top of the ordinary domain instead.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_priority_policy as policy

    with kbc.connect() as conn:
        parent, (child,), _ = _plant(conn)
        kb.designate_priority(conn, parent, reason="operator P0 (this very card's class)")
        assert _priority(conn, parent) == policy.DESIGNATED_PRIORITY

        assert kb.complete_task(conn, parent, result="closed as a phase")

        assert _priority(conn, child) == policy.ORDINARY_MAX
        assert _priority(conn, child) < _priority(conn, parent)
        lifted = _events(conn, child, kb.PRIORITY_PROMOTED_EVENT)[0]
        assert lifted["parent_priority"] == policy.DESIGNATED_PRIORITY
        assert lifted["to"] == policy.ORDINARY_MAX
        assert lifted["capped_at"] == policy.ORDINARY_MAX
        # The child is NOT designated: the ledger owns the tranche, not the floor.
        assert not kb.is_priority_designated(conn, child)


# ---------------------------------------------------------------------------
# The explicit, recorded deferral
# ---------------------------------------------------------------------------


def test_an_explicit_deferral_holds_a_child_out_and_is_recorded(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        parent, kids, _ = _plant(conn, children=2)
        deferred, carried = kids

        assert kb.complete_task(
            conn, parent, result="closed as a phase",
            deferred_children={deferred: "separate workstream; not part of this DoD"},
        )

        assert _priority(conn, deferred) == 0, "a declared deferral does not move"
        assert _priority(conn, carried) == 700000

        # The declaration is recorded on the child it holds out...
        held = _events(conn, deferred, kb.PRIORITY_DEFERRED_EVENT)
        assert held == [{"parent": parent, "reason": "separate workstream; not part of this DoD"}]
        assert _events(conn, deferred, kb.PRIORITY_PROMOTED_EVENT) == []

        # ...and on the completion, so the closure answers "why is that one still at 0?".
        payload = _completed_event(conn, parent)
        assert payload["priority_deferrals"] == [
            {"child": deferred, "reason": "separate workstream; not part of this DoD"},
        ]
        assert [move["child"] for move in payload["priority_promotions"]] == [carried]


def test_a_deferral_of_a_card_that_is_not_a_child_is_refused(kanban_home: Path) -> None:
    """An exemption must name this card's own child — never a card it does not carry."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        parent, (child,), unrelated = _plant(conn)

        refusal = kb.complete_task(
            conn, parent, result="closed",
            deferred_children={unrelated: "someone else's card"},
        )

        assert isinstance(refusal, kb.CompletionRefusal)
        assert refusal.cause == "deferral_not_a_child"
        assert unrelated in refusal.detail
        # Nothing changed: the refusal happens before the closing write.
        assert _status(conn, parent) == "ready"
        assert _priority(conn, child) == 0
        assert _events(conn, parent, "completed") == []
        assert _events(conn, child, kb.PRIORITY_PROMOTED_EVENT) == []


def test_a_deferral_without_a_reason_is_refused(kanban_home: Path) -> None:
    """A low priority is not intent: the exemption has to say why."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        parent, (child,), _ = _plant(conn)

        refusal = kb.complete_task(conn, parent, result="closed",
                                   deferred_children={child: "   "})

        assert isinstance(refusal, kb.CompletionRefusal)
        assert refusal.cause == "deferral_needs_reason"
        assert _status(conn, parent) == "ready"
        assert _priority(conn, child) == 0


def test_the_tool_layer_refuses_a_bare_id_list_as_a_deferral(kanban_home: Path) -> None:
    """``kanban_complete(deferred_children=[...])`` is not a declaration: it states no why."""
    from tools.kanban_tools import _coerce_reason_map

    assert _coerce_reason_map(None, "deferred_children") is None
    assert _coerce_reason_map({"t_abc": " delivery deferred to the next phase "},
                              "deferred_children") == {"t_abc": "delivery deferred to the next phase"}
    with pytest.raises(Exception):
        _coerce_reason_map(["t_abc"], "deferred_children")
    with pytest.raises(Exception):
        _coerce_reason_map({"t_abc": ""}, "deferred_children")


# ---------------------------------------------------------------------------
# Same transaction as the completion
# ---------------------------------------------------------------------------


def test_the_lift_rides_the_completion_transaction(kanban_home: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure while recording the lift rolls the whole completion back."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        parent, (child,), _ = _plant(conn)
        real = kb._append_event

        def boom(*args, **kwargs):
            kind = kwargs.get("kind", args[2] if len(args) > 2 else None)
            if kind == kb.PRIORITY_PROMOTED_EVENT:
                raise RuntimeError("simulated failure while recording the lift")
            return real(*args, **kwargs)

        monkeypatch.setattr(kb, "_append_event", boom)
        with pytest.raises(RuntimeError):
            kb.complete_task(conn, parent, result="closed as a phase")
        monkeypatch.undo()

        assert _status(conn, parent) == "ready", "the closing write rolled back with the lift"
        assert _priority(conn, child) == 0
        assert _events(conn, parent, "completed") == []
