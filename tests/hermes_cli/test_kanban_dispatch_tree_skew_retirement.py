"""The tree-skew ACTOR-SWEEP door on its own: the declaration it sends, and its bound.

Card ``t_028ad743``, review round 1 (``platform-stl``, D1). The retirement of a CLEARED tree-skew
guard is carried by two doors -- the narrow "the guard cleared" sweep and the broad actor sweep
(``_retire_tree_skew_actors``) -- and the tests that cover the NARROW door live in
``test_kanban_dispatch_tree_skew_gate.py``. They are here, in a topical sibling, because that file
is at the repo's 2000-line cap and new behaviour never goes into a facade.

Two defects lived in the broad door alone, and neither had a test at all:

1. **It declared no evidence.** ``close_kwargs={"result": ...}`` and nothing else, so with the
   evidence gate at its default ``refuse`` the kernel answered
   ``CompletionRefusal(cause='no_evidence')`` on EVERY broad-door close -- the door could never
   close anything. A card the sweep had correctly judged cleared stayed live, and (with the bound
   this card adds) was given up on permanently after three self-inflicted refusals, left in the
   spawnable shape the card's own WHY section names. Both doors now send the SAME declaration,
   built by one helper so they cannot drift apart again.
2. **The bound counted our plumbing, not the kernel.** On a tick whose board IS the actor board
   both doors see the same card, so ONE kernel refusal wrote TWO ``..._retire_refused`` events and
   ``TICK_YIELD_RETIRE_MAX_ATTEMPTS`` (3) was spent in two ticks. The tick now shares ONE
   ``attempted`` set between the doors: one attempt per card per tick.

Everything runs on a SCRATCH surface: the ticking board and the ops board are throwaway DBs under
``tmp_path`` and ``TICK_YIELD_ACTOR_BOARD`` is pointed at the scratch ops board, so the fleet's
``migrations`` board is unreachable from this module.
"""

from __future__ import annotations

from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_gate_invariants as kgi

#: The scratch board the acting card rides in these tests. ``migrations`` is the production value
#: of ``TICK_YIELD_ACTOR_BOARD``; a test must never reach it.
OPS_BOARD = "scratch-ops"


def _retirement_owner():
    """The module that OWNS the per-card close on THIS revision.

    It is the topical sibling from card t_028ad743's round 1 on; before that the function was
    still defined in the facade. Resolving it (rather than importing one name) keeps the case
    below BEHAVIOURAL on either revision when a reviewer runs it against a base clone.
    """
    try:
        from hermes_cli import kanban_db_dispatch_retire as mod
    except ImportError:
        from hermes_cli import kanban_db_dispatch as mod
    return mod


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A sandbox pair: the TICKING board, plus the scratch OPS board the actor must ride."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr(kbd, "TICK_YIELD_ACTOR_BOARD", OPS_BOARD, raising=False)
    kb.create_board(OPS_BOARD, dispatch=False)
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _card_kinds(conn, task_id):
    return [
        row["kind"] for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
    ]


def _kinds_on(board_slug, task_id):
    conn = kbc.connect(board=board_slug)
    try:
        return _card_kinds(conn, task_id)
    finally:
        conn.close()


def _status_on(board_slug, task_id):
    conn = kbc.connect(board=board_slug)
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row["status"] if row is not None else None
    finally:
        conn.close()


def test_the_BROAD_actor_sweep_closes_under_the_evidence_gate(board, monkeypatch):
    """Review D1 (1): the broad sweep must carry the SAME declaration the narrow door carries.

    Driven directly at ``_retire_tree_skew_actors`` -- no tick -- because that is exactly where the
    defect lived: the sweep passed ``result`` ALONE, so the kernel refused a card the sweep had
    correctly judged CLEARED, and the card stayed live in a spawnable status (``blocked``, walked to
    ``ready`` by the sweep's own ``unblock_task``).
    """
    monkeypatch.setenv(kgi.EVIDENCE_GATE_ENV, "refuse")
    conn = board
    ops = kbc.connect(board=OPS_BOARD)
    try:
        task_id = kb.create_task(
            ops,
            title="[guard] cleared episode, filed where the broad sweep looks",
            body="seeded by card t_028ad743's review round 2",
            assignee=kbd.TICK_YIELD_ACTOR_ASSIGNEE,
            created_by="kanban-dispatcher",
            idempotency_key=(
                f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}tree-content:{'d' * 64}:1792800954"
            ),
        )
        with kb.write_txn(ops):
            ops.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,))
    finally:
        ops.close()

    assert kbd._retire_tree_skew_actors(conn) == 1, "the sweep judged this card cleared"
    assert _status_on(OPS_BOARD, task_id) == "done", (
        "a clearance the sweep has PROVED must close under the shipped evidence gate"
    )
    kinds = _kinds_on(OPS_BOARD, task_id)
    assert kbd.TICK_YIELD_RETIRED_EVENT in kinds, kinds
    assert kbd.TICK_YIELD_RETIRE_FAILED_EVENT not in kinds, (
        f"a clearance we proved must never be given up on: {kinds}"
    )
    assert kbd.TICK_YIELD_RETIRE_REFUSED_EVENT not in kinds, (
        f"the kernel refused a clearance we proved -- the close declared no evidence: {kinds}"
    )
    assert "evidence_none" in kinds, (
        f"the close must DECLARE its class on the card, never lean on a gate waiver: {kinds}"
    )


