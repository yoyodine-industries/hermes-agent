"""The declared-gate EDGE gate: prose that names a gate must be an edge on the board.

Contract under test (ruling, card ``t_26d2f957``):

* a card whose LIVE text DECLARES it waits on another card -- the predicate form
  ("gated on ``t_x``") or the subject form ("the consent card ``t_x`` is still
  blocked") -- is refused the RUN while that card is unfinished and is not an
  ancestor, and parked ``blocked``/``needs_input`` -- parked, never silently
  skipped;
* the hold is ONE deduped event and ONE comment per episode, so a per-tick guard
  cannot bury the board;
* a bare mention of a card id, the noun class ("gate PRs"), a negated claim, a
  sentence about a SUPERSEDED card, a claim on a FINISHED card, and a claim on a
  card that IS an ancestor are all NOT refusals;
* a LANDING-scope claim does not hold a spawn: the measured instance's build
  carrier says "BUILD AND PR NOW. DO NOT MERGE OR DEPLOY", and holding it would
  freeze the work the ruling ordered -- the false-negative mirror of the defect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_gate_edge as ge


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _card(conn, title, body="", *, parents=(), status=None):
    tid = kb.create_task(conn, title=title, body=body, assignee="platform-worker",
                         parents=parents)
    if status:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        conn.commit()
    return tid


# --- the clauses: what IS a claim --------------------------------------------

@pytest.mark.parametrize("text,want", [
    # the measured instance, verbatim shapes from the three carriers
    ("The operator consent card t_a1e53cdd is still blocked; landing is gated on its answer.",
     "landing"),
    ("Landing gated with the rest (operator consent t_a1e53cdd).", "landing"),
    ("**Operator-gated**: t_a1e53cdd (consent) is still blocked -- do not apply "
     "before its answer.", "start"),
    # the general predicate class (this gate is not consent-specific)
    ("Sequenced after t_11111111; do not start this leg before it lands.", "start"),
    ("Blocked until t_22222222 closes the store.", "start"),
    ("Waiting on card t_33333333.", "start"),
])
def test_claim_and_scope(text, want):
    found = ge.claims(text)
    assert len(found) == 1, found
    assert found[0].card_id.startswith("t_")
    assert found[0].scope == want, (found[0].evidence, found[0].scope)


@pytest.mark.parametrize("text", [
    # a bare cross-reference is not a claim: the 1,441-hit class
    "See t_44444444 for the ruling; the window in t_55555555 decides the rest.",
    # the noun class the loose trigger wrecked itself on
    "Dispose the 3 open gate PRs and the gate.py merge-refusal wording.",
    "GATE-TOP-LEVEL-ONLY: a dispatched worker cannot make this move.",
    # negated claims release
    "This is NOT gated on t_66666666 any more.",
    "No longer blocked on t_77777777.",
    # a sentence about a superseded card states THAT card's gate
    "Supersedes t_88888888, which was filed with a parent edge that gated it "
    "behind t_99999999 -- do THIS card, ignore that one.",
])
def test_not_a_claim(text):
    assert ge.claims(text) == ()


def test_self_reference_is_not_a_gate():
    assert ge.claims("This card (t_abcdef01) is gated on t_abcdef01.", own_id="t_abcdef01") == ()


# --- the verdict: edge present or absent --------------------------------------

def test_unbacked_start_claim_is_refused(home):
    with kbc.connect() as conn:
        gate = _card(conn, "operator consent", status="blocked")
        tid = _card(conn, "cfg: apply the retention change",
                    f"Operator-gated: {gate} (consent) is still blocked -- do not apply "
                    f"before its answer.", status="ready")
        v = ge.evaluate(conn, tid)
        assert v.refused and v.cause == "gate_edge_missing"
        assert [c.card_id for c in v.unbacked_start] == [gate]

        # the edge is the fix, and it clears the verdict
        kb.link_tasks(conn, parent_id=gate, child_id=tid)
        conn.commit()
        v2 = ge.evaluate(conn, tid)
        assert not v2.refused and not v2.reported


def test_finished_named_card_is_satisfied(home):
    with kbc.connect() as conn:
        gate = _card(conn, "operator consent", status="done")
        tid = _card(conn, "apply the retention change",
                    f"gated on {gate}; do not run before it is answered.", status="ready")
        v = ge.evaluate(conn, tid)
        assert not v.refused and gate in v.finished


def test_landing_scope_does_not_hold_a_spawn(home):
    """The measured over-correction: this card must still build."""
    with kbc.connect() as conn:
        gate = _card(conn, "operator consent", status="blocked")
        tid = _card(conn, "fix(state-retention): widen source scope",
                    f"BUILD AND PR NOW. DO NOT MERGE OR DEPLOY. The operator consent "
                    f"card {gate} is still blocked; landing is gated on its answer.",
                    status="ready")
        v = ge.evaluate(conn, tid)
        assert v.reported and not v.refused
        assert [c.scope for c in v.unbacked] == ["landing"]


def test_unknown_named_card_is_prose_not_a_gate(home):
    with kbc.connect() as conn:
        tid = _card(conn, "x", "gated on t_deadbeef which is not on this board.",
                    status="ready")
        v = ge.evaluate(conn, tid)
        assert not v.reported


def test_audit_orders_start_before_landing(home):
    with kbc.connect() as conn:
        gate = _card(conn, "operator consent", status="blocked")
        start = _card(conn, "apply config",
                      f"do not apply before {gate} answers.", status="ready")
        land = _card(conn, "build leg",
                     f"do not merge or deploy before {gate} answers.", status="ready")
        rows = ge.audit(conn)
        assert [r.task_id for r in rows] == [start, land]
        assert rows[0].refused and not rows[1].refused


# --- the RUN door -------------------------------------------------------------

def test_run_door_parks_once_and_writes_one_comment(home):
    with kbc.connect() as conn:
        gate = _card(conn, "operator consent", status="blocked")
        tid = _card(conn, "cfg: apply retention",
                    f"**Operator-gated**: {gate} (consent) is still blocked -- do not "
                    f"apply before its answer.", status="ready")

        assert kbd._hold_declared_gate(conn, tid) is True
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?",
                           (tid,)).fetchone()
        assert row["status"] == "blocked" and row["block_kind"] == "needs_input"
        events = conn.execute(
            "SELECT count(*) AS n FROM task_events WHERE task_id = ? "
            "AND kind = 'gate_edge_refused'", (tid,)).fetchone()["n"]
        comments = conn.execute(
            "SELECT count(*) AS n FROM task_comments WHERE task_id = ?", (tid,)).fetchone()["n"]
        assert events == 1 and comments == 1
        # already parked: the guard does not re-refuse a card it has held
        assert kbd._hold_declared_gate(conn, tid) is False


def test_run_door_dry_run_writes_nothing(home):
    with kbc.connect() as conn:
        gate = _card(conn, "operator consent", status="blocked")
        tid = _card(conn, "cfg: apply retention",
                    f"do not apply before {gate} answers.", status="ready")
        assert kbd._hold_declared_gate(conn, tid, dry_run=True) is True
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["status"] == "ready"
        n = conn.execute("SELECT count(*) AS n FROM task_events WHERE task_id = ? "
                         "AND kind = 'gate_edge_refused'", (tid,)).fetchone()["n"]
        assert n == 0


def test_run_door_leaves_a_landing_scope_card_alone(home):
    with kbc.connect() as conn:
        gate = _card(conn, "operator consent", status="blocked")
        tid = _card(conn, "build the leg",
                    f"BUILD NOW. DO NOT MERGE OR DEPLOY. landing is gated on {gate}.",
                    status="ready")
        assert kbd._hold_declared_gate(conn, tid) is False
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["status"] == "ready"


def test_run_door_holds_nothing_without_a_claim(home):
    with kbc.connect() as conn:
        tid = _card(conn, "Rotate the bloom filters",
                    "see t_12345678 for background only.", status="ready")
        assert kbd._hold_declared_gate(conn, tid) is False
