"""The tranche ORDER IS DECLARED, not inherited in silence (card t_a1890b04).

Ruling: platform-stl 2026-11-08, item 2, recorded on ``t_ceb583fb`` — "the tie-break is
already decided and nobody declared it". The measured defect was that
``dispatch --dry-run`` printed a spawn list whose order came from a query clause nobody had
declared: every card in the reserved tranche ties at the floor, so "designated" silently meant
"oldest first", and a newly designated SEV1 landed LAST among its equals.

Three behaviour contracts, and NOT a fourth (the card forbids re-ordering):

1. the order in force is PRINTED wherever the tranche is dispatched or listed — the clause AND
   its tie-break, in the output;
2. the tie DEPTH is printed per tranche value, because a tie of 2 and a tie of 173 are
   different operational facts;
3. the printed line is READ from the same object the query is built from, so a policy
   tie-break chosen later moves the query and the sentence together — a print can never claim
   an order the dispatcher does not run.
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


def _place(conn, task_id: str, priority: int, *, created_at: int | None = None) -> None:
    """Put a card ON a tranche value directly — the guard is not what this file tests."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET priority = ? WHERE id = ?", (int(priority), task_id)
        )
        if created_at is not None:
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?", (int(created_at), task_id)
            )


def _dispatch_args(**overrides) -> Namespace:
    values = dict(json=False, dry_run=True, max=None, failure_limit=3)
    values.update(overrides)
    return Namespace(**values)


# ---------------------------------------------------------------------------
# 1. The declaration cannot disagree with the query it declares
# ---------------------------------------------------------------------------


def test_declaration_is_built_from_the_query_it_declares():
    decl = kb.priority_order_declaration()
    order_sql, _params = kb.effective_priority_order_sql()
    # The exact bytes the ORDER BY uses, built by the same expression producer.
    assert decl["order_sql"] == order_sql
    assert decl["tie_sql"] in order_sql
    # ...and the sentence is honest about who chose that tail.
    assert "tie-break: OLDEST FIRST" in decl["line"]
    assert "undeclared by policy" in decl["line"]
    assert decl["tie_declared"] is False


def test_a_policy_tie_break_moves_the_query_and_the_print_together(monkeypatch):
    """Requirement 3: a later policy choice reaches BOTH, or neither.

    Nothing may be edited here but the object — if a print could keep saying "oldest first"
    while the query ordered newest first, the declaration would be decoration.
    """
    before_sql, _ = kb.effective_priority_order_sql()
    assert kb.PRIORITY_TIE_BREAK["sql"] in before_sql

    monkeypatch.setitem(kb.PRIORITY_TIE_BREAK, "sql", "created_at DESC")
    monkeypatch.setitem(kb.PRIORITY_TIE_BREAK, "label", "NEWEST FIRST")
    monkeypatch.setitem(kb.PRIORITY_TIE_BREAK, "declared", True)

    after_sql, _ = kb.effective_priority_order_sql()
    decl = kb.priority_order_declaration()
    assert after_sql.endswith("created_at DESC")
    assert decl["order_sql"] == after_sql
    assert "tie-break: NEWEST FIRST" in decl["line"]
    # A declared tie-break carries no "nobody chose this" qualifier.
    assert "undeclared by policy" not in decl["line"]


# ---------------------------------------------------------------------------
# 2. The tie depth is measured per tranche value, both populations
# ---------------------------------------------------------------------------


def test_tie_depth_counts_each_value_and_separates_the_dispatch_lane(
    kanban_home, all_assignees_spawnable
):
    with kbc.connect() as conn:
        ready_a = kb.create_task(conn, title="a", assignee="alice")
        ready_b = kb.create_task(conn, title="b", assignee="alice")
        held = kb.create_task(conn, title="c", assignee="alice", initial_status="blocked")
        top = kb.create_task(conn, title="d", assignee="alice")
        for tid in (ready_a, ready_b, held):
            _place(conn, tid, 990000)
        _place(conn, top, 999999)

        depth = {row["priority"]: row for row in kb.tranche_tie_depth(conn)}

    assert depth[990000]["non_terminal"] == 3
    # Two of the three are IN the lane; the third is blocked, and the two numbers say so.
    assert depth[990000]["lane"] == 2
    assert depth[999999]["non_terminal"] == 1
    assert depth[999999]["lane"] == 1