def test_the_give_up_bound_counts_the_KERNEL_refusals_not_the_two_doors(board, monkeypatch):
    """Review D1 (3): ONE attempt per card per TICK, whichever door reaches it.

    On a tick whose board IS the actor board, BOTH doors see the same card. They used to write TWO
    ``..._retire_refused`` events for ONE kernel refusal, so ``TICK_YIELD_RETIRE_MAX_ATTEMPTS`` (3)
    was spent in two ticks and a guard was given up on before it had been tried three times. Driven
    through the real entry ``_retire_cleared_tree_skew_cards``: both doors are shown to run, and the
    bound is shown to count TICKS.
    """
    # The door lives in the topical sibling from this card's round 1 on; before that it was still
    # defined in the facade. Patch the module that OWNS it, so the count below is behavioural on
    # either revision rather than a signature red.
    owner: Any = _retirement_owner()
    monkeypatch.setattr(kbd, "TICK_YIELD_ACTOR_BOARD", "migrations", raising=False)
    kb.create_board("migrations", dispatch=False)
    calls: list = []
    real = owner._retire_one_tree_skew_actor

    def _spy(conn, card_id, **kwargs):
        calls.append(kwargs.get("log_what"))
        return real(conn, card_id, **kwargs)

    monkeypatch.setattr(owner, "_retire_one_tree_skew_actor", _spy)

    actor = kbc.connect(board="migrations")
    try:
        parent_id = kb.create_task(
            actor,
            title="[guard] an unfinished parent (keeps the guard out of ready)",
            body="seeded by card t_028ad743's review round 2",
            created_by="kanban-dispatcher",
        )
        with kb.write_txn(actor):
            actor.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent_id,))
        task_id = kb.create_task(
            actor,
            title="[guard] a shape the kernel will not take",
            body="seeded by card t_028ad743's review round 2",
            assignee=kbd.TICK_YIELD_ACTOR_ASSIGNEE,
            created_by="kanban-dispatcher",
            parents=(parent_id,),
            idempotency_key=(
                f"{kbd.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}tree-content:{'e' * 64}:1792800954"
            ),
        )
        assert not [
            kind for kind in _card_kinds(actor, task_id)
            if kind.startswith("tree_skew_actor_retire")
        ], "the guard starts with no retirement history"

        kbd._retire_cleared_tree_skew_cards(actor, board="migrations")
        assert len(calls) == 2, (
            "BOTH doors reach this card on an actor-board tick (narrow, then the broad sweep); "
            f"got {calls}"
        )
        kinds = _card_kinds(actor, task_id)
        assert kinds.count(kbd.TICK_YIELD_RETIRE_REFUSED_EVENT) == 1, (
            f"ONE kernel refusal must be counted ONCE, not once per door: {kinds}"
        )

        for _ in range(kbd.TICK_YIELD_RETIRE_MAX_ATTEMPTS - 1):
            kbd._retire_cleared_tree_skew_cards(actor, board="migrations")
        kinds = _card_kinds(actor, task_id)
        assert kinds.count(kbd.TICK_YIELD_RETIRE_REFUSED_EVENT) == (
            kbd.TICK_YIELD_RETIRE_MAX_ATTEMPTS - 1
        ), f"the bound counts TICKS (one attempt per tick): {kinds}"
        assert kinds.count(kbd.TICK_YIELD_RETIRE_FAILED_EVENT) == 1, kinds

        # ... and a given-up card is left alone: the next tick adds nothing at all.
        before = kinds
        kbd._retire_cleared_tree_skew_cards(actor, board="migrations")
        assert _card_kinds(actor, task_id) == before, "a given-up card must leave every sweep"
    finally:
        actor.close()
