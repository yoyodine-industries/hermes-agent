"""A triage card that carries a human design spec must not reach the decomposer.

The fan-out is the ONE destructive door out of ``triage``: ``decompose_task``
hands the card's title/body to an auxiliary LLM and rewrites the root into
children, so a frozen design spec consumed that way is not recoverable from the
card — the body the designer wrote is gone and the children carry the model's
paraphrase of it.

That is what happened on card t_f5df548b (board yoyoflow): an 8-step design spec
authored by ``platform-stl`` (assignee ``platform-coder``, 19 spec comments) whose
worker blocked on a context wall; the transient retry budget ran out
(``block_kind=transient``, ``block_recurrences=2``) and the card was moved to
``triage``, where the only supported exit was ``specify`` — an LLM specifier that
would have rewritten the frozen spec — or the decomposer.

These tests pin the guard, its narrow scope, and what is left for a card it
refuses.  ``spec-carrying`` is deliberately two narrow mechanical clauses so that
``triage`` keeps working for genuinely under-specified work (its purpose):

  * an explicit FROZEN marker in the title/body/comments -- the deliberate,
    reassignment-proof form; or
  * a comment authored by an identity OTHER than the assignee, i.e. a designer or
    another lane has already engaged with the card.

Measured on the seven live board stores (read-only, 2026-09-21): 61 cards have
ever been in ``triage``; 44 of them (72%) carry the comment clause, 42 with a
human/lane author and only 2 whose sole foreign commenter is the disposition
sweep's own ``kanban-disposition`` identity -- so the clause is not a proxy for
"a machine touched this card".  Both refusals are the safe direction: refusing
costs a manual ``promote``/``specify``, while shredding a spec is unrecoverable.
"""

from __future__ import annotations

import argparse

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp
from hermes_cli.kanban_db_graph import decompose_triage_task

CHILDREN = [{"title": "do the thing", "body": "spec", "assignee": "cc-nova", "parents": []}]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _specd_card(conn, *, title="frozen 8-step design spec", body="step 1: ...", comments=True):
    """The measured t_f5df548b shape: a spec authored by someone else."""
    tid = kb.create_task(conn, title=title, body=body, assignee="cc-nova", triage=True)
    if comments:
        kb.add_comment(conn, tid, "platform-stl", "RULE 1: the slot pool is per-tenant.")
    return tid


def _children_of(conn):
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] - 1


def test_decompose_refuses_a_card_another_identity_has_specified(conn):
    tid = _specd_card(conn)

    assert decompose_triage_task(conn, tid, root_assignee="orch", children=CHILDREN) is None

    assert kb.get_task(conn, tid).status == "triage", "a refused spec card stays where it was"
    assert _children_of(conn) == 0, "nothing may be spawned from a card we refused"
    refusals = [e for e in kb.list_events(conn, tid) if e.kind == "decompose_refused"]
    assert len(refusals) == 1
    assert "platform-stl" in refusals[0].payload["reason"]


def test_decompose_refuses_an_explicit_frozen_marker(conn):
    # No foreign comment at all: the deliberate marker is the whole signal, which
    # is the form that survives a later reassignment of the card.
    tid = _specd_card(conn, title="FROZEN: pool redesign", body="8 steps, do not paraphrase",
                      comments=False)

    assert decompose_triage_task(conn, tid, root_assignee="orch", children=CHILDREN) is None
    assert kb.get_task(conn, tid).status == "triage"
    assert _children_of(conn) == 0


def test_decompose_still_fans_out_an_unengaged_rough_idea(conn):
    # The regression half: triage exists to shred exactly this, and the guard
    # must not disable it.
    tid = kb.create_task(conn, title="rough idea", body="maybe we need a widget",
                         assignee="cc-nova", triage=True)

    child_ids = decompose_triage_task(conn, tid, root_assignee="orch", children=CHILDREN)

    assert child_ids is not None
    assert kb.get_task(conn, tid).status == "todo"
    assert _children_of(conn) == 1


def test_the_refusal_is_decided_before_any_aux_llm_call(kanban_home, monkeypatch):
    """The guard must not pay for a decomposition it is going to throw away."""
    def _boom(*a, **kw):
        raise AssertionError("the decomposer spent an aux LLM call on a spec card")

    monkeypatch.setattr(decomp, "_call_aux", _boom)
    with kbc.connect_closing() as conn:
        tid = _specd_card(conn)

    outcome = decomp.decompose_task(tid)

    assert outcome.ok is False
    assert "spec" in outcome.reason.lower()
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_a_refused_spec_card_keeps_a_supported_exit(kanban_home):
    """A refusal must not re-create the trap: the card is still releasable."""
    with kbc.connect_closing() as conn:
        tid = _specd_card(conn)

    ns = argparse.Namespace(
        task_id=tid, reason=[], ids=None, dry_run=False, json=False, force=False
    )
    assert kb_cli._cmd_promote(ns) == 0

    with kbc.connect_closing() as conn:
        row = kb.get_task(conn, tid)
        assert row.status == "ready"
        assert row.assignee == "cc-nova"


def test_transient_exhaustion_parks_a_spec_card_instead_of_the_spec_shelf(conn):
    """The measured route: this is how t_f5df548b reached ``triage``.

    The card was ordinary work that a designer had specified -- the spec shelf is
    reached from the outside, never by creating the card there.
    """
    tid = kb.create_task(conn, title="frozen 8-step design spec", body="step 1: ...",
                         assignee="cc-nova")
    kb.add_comment(conn, tid, "platform-stl", "RULE 1: the slot pool is per-tenant.")
    assert kb.claim_task(conn, tid) is not None
    assert kb.block_task(conn, tid, reason="context wall", kind="transient")
    assert kb.unblock_task(conn, tid)
    assert kb.claim_task(conn, tid) is not None
    assert kb.block_task(conn, tid, reason="context wall", kind="transient")

    row = kb.get_task(conn, tid)
    assert row.status == "blocked", "transient exhaustion parks; it never reaches triage"
    assert row.block_kind == "transient"
    assert row.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    assert kb.board_stats(conn)["by_status"].get("triage", 0) == 0
    trips = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"]
    assert len(trips) == 1
    assert trips[0].payload["kind"] == "transient"