def test_lane_bracket_reads_the_dispatchers_lane_not_just_ready(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Review round 1, item 1: a REVIEW card the next tick will spawn is in the lane.

    The old bracket counted ``status = 'ready'`` only, so the one unclaimed review card at the
    floor printed as ``(0 ready)`` while the dispatcher would have spawned it. The bracket now
    reads the dispatcher's own predicate, so a ready card AND a review card at the same tranche
    value are both counted.
    """
    from hermes_cli import kanban_db_dispatch as dispatch

    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: True)
    with kbc.connect() as conn:
        ready = kb.create_task(conn, title="ready", assignee="alice")
        review = kb.create_task(conn, title="review", assignee="alice")
        claimed = kb.create_task(conn, title="claimed", assignee="alice")
        for tid in (ready, review, claimed):
            _place(conn, tid, 990000)
        with kb.write_txn(conn):
            # ready -> review: the column the dispatcher spawns from when review dispatch is on.
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))
            # A claim-locked ready card is NOT in the lane (the dispatcher skips it).
            conn.execute(
                "UPDATE tasks SET claim_lock = 'held', status = 'ready' WHERE id = ?", (claimed,)
            )
        depth = {row["priority"]: row for row in kb.tranche_tie_depth(conn)}
        lines = kb.tranche_order_lines(conn)

    entry = depth[990000]
    assert entry["non_terminal"] == 3
    assert entry["lane"] == 2, "ready + review are both spawnable; the claimed row is not"
    assert "990000: 3 tied (2 spawnable)" in lines[1]


def test_lane_bracket_follows_the_review_dispatch_switch(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """With review dispatch OFF, the same review card is not in the lane — and says so."""
    from hermes_cli import kanban_db_dispatch as dispatch

    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: False)
    with kbc.connect() as conn:
        ready = kb.create_task(conn, title="ready", assignee="alice")
        review = kb.create_task(conn, title="review", assignee="alice")
        for tid in (ready, review):
            _place(conn, tid, 990000)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))
        entry = {row["priority"]: row for row in kb.tranche_tie_depth(conn)}[990000]
        lines = kb.tranche_order_lines(conn)

    assert entry["non_terminal"] == 2
    assert entry["lane"] == 1
    assert "990000: 2 tied (1 spawnable)" in lines[1]


def test_tie_depth_line_is_printed_with_the_counts(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        tids = [kb.create_task(conn, title=f"t{i}", assignee="alice") for i in range(3)]
        for tid in tids:
            _place(conn, tid, 990000)
        lines = kb.tranche_order_lines(conn)

    assert lines[0].startswith("Tranche order: order=")
    assert "990000: 3 tied (3 spawnable)" in lines[1]


def test_tie_depth_line_is_measured_even_when_no_card_is_spawnable(
    kanban_home, all_assignees_spawnable
):
    """A full lane of 0 is still a printed fact, not an omission."""
    with kbc.connect() as conn:
        blocked = kb.create_task(conn, title="blocked", assignee="alice", initial_status="blocked")
        _place(conn, blocked, 990000)
        lines = kb.tranche_order_lines(conn)

    assert "990000: 1 tied (0 spawnable)" in lines[1]


def test_no_tranche_card_says_so_rather_than_printing_an_empty_list(kanban_home):
    with kbc.connect() as conn:
        kb.create_task(conn, title="ordinary", assignee="alice")
        lines = kb.tranche_order_lines(conn)
    assert lines[0].startswith("Tranche order: order=")
    assert "no card sits in" in lines[1]


# ---------------------------------------------------------------------------
# 3. Wherever the tranche is DISPATCHED, the order is declared — in the output
# ---------------------------------------------------------------------------


def test_dispatch_dry_run_prints_the_declaration_before_the_spawn_list(
    kanban_home, all_assignees_spawnable, capsys
):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="designated", assignee="alice")
        _place(conn, tid, 990000)

    assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()

    assert lines[0].startswith("Tranche order: order=")
    assert "tie-break: OLDEST FIRST — undeclared by policy, inherited from the query" in lines[0]
    assert "990000: 1 tied (1 spawnable)" in lines[1]
    # The list it declares must come AFTER the declaration, or the reader believes the list
    # chose its own order.
    assert any(line.startswith("Reclaimed:") for line in lines)
    assert lines.index(next(l for l in lines if l.startswith("Reclaimed:"))) > 1
    # The card this tranche's tie decides is the one that got listed.
    assert f"- {tid} " in out


def test_dispatch_json_carries_the_same_declaration(kanban_home, all_assignees_spawnable, capsys):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="designated", assignee="alice")
        _place(conn, tid, 990000)

    assert kanban_ops._cmd_dispatch(_dispatch_args(json=True)) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["tranche_order"][0].startswith("Tranche order: order=")
    assert "990000: 1 tied (1 spawnable)" in payload["tranche_order"][1]


# ---------------------------------------------------------------------------
# 4. Wherever the tranche is LISTED, the order is declared — and only then
# ---------------------------------------------------------------------------


def _list_args(**overrides) -> Namespace:
    values = dict(
        assignee=None, mine=False, status=None, tenant=None, session=None, archived=False,
        sort=None, workflow_template_id=None, current_step_key=None, json=False, board=None,
    )
    values.update(overrides)
    return Namespace(**values)


def test_listing_that_shows_the_tranche_declares_its_order(
    kanban_home, all_assignees_spawnable, capsys
):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="designated", assignee="alice")
        _place(conn, tid, 990000)

    from hermes_cli import kanban as kanban_cli

    assert kanban_cli._cmd_list(_list_args()) == 0
    out = capsys.readouterr().out
    assert "Tranche order: order=" in out
    assert "990000: 1 tied (1 spawnable)" in out
    assert tid in out


def test_listing_overridden_by_sort_declares_the_sort_in_force(
    kanban_home, all_assignees_spawnable, capsys
):
    """Review round 1, item 2: with ``--sort`` the list does NOT run the default order.

    The declaration must not describe a query the listing never executed; it now carries the
    sort actually in force, from the same ``VALID_SORT_ORDERS`` mapping ``list_tasks`` uses.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="designated", assignee="alice")
        _place(conn, tid, 990000)

    from hermes_cli import kanban as kanban_cli

    assert kanban_cli._cmd_list(_list_args(sort="priority")) == 0
    lines = capsys.readouterr().out.splitlines()

    assert lines[0].startswith("Tranche order: order="), "the dispatcher's order still declared"
    sort_line = next(line for line in lines if line.startswith("Tranche listing order:"))
    assert "--sort priority = priority DESC, created_at ASC" in sort_line
    assert "not in the dispatcher's order" in sort_line
    # The two claims are different on purpose: the sort SQL has no age bonus.
    assert "priority + MIN(" not in sort_line
    # ...and the tie depth is still printed after it.
    assert any("990000: 1 tied (1 spawnable)" in line for line in lines)


