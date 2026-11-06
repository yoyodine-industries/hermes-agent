"""THE ALIGNMENT-VEHICLE GATE (card t_f79daa7f, second half).

What carries this design:

* a card that ORDERS a mutating git act in a LIVE tree and names no admitted vehicle is
  REFUSED at the kernel's create door, before any write - the class this closes is a card
  whose only possible executor is one the estate does not admit;
* a card that NAMES the update chain (``r5-install`` / ``hermes-update`` / ``update
  chain``) is admitted - the rule routes the act, it does not ban the work;
* MEASURING a live tree is always allowed: read-only verbs never match;
* an act bound to a NON-live tree (``git -C /opt/hermes_sandbox/...``) is not this rule's
  business, and a host whose cards never name a live tree never matches (inert);
* the refusal is an ANSWER, not an exception: the same falsy ``str`` the filing budget
  returns, so the CLI already reports it (rc 3) and a producer cannot mistake it for an id;
* the DECOMPOSE seam is covered per child, all-or-nothing: an orchestrator must not fan
  out through a second door what the ordinary door refuses.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_graph as kbg
from hermes_cli import kanban_alignment_vehicle_gate as gate
from hermes_cli.kanban_filing_policy import is_refusal

LIVE = "~/.hermes/hermes-agent"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HERMES_HOME: its own board, and the host's default live roots."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(gate.ENV_ROOTS, raising=False)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT"):
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


# --- the grammar: no inference, three ANDed conditions --------------------------- #

def test_a_git_act_in_a_live_tree_with_no_vehicle_is_refused():
    record = gate.evaluate(
        title="align the live tree",
        body="On the serving checkout, run `git reset --hard origin/overrides/live` "
             "in ~/.hermes/hermes-agent so HEAD matches the branch.",
    )
    assert record is not None and record["status"] == "refused"
    assert record["act"] == "git reset"
    assert record["root"].endswith(".hermes/hermes-agent")
    assert "r5-install" in record["line"] and "hermes-update" in record["line"]


def test_git_dash_C_bound_to_the_live_root_is_refused():
    record = gate.evaluate(title="sync", body="git -C ~/.hermes/hermes-agent checkout .")
    assert record is not None and record["act"] == "git checkout"


def test_naming_the_vehicle_admits_the_same_card():
    for vehicle in ("r5-install", "hermes-update", "the update chain"):
        assert gate.evaluate(
            title="align the live tree",
            body="git reset --hard origin/overrides/live in ~/.hermes/hermes-agent; "
                 "carried by %s." % vehicle,
        ) is None, vehicle


def test_measuring_a_live_tree_is_always_allowed():
    """The audit's first half exists to make measurement cheap - it must never be refused."""
    assert gate.evaluate(
        title="measure the live tree",
        body="Run `git status --porcelain` and `git rev-parse HEAD` in ~/.hermes/hermes-agent, "
             "then compare with origin/overrides/live.",
    ) is None


def test_an_act_bound_to_a_non_live_tree_is_not_this_rules_business():
    assert gate.evaluate(
        title="fix in a sandbox clone",
        body="git -C /opt/hermes_sandbox/platform/work/x reset --hard origin/main, then open a PR.",
    ) is None


def test_a_card_about_nothing_is_admitted():
    assert gate.evaluate(title="write the release note", body="No checkout involved.") is None
    assert gate.evaluate(title="", body="") is None


def test_a_host_with_no_live_roots_is_inert(monkeypatch):
    monkeypatch.setenv(gate.ENV_ROOTS, "")
    assert gate.evaluate(title="align", body="git reset --hard in ~/.hermes/hermes-agent") is None


def test_the_refusal_is_an_answer_not_an_exception():
    refusal, record = gate.apply(title="align", body="git reset --hard in %s" % LIVE)
    assert is_refusal(refusal)
    assert not refusal, "a refusal is FALSY: `if not task_id:` must catch it"
    assert isinstance(refusal, str) and "refused: alignment card" in refusal
    assert record["gate"] == "alignment_vehicle"


# --- the door: refused at creation, nothing written ------------------------------ #

def test_the_create_door_refuses_and_writes_no_row(conn, capsys):
    task_id = kb.create_task(
        conn, title="align the live tree",
        body="git reset --hard origin/overrides/live in %s" % LIVE,
        assignee="platform-coder", created_by="test",
    )
    assert is_refusal(task_id)
    assert "alignment card" in str(task_id)
    assert str(task_id) in capsys.readouterr().err, "the line is printed where a producer reads"
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_the_create_door_admits_the_same_card_naming_the_vehicle(conn):
    task_id = kb.create_task(
        conn, title="align the live tree via hermes-update",
        body="Wait for the update chain (r5-install) to carry origin/overrides/live into "
             "~/.hermes/hermes-agent.",
        assignee="platform-coder", created_by="test",
    )
    assert not is_refusal(task_id)
    row = conn.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None


def test_the_decompose_seam_refuses_per_child_and_writes_nothing(conn):
    root = kb.create_task(conn, title="plan the alignment", assignee="platform-stl",
                          created_by="test", triage=True)
    assert not is_refusal(root)
    refusal = kbg.decompose_triage_task(
        conn, root, root_assignee="platform-stl", author="test", children=[
            {"title": "measure the live tree", "assignee": "platform-coder",
             "body": "git status in %s" % LIVE},
            {"title": "align the live tree", "assignee": "platform-coder",
             "body": "git reset --hard origin/overrides/live in %s" % LIVE},
        ])
    assert is_refusal(refusal)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1, \
        "all-or-nothing: the root remains the only row"
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (root,)).fetchone()[0] == "triage"
