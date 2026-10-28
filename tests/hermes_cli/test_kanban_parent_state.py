"""The link door names its parents' PROMOTION state: will anything make them finish?

A link gates a child behind a parent, but a gated child only runs once the parent FINISHES.
A parent parked in ``review``/``triage``/``blocked``, scheduled for a clock nobody owns, or
assigned to a profile that no longer exists will never move by itself — so a link into it is
a silent park. The classifier lives in ``hermes_cli.kanban_parent_state`` (pure, stdlib-only)
and every link door (DB, ``kanban_link`` tool, ``hermes kanban link``) reports its verdict.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_parent_state import (
    PARENT_STATE_CODES, ParentState, parent_promotion_state)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_DB", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


def _parent(status, **extra):
    row = {"status": status, "assignee": "lane", "block_kind": None,
           "current_run_id": None, "due_at": None}
    row.update(extra)
    return row


# --------------------------------------------------------------------------- #
# The classifier
# --------------------------------------------------------------------------- #

def test_codes_are_the_frozen_ordered_tuple():
    """The ordered tuple IS the contract a string-switching consumer dispatches off."""
    assert PARENT_STATE_CODES == (
        "running", "done", "blocked", "review", "triage", "scheduled",
        "unassigned", "dead-assignee", "ready", "todo", "other")


@pytest.mark.parametrize("status,code,self_moves", [
    ("running", "running", True),
    ("done", "done", True),
    ("blocked", "blocked", False),
    ("review", "review", False),
    ("triage", "triage", False),
    ("scheduled", "scheduled", False),
    ("ready", "ready", False),
    ("todo", "todo", False),
])
def test_each_known_status_maps_to_its_code(status, code, self_moves):
    state = parent_promotion_state(_parent(status), profiles=["lane"])
    assert state.code == code
    assert state.self_moves is self_moves
    assert isinstance(state, ParentState)


def test_only_a_moving_or_moved_parent_self_moves():
    """The whole point: everything but ``running``/``done`` is somebody's to unblock."""
    for code in PARENT_STATE_CODES:
        status = {"other": "no-such-status"}.get(code, code)
        row = _parent(status, assignee="" if code == "unassigned" else "lane")
        state = parent_promotion_state(row, profiles=["lane"])
        assert state.self_moves is (code in ("running", "done")), code


def test_running_reports_the_run_and_blocked_the_kind():
    running = parent_promotion_state(_parent("running", current_run_id=41), profiles=["lane"])
    assert running.text == "is RUNNING (run 41)"
    blocked = parent_promotion_state(_parent("blocked", block_kind="dependency"),
                                     profiles=["lane"])
    assert "is BLOCKED (block_kind=dependency)" in blocked.text
    # (e) without a block_kind the sentence must not leak the empty parenthesis.
    bare = parent_promotion_state(_parent("blocked"), profiles=["lane"])
    assert bare.code == "blocked"
    assert bare.text.startswith("is BLOCKED:")
    # (g) a running parent with no run id still names the run.
    assert parent_promotion_state(_parent("running"), profiles=["lane"]).text == "is RUNNING"


def test_scheduled_carries_the_clock_in_one_format_for_epoch_and_iso():
    """``due_at`` is a stored epoch, but the classifier takes the clock as DATA."""
    epoch = parent_promotion_state(_parent("scheduled", due_at=1792540800),
                                   profiles=["lane"])
    iso = parent_promotion_state(_parent("scheduled", due_at="2026-10-21T00:00:00Z"),
                                 profiles=["lane"])
    assert epoch.code == iso.code == "scheduled"
    assert epoch.text == iso.text


def test_scheduled_clock_text_is_iso_utc():
    state = parent_promotion_state(_parent("scheduled", due_at=1792540800), profiles=["lane"])
    assert " until " in state.text
    token = state.text.split(" until ")[1].split(" ")[0].rstrip(":")
    assert token.startswith("2026-10-") and token.endswith("+0000"), state.text
    # A scheduled parent with no clock still names the wait rather than crashing on None.
    clockless = parent_promotion_state(_parent("scheduled"), profiles=["lane"])
    assert " until " not in clockless.text