def test_listing_without_sort_declares_no_listing_order(
    kanban_home, all_assignees_spawnable, capsys
):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="designated", assignee="alice")
        _place(conn, tid, 990000)

    from hermes_cli import kanban as kanban_cli

    assert kanban_cli._cmd_list(_list_args()) == 0
    assert "Tranche listing order:" not in capsys.readouterr().out


def test_machine_listing_keeps_its_array_shape(kanban_home, all_assignees_spawnable, capsys):
    """Review round 1, item 3, decided: machine LISTINGS stay out of scope.

    ``ls --json`` is an array of tasks and every consumer depends on that shape; wrapping it to
    add a top-level ``tranche_order`` would break them. The machine surface that carries the
    declaration is ``dispatch --json`` (tested above). This pins the decision so it cannot be
    silently reversed in one direction or the other.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="designated", assignee="alice")
        _place(conn, tid, 990000)

    from hermes_cli import kanban as kanban_cli

    assert kanban_cli._cmd_list(_list_args(json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload and payload[0]["id"] == tid


def test_ordinary_listing_stays_quiet(kanban_home, capsys):
    with kbc.connect() as conn:
        kb.create_task(conn, title="ordinary", assignee="alice")

    from hermes_cli import kanban as kanban_cli

    assert kanban_cli._cmd_list(_list_args()) == 0
    out = capsys.readouterr().out
    assert "Tranche order:" not in out
    assert "ordinary" in out
