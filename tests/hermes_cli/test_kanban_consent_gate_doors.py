"""The consent gate's two DOORS, exercised through the tick they are wired into.

Why this file exists (card ``t_2460e4ba``): the gate module had 33 passing tests over
``evaluate()`` and ``check_consent_guard()`` while **neither door was called from
production** -- the run door had been dropped from the tracked dispatch file and the filing
door from ``create_task``, and both suites stayed green for 17 days. Testing the verdict is
not testing the door. These tests drive ``dispatch_once`` and ``create_task`` themselves, so
a wiring that goes missing again fails here.

Contract:

* the RUN door is inside the tick: a refused card does not spawn, is parked
  ``blocked``/``needs_input``, and appears on ``DispatchResult.consent_held`` -- which the
  tick's own suppression line counts, so a held card never reads as an idle board;
* ``dry_run`` reports the refusal and writes nothing;
* the FILING door refuses an unbacked CLAIM before any row is written, and admits a
  DECLARED card (proposing gated work is legal; running it is not).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent import estop
from hermes_cli import kanban_consent_gate as cg
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture(autouse=True)
def operator_context(monkeypatch):
    """Run as an operator, not inside a dispatched worker's inherited env."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nobody")
    estop._logged_components.clear()
    kb.init_db()
    return home


@pytest.fixture
def approvals(tmp_path, monkeypatch) -> str:
    """A stand-in approvals store: exactly one APPROVED row, card t_00000007."""
    path = tmp_path / "approvals.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE approvals (id INTEGER PRIMARY KEY, status TEXT, decision TEXT, "
        "card_id TEXT, board TEXT, superseded_by TEXT, reason TEXT)"
    )
    con.execute(
        "INSERT INTO approvals (id, status, decision, card_id, board, superseded_by) "
        "VALUES (7, 'approved', 'approved', 't_00000007', 'ops', NULL)"
    )
    con.commit()
    con.close()
    monkeypatch.setenv(cg.APPROVALS_DB_ENV, str(path))
    return str(path)


def _spawn_recorder(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    return fake_spawn


def _ready(conn, title, body=""):
    tid = kb.create_task(conn, title=title, body=body, assignee="platform-worker")
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    conn.commit()
    return tid


def test_the_tick_refuses_and_parks_a_declared_card(kanban_home, approvals, all_assignees_spawnable):
    """The RUN door is IN the tick: no spawn, parked with its cause, and counted."""
    spawns: list = []
    with kbc.connect() as conn:
        tid = _ready(conn, "CONSENT-GATED: install the rewritten plists",
                     "bootout and bootstrap the agent")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(spawns))

        assert spawns == [], "a consent-refused card must not spawn"
        assert (tid, "consent-refused:no_ref") in res.consent_held
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["status"] == "blocked" and row["block_kind"] == "needs_input"
        # ONE record, not one per tick.
        assert conn.execute(
            "SELECT count(*) AS n FROM task_events WHERE task_id = ? AND kind = 'consent_refused'",
            (tid,)).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT count(*) AS n FROM task_comments WHERE task_id = ?", (tid,)).fetchone()["n"] == 1

        # A SECOND tick finds nothing to do (the card is parked) and adds no record.
        res2 = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(spawns))
        assert res2.consent_held == []
        assert conn.execute(
            "SELECT count(*) AS n FROM task_comments WHERE task_id = ?", (tid,)).fetchone()["n"] == 1

    # The hold is on the suppression line: a refused card is never an idle board.
    line = kbd.describe_suppression([res])
    assert "consent_held=1" in line, line


def test_the_tick_does_not_hold_a_card_that_only_NAMES_the_mechanism(
    kanban_home, approvals, all_assignees_spawnable,
):
    """The measured false positive (t_2460e4ba / t_919ee6ad): a mention must run."""
    spawns: list = []
    with kbc.connect() as conn:
        tid = _ready(conn, "Implement: enforce the consent-gated SURFACE class at the write boundary",
                     "the module's own cards must not be held")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(spawns))

        assert spawns == [tid], "a mid-line mention is not a declaration and must spawn"
        assert res.consent_held == []


def test_the_tick_dry_run_reports_the_hold_and_writes_nothing(
    kanban_home, approvals, all_assignees_spawnable,
):
    with kbc.connect() as conn:
        tid = _ready(conn, "CONSENT-GATED: bounce the gateway", "reload the plists")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder([]), dry_run=True)

        assert (tid, "consent-refused:no_ref") in res.consent_held
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["status"] == "ready", "dry_run must not write the hold"
        assert conn.execute(
            "SELECT count(*) AS n FROM task_events WHERE task_id = ? AND kind = 'consent_refused'",
            (tid,)).fetchone()["n"] == 0


def test_the_filing_door_refuses_an_unbacked_claim(kanban_home, approvals):
    """A claim with no reference behind it is a false statement in the record."""
    with kbc.connect() as conn:
        with pytest.raises(cg.ConsentRefused):
            kb.create_task(
                conn,
                title="Raise the script timeout (operator approved this)",
                body="no reference behind it",
                assignee="platform-worker",
            )
        assert conn.execute(
            "SELECT count(*) AS n FROM tasks WHERE title LIKE 'Raise the script timeout%'",
        ).fetchone()["n"] == 0, "nothing may be written by a refused filing"


def test_the_filing_door_admits_a_declared_card(kanban_home, approvals):
    """Proposing gated work is legal; the RUN door is what holds it."""
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="CONSENT-GATED: activate ready-queue admission",
            body="propose the keys, wait for the decision", assignee="platform-worker",
        )
        assert conn.execute("SELECT id FROM tasks WHERE id = ?", (tid,)).fetchone()["id"] == tid


def test_the_filing_door_admits_a_backed_claim(kanban_home, approvals):
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="Operator approved the cap change (APR-0007)", body="go",
            assignee="platform-worker",
        )
        assert conn.execute("SELECT id FROM tasks WHERE id = ?", (tid,)).fetchone()["id"] == tid