def test_unassigned_beats_a_dead_assignee():
    state = parent_promotion_state(_parent("ready", assignee="   "), profiles=["lane"])
    assert state.code == "unassigned"


def test_dead_assignee_is_probed_only_when_profiles_are_injected():
    """None means 'not injected' — the classifier does not guess a fleet from disk."""
    row = _parent("ready", assignee="ghost-lane")
    assert parent_promotion_state(row, profiles=["lane"]).code == "dead-assignee"
    assert parent_promotion_state(row, profiles=None).code == "ready"
    assert parent_promotion_state(row).code == "ready"


def test_unknown_status_is_other_and_never_guessed_into_a_known_code():
    state = parent_promotion_state(_parent("quarantined"), profiles=["lane"])
    assert state.code == "other"
    assert state.self_moves is False
    assert "quarantined" in state.text


def test_no_text_carries_an_identity_but_the_parent_it_describes():
    """A notice consumer renders one sentence per parent id: no edge id may appear in it."""
    ids = ("t_aaaa1111", "t_bbbb2222")
    statuses = [{"status": s} for s in
                ("running", "done", "blocked", "review", "triage", "scheduled",
                 "ready", "todo", "quarantined")]
    statuses += [{"status": "ready", "assignee": ""},
                 {"status": "ready", "assignee": "ghost-lane"}]
    for base in statuses:
        row = {"assignee": "lane", "current_run_id": 7, "block_kind": "needs_input",
               "due_at": 1792540800}
        row.update(base)
        row["id"] = ids[0]
        state = parent_promotion_state(row, profiles=["lane"])
        for other in ids:
            assert other not in state.text, (state.code, state.text)


# --------------------------------------------------------------------------- #
# The link door
# --------------------------------------------------------------------------- #

def _link(parent_status, *, profiles=("lane",), board=None):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="upstream", assignee="lane")
        child = kb.create_task(conn, title="downstream", assignee="lane")
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (parent_status, parent))
        conn.commit()
        outcome = kb.link_tasks_reported(conn, parent, child,
                                         profiles=list(profiles), board=board)
    return parent, child, outcome


def test_link_reports_a_standing_parent_in_from_the_child_side(kanban_home):
    parent, _child, outcome = _link("ready")
    assert outcome.gated is True
    assert [e.id for e in outcome.parents] == [parent]
    assert outcome.parents[0].code == "ready"
    assert outcome.parents[0].status == "ready"
    assert outcome.standing == [parent]
    assert outcome.warning is not None
    assert parent in outcome.warning
    assert "NOT MOVING ON ITS OWN" in outcome.warning
    assert "is READY and nothing has claimed it" in outcome.warning


def test_link_behind_a_running_parent_gates_but_warns_nothing(kanban_home):
    """The demotion is not the warning: a running parent is going to finish."""
    _parent_id, _child, outcome = _link("running")
    assert outcome.gated is True
    assert outcome.parents[0].code == "running"
    assert outcome.standing == []
    assert outcome.warning is None


def test_link_behind_a_done_parent_neither_gates_nor_warns(kanban_home):
    _parent_id, _child, outcome = _link("done")
    assert outcome.gated is False
    assert outcome.standing == []
    assert outcome.warning is None


def test_link_reads_every_parent_edge_not_just_the_new_one(kanban_home):
    with kbc.connect() as conn:
        done_parent = kb.create_task(conn, title="finished")
        kb.complete_task(conn, done_parent, result="ok")
        parked = kb.create_task(conn, title="parked in review")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (parked,))
        child = kb.create_task(conn, title="child")
        conn.commit()
        first = kb.link_tasks_reported(conn, done_parent, child, profiles=["lane"])
        assert first.standing == []
        second = kb.link_tasks_reported(conn, parked, child, profiles=["lane"])
    assert {e.id for e in second.parents} == {done_parent, parked}
    assert second.standing == [parked]


def test_link_tasks_stays_the_boolean_face_of_one_write_path(kanban_home):
    """`link_tasks(...) is link_tasks_reported(...).gated` — same inputs, same verdict."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="upstream")
        child = kb.create_task(conn, title="downstream")
        other_parent = kb.create_task(conn, title="other upstream")
        other_child = kb.create_task(conn, title="other downstream")
        # two independent edges, one per door, so each verdict comes from a fresh write
        reported = kb.link_tasks_reported(conn, parent, child)
        boolean = kb.link_tasks(conn, other_parent, other_child)
        done_parent = kb.create_task(conn, title="finished")
        kb.complete_task(conn, done_parent, result="ok")
        third_child = kb.create_task(conn, title="third child")
        unreported_false = kb.link_tasks(conn, done_parent, third_child)
        reported_false = kb.link_tasks_reported(conn, done_parent, third_child)
    assert boolean is True and reported.gated is True
    assert unreported_false is False and reported_false.gated is False


def test_a_non_dispatching_board_earns_its_own_warning_line(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "board_dispatch_enabled", lambda board=None: False)
    _parent_id, _child, outcome = _link("running", board="default")
    assert outcome.board_dispatch == {"default": False}
    assert outcome.warning is not None
    assert "does not dispatch" in outcome.warning


def test_the_board_is_never_inferred_when_the_caller_names_none(kanban_home):
    _parent_id, _child, outcome = _link("ready")
    assert outcome.board_dispatch == {}


# --------------------------------------------------------------------------- #
# The reporting doors
# --------------------------------------------------------------------------- #

def test_kanban_link_tool_reports_parents_standing_and_warning(kanban_home):
    from tools import kanban_tools as kt

    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="waiting in review", assignee="lane")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (parent,))
        child = kb.create_task(conn, title="gated child", assignee="lane")
        conn.commit()
    out = json.loads(kt._handle_link({"parent_id": parent, "child_id": child}))
    assert out.get("ok"), out
    assert out["gated"] is True
    assert out["parents"] == [{"id": parent, "status": "review", "code": "review",
                               "self_moves": False}]
    assert out["standing"] == [parent]
    assert "NOT MOVING ON ITS OWN" in out["warning"]
    assert "board_dispatch" in out


def test_kanban_link_tool_reads_a_status_the_classifier_calls_other(kanban_home):
    from tools import kanban_tools as kt

    # A LIVE assignee ("default" is always a profile) keeps the row out of the
    # dead-assignee branch, so the unknown STATUS is what the classifier reports.
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="odd parent", assignee="default")
        conn.execute("UPDATE tasks SET status = 'quarantined' WHERE id = ?", (parent,))
        child = kb.create_task(conn, title="gated child", assignee="default")
        conn.commit()
    out = json.loads(kt._handle_link({"parent_id": parent, "child_id": child}))
    assert out.get("ok"), out
    assert out["parents"][0]["code"] == "other"
    assert out["standing"] == [parent]


def test_cli_link_prints_the_promotion_receipt_to_stderr(kanban_home, capsys):
    from hermes_cli import kanban as kc

    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="unclaimed parent", assignee="default")
        child = kb.create_task(conn, title="gated child", assignee="default")
        conn.commit()
    rc = kc._cmd_link(argparse.Namespace(parent_id=parent, child_id=child))
    captured = capsys.readouterr()
    assert rc == 0
    assert f"Linked {parent} -> {child}" in captured.out
    assert "Parent state (1 edge(s)):" in captured.err
    assert parent in captured.err
    assert "ready" in captured.err
    assert "NOT MOVING ON ITS OWN" in captured.err


def test_the_real_parser_and_dispatch_reach_the_link_receipt(kanban_home, capsys):
    """The subprocess boundary is the only thing a worker cannot cross (provenance fence);
    parser -> dispatch -> verb must still land the receipt, so exercise it for real."""
    from hermes_cli import kanban as kc
    from hermes_cli.kanban_parser import build_parser

    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent waiting in review", assignee="default")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (parent,))
        child = kb.create_task(conn, title="gated child", assignee="default")
        conn.commit()
    parser = argparse.ArgumentParser(prog="hermes")
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "link", parent, child])
    rc = kc.kanban_command(args)
    captured = capsys.readouterr()
    assert rc == 0
    assert f"Linked {parent} -> {child}" in captured.out
    assert f"  {parent}  review  " in captured.err
    assert "NOT MOVING ON ITS OWN" in captured.err


# --------------------------------------------------------------------------- #
# The CREATE door: a card FILED behind a standing parent is the same silent park
# --------------------------------------------------------------------------- #

def _park(conn, status: str, title: str = "parent") -> str:
    parent = kb.create_task(conn, title=title, assignee="default")
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, parent))
    return parent


def test_create_behind_a_ready_parent_names_the_parent(kanban_home):
    """The common shape: `kanban_create(parents=[...])` on an unclaimed ready parent."""
    with kbc.connect() as conn:
        ready = _park(conn, "ready", "parent waiting in the queue")
        conn.commit()
        child, outcome = kb.create_task_reported(
            conn, title="child", assignee="default", parents=(ready,), profiles=["default"])
    assert outcome.gated is True
    assert [edge.id for edge in outcome.parents] == [ready]
    assert outcome.standing == [ready]
    assert ready in outcome.warning and "NOT MOVING ON ITS OWN" in outcome.warning
    assert kb.receipt_fields(outcome)["gated_by"] == ready
    # The receipt names the CALLER's board, never the ambient one.
    assert outcome.board_dispatch == {}
    with kbc.connect() as conn:
        assert kb.get_task(conn, child).status == "todo"


def test_create_reports_a_parent_that_will_never_move(kanban_home):
    with kbc.connect() as conn:
        parked = _park(conn, "review", "parent waiting in review")
        conn.commit()
        _child, outcome = kb.create_task_reported(
            conn, title="child", assignee="default", parents=(parked,), profiles=["default"])
    assert outcome.parents[0].code == "review"
    assert outcome.standing == [parked]
    assert "NOT MOVING ON ITS OWN" in outcome.warning


def test_create_behind_a_dead_assignee_names_the_assignee(kanban_home):
    """A parent assigned to a lane that is not live: the warning names WHO, not just 'gated'."""
    with kbc.connect() as conn:
        ghost = _park(conn, "ready", "parent of a retired lane")
        conn.execute("UPDATE tasks SET assignee = 'ghost-lane' WHERE id = ?", (ghost,))
        conn.commit()
        _child, outcome = kb.create_task_reported(
            conn, title="child", assignee="default", parents=(ghost,), profiles=["default"])
    assert outcome.parents[0].code == "dead-assignee"
    assert outcome.standing == [ghost]
    assert "ghost-lane" in outcome.warning


def test_create_behind_a_running_parent_gates_but_warns_nothing(kanban_home):
    """A running parent still finishes by itself: the card waits, and that is not a defect."""
    with kbc.connect() as conn:
        running = _park(conn, "running", "parent mid-run")
        conn.commit()
        _child, outcome = kb.create_task_reported(
            conn, title="child", assignee="default", parents=(running,), profiles=["default"])
    assert outcome.gated is True
    assert outcome.standing == []
    assert outcome.warning is None
    # gated_by still names it (that is today's contract) - the point is that no WARNING does.
    assert kb.receipt_fields(outcome)["gated_by"] == running


def test_create_behind_a_done_parent_is_ready_and_reports_nothing(kanban_home):
    with kbc.connect() as conn:
        done = kb.create_task(conn, title="finished", assignee="default")
        kb.complete_task(conn, done, result="ok")
        child, outcome = kb.create_task_reported(
            conn, title="child", assignee="default", parents=(done,), profiles=["default"])
    assert outcome.gated is False
    assert outcome.standing == []
    assert outcome.warning is None
    assert "gated_by" not in kb.receipt_fields(outcome)
    with kbc.connect() as conn:
        assert kb.get_task(conn, child).status == "ready"


def test_create_without_parents_answers_exactly_what_it_always_did(kanban_home):
    """No edge, no receipt: the keys are the ones the door shipped before this change."""
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({"title": "solo", "assignee": "default"}))
    assert out.get("ok"), out
    assert set(out) == {"ok", "task_id", "gated", "subscribed"} | set(kt._CREATED_FIELDS)
    assert out["gated"] is False


def test_create_reads_the_existing_card_when_an_idempotency_key_dedupes(kanban_home):
    with kbc.connect() as conn:
        parked = _park(conn, "review")
        conn.commit()
        first, _ = kb.create_task_reported(
            conn, title="child", assignee="default", parents=(parked,),
            idempotency_key="t-once", profiles=["default"])
        again, outcome = kb.create_task_reported(
            conn, title="child", assignee="default", idempotency_key="t-once", profiles=["default"])
    assert again == first
    assert [edge.id for edge in outcome.parents] == [parked]
    assert outcome.standing == [parked]


def test_create_task_still_returns_the_id_the_reported_door_returns(kanban_home):
    """`create_task` and `create_task_reported` are two faces of ONE write path."""
    with kbc.connect() as conn:
        parked = _park(conn, "review")
        conn.commit()
        child = kb.create_task(conn, title="child", assignee="default", parents=(parked,),
                               idempotency_key="t-shared")
        reported, outcome = kb.create_task_reported(
            conn, title="child", assignee="default", idempotency_key="t-shared",
            profiles=["default"])
    assert child == reported
    assert isinstance(child, str) and child.startswith("t_")
    assert outcome.standing == [parked]
    with kbc.connect() as conn:
        assert kb.parent_ids(conn, child) == [parked]
        assert kb.get_task(conn, child).status == "todo"


def test_kanban_create_tool_reports_the_parent_receipt(kanban_home):
    from tools import kanban_tools as kt

    with kbc.connect() as conn:
        parked = _park(conn, "review", "parent waiting in review")
        conn.commit()
    out = json.loads(kt._handle_create(
        {"title": "child", "assignee": "default", "parents": [parked]}))
    assert out.get("ok"), out
    assert out["gated"] is True and out["gated_by"] == parked
    assert out["standing"] == [parked]
    assert out["parents_state"][0]["id"] == parked
    assert out["parents_state"][0]["code"] == "review"
    assert "NOT MOVING ON ITS OWN" in out["warning"]


def _create_args(**overrides) -> argparse.Namespace:
    args = {"title": "child", "assignee": "default", "body": None, "body_file": None,
            "workspace": None, "priority": 0, "parent": (), "created_by": None,
            "project": None, "tenant": None, "json": False}
    args.update(overrides)
    return argparse.Namespace(**args)


def test_cli_create_prints_the_parent_receipt_to_stderr(kanban_home, capsys):
    """The same lines `hermes kanban link` prints, on the create door too."""
    from hermes_cli import kanban as kc

    with kbc.connect() as conn:
        parked = _park(conn, "review", "parent waiting in review")
        conn.commit()
    rc = kc._cmd_create(_create_args(parent=[parked]))
    captured = capsys.readouterr()
    assert rc == 0
    assert "Created " in captured.out
    assert "Parent state (1 edge(s)):" in captured.err
    assert f"  {parked}  review  " in captured.err
    assert "NOT MOVING ON ITS OWN" in captured.err


def test_cli_create_behind_a_running_parent_prints_the_state_but_no_warning(kanban_home, capsys):
    """A parent that IS moving needs no alarm - the state line alone says so."""
    from hermes_cli import kanban as kc

    with kbc.connect() as conn:
        running = _park(conn, "running", "parent mid-run")
        conn.commit()
    rc = kc._cmd_create(_create_args(parent=[running]))
    captured = capsys.readouterr()
    assert rc == 0
    assert "Parent state (1 edge(s)):" in captured.err
    assert f"  {running}  running  " in captured.err
    assert "NOT MOVING ON ITS OWN" not in captured.err


def test_cli_create_json_carries_the_receipt(kanban_home, capsys):
    from hermes_cli import kanban as kc

    with kbc.connect() as conn:
        parked = _park(conn, "review", "parent waiting in review")
        conn.commit()
    rc = kc._cmd_create(_create_args(parent=[parked], json=True))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["gated"] is True and payload["gated_by"] == parked
    assert payload["standing"] == [parked]
    assert payload["parents_state"][0]["id"] == parked
    assert "NOT MOVING ON ITS OWN" in payload["warning"]
