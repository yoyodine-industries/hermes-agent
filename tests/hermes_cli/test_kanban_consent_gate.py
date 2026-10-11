"""The consent-reference gate: claims must resolve, and the doors must act.

Contract under test (ruling, card t_e31d9241):

* a card that ASSERTS consent with no reference behind it is refused at the
  FILING door (``kanban_db.create_task``) before any row is written;
* a card that asserts consent, or that DECLARES itself consent-gated, with no
  APPROVED reference is refused the RUN at the dispatch door and parked
  ``blocked``/``needs_input`` -- parked, never silently skipped;
* a card with neither trigger is untouched, and a prohibition that merely NAMES
  a consent-gated surface is not a trigger (the measured false-positive class
  that keeps the lexical "write verb + gated surface" rule out of the gate).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_consent_gate as cg
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def approvals(tmp_path) -> str:
    """A stand-in approvals store: one approved, one pending, one superseded row."""
    path = tmp_path / "approvals.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE approvals (id INTEGER PRIMARY KEY, status TEXT, decision TEXT, "
        "card_id TEXT, board TEXT, superseded_by TEXT, reason TEXT)"
    )
    rows = [
        (7, "approved", "approved", "t_00000007", "ops", None),
        (284, "pending", "", "t_42075b98", "ops", None),
        (375, "responded", "responded", "", "ops", None),
        (400, "superseded", "", "t_00000400", "ops", "APR-0007"),
        (401, "superseded", "", "t_00000401", "ops", "APR-0284"),
    ]
    con.executemany(
        "INSERT INTO approvals (id, status, decision, card_id, board, superseded_by) "
        "VALUES (?, ?, ?, ?, ?, ?)", rows,
    )
    con.commit()
    con.close()
    return str(path)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# --- the clauses -------------------------------------------------------------

@pytest.mark.parametrize("text,trigger", [
    ("Immediate liveness (operator approval attached): kanban.max_spawn=3", "claim"),
    ("Execute approved work order APR-0024: schedule the next platform leg", "claim"),
    ("APPROVED: raise the cap", "claim"),
    ("This change is consent-gated: install the plist", "declared"),
    ("Batch C: convert 11 live config.yaml sites (system change, needs Rob)", "declared"),
    ("CONSENT-GATED: activate ready-queue admission", "declared"),
    ("Rotate the compressor bloom filters", ""),
])
def test_trigger_detection(text, trigger):
    assert cg.evaluate(text, approvals_db="x").trigger == trigger


@pytest.mark.parametrize("text", [
    # measured false positives of the lexical surface rule: PROHIBITIONS
    "PROHIBITED: hand copies onto a live path, bulk board actions, `config.yaml` changes.",
    "HARD STOPS: do not commit, do not edit config.yaml, .env or any other live path.",
    "Any fork/kernel change, any new DB, any runtime `config.yaml` write is out of scope.",
    "Run `hermes gateway restart`",  # a receipt line, not a claim or a declaration
    "the port registry forbids installing the LaunchDaemon",
])
def test_named_surfaces_are_not_triggers(text):
    verdict = cg.evaluate(text, approvals_db="x")
    assert verdict.trigger == "" and verdict.licensed


def test_claim_with_no_reference_is_refused(approvals):
    v = cg.evaluate("Liveness (operator approval attached)", "", approvals_db=approvals)
    assert v.refused and v.cause == "no_ref"


def test_pending_row_does_not_license(approvals):
    """The measured instance: the claim cites APR-0284, which was still pending."""
    v = cg.evaluate("Immediate liveness (operator approval attached) -- see APR-0284",
                    approvals_db=approvals)
    assert v.refused and v.cause == "ref_not_approved"
    assert [(r.ref, r.status) for r in v.refs] == [("APR-0284", "pending")]


def test_approved_row_licenses(approvals):
    v = cg.evaluate("Operator approved the cap change (APR-0007)", approvals_db=approvals)
    assert v.licensed and v.refs[0].status == "approved"


def test_responded_row_does_not_license(approvals):
    v = cg.evaluate("consent granted for the invariant sweep (APR-0375)", approvals_db=approvals)
    assert v.refused and v.cause == "ref_not_approved"


def test_superseded_row_follows_to_the_covering_decision(approvals):
    v = cg.evaluate("Operator approved the cap change (APR-0400)", approvals_db=approvals)
    assert v.licensed and v.refs[0].ref == "APR-0007" and "superseded by" in v.refs[0].note


def test_superseded_onto_pending_still_refuses(approvals):
    v = cg.evaluate("consent granted (APR-0401)", approvals_db=approvals)
    assert v.refused and v.cause == "ref_not_approved" and v.refs[0].ref == "APR-0284"


def test_unresolved_reference_refuses(approvals):
    v = cg.evaluate("Operator approved it (APR-9999)", approvals_db=approvals)
    assert v.refused and v.cause == "unresolved_ref"


def test_unreadable_store_fails_closed_for_a_claim(tmp_path):
    v = cg.evaluate("operator approved this (APR-0007)", approvals_db=str(tmp_path / "nope.db"))
    assert v.refused and v.cause == "store_unreadable"


def test_unreadable_store_still_refuses_a_refless_claim(tmp_path):
    # No reference means the store is never even consulted -- and a claim with
    # nothing behind it is refused whether or not the store answers.
    v = cg.evaluate("operator approved this", approvals_db=str(tmp_path / "nope.db"))
    assert v.refused and v.cause == "no_ref"


def test_unreadable_store_leaves_other_cards_alone(tmp_path):
    assert cg.evaluate("Rotate the bloom filters", approvals_db=str(tmp_path / "nope.db")).licensed


def test_refusal_message_names_the_moves(approvals):
    msg = cg.refusal_message("t_x", cg.evaluate("operator approved this", approvals_db=approvals))
    assert "CONSENT REFUSED (no_ref)" in msg and "hold the card" in msg


# --- the FILING door ---------------------------------------------------------

def test_create_task_refuses_an_asserted_claim(home, approvals, monkeypatch):
    monkeypatch.setenv(cg.APPROVALS_DB_ENV, approvals)
    with kbc.connect() as conn:
        before = conn.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"]
        # Asserted by name, not by class object: the refusal type is imported
        # lazily in ``create_task``, and module purges elsewhere in the suite
        # would otherwise make two distinct classes of the same name.
        with pytest.raises(ValueError) as caught:
            kb.create_task(conn, title="Immediate liveness (operator approval attached)",
                           body="kanban.max_spawn=3 + gateway restart", assignee="default")
        assert type(caught.value).__name__ == "ConsentRefused"
        assert "CONSENT REFUSED" in str(caught.value)
        assert conn.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"] == before


def test_create_task_admits_a_backed_claim(home, approvals, monkeypatch):
    monkeypatch.setenv(cg.APPROVALS_DB_ENV, approvals)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Operator approved the cap change (APR-0007)",
                             body="raise the ceiling", assignee="default")
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]


def test_create_task_admits_a_consent_gated_proposal(home, approvals, monkeypatch):
    """Filing a consent-gated PROPOSAL is legal -- only running it is not."""
    monkeypatch.setenv(cg.APPROVALS_DB_ENV, approvals)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="CONSENT-GATED: activate ready-queue admission",
                             body="propose the keys, wait for the decision", assignee="default")
        assert conn.execute("SELECT id FROM tasks WHERE id = ?", (tid,)).fetchone()["id"] == tid


# --- the RUN door ------------------------------------------------------------

def _ready_card_with(conn, title, body):
    tid = kb.create_task(conn, title=title, body=body, assignee="platform-worker")
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    conn.commit()
    return tid


def test_dispatch_guard_parks_an_amended_claim(home, approvals, monkeypatch):
    """The live hole: the claim arrives after filing (amendment or a revoked row)."""
    monkeypatch.setenv(cg.APPROVALS_DB_ENV, approvals)
    with kbc.connect() as conn:
        tid = _ready_card_with(conn, "Rotate the bloom filters", "no consent language here")
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?",
                     ("Immediate liveness (operator approval attached): restart the gateway", tid))
        conn.commit()

        reason = kbd.check_consent_guard(conn, tid)
        assert reason == "consent-refused:no_ref"
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["status"] == "blocked" and row["block_kind"] == "needs_input"
        events = conn.execute(
            "SELECT count(*) AS n FROM task_events WHERE task_id = ? AND kind = 'consent_refused'",
            (tid,)).fetchone()["n"]
        comments = conn.execute(
            "SELECT count(*) AS n FROM task_comments WHERE task_id = ?", (tid,)).fetchone()["n"]
        assert events == 1 and comments == 1
        # Already parked: the guard does not re-refuse a card it has held.
        assert kbd.check_consent_guard(conn, tid) is None
        # Re-admitted by hand (a lane unblocks it): the refusal holds, and the
        # second tick adds no duplicate comment.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        assert kbd.check_consent_guard(conn, tid) == "consent-refused:no_ref"
        assert conn.execute(
            "SELECT count(*) AS n FROM task_comments WHERE task_id = ?", (tid,)).fetchone()["n"] == 1


def test_dispatch_guard_holds_a_declared_consent_gated_card(home, approvals, monkeypatch):
    monkeypatch.setenv(cg.APPROVALS_DB_ENV, approvals)
    with kbc.connect() as conn:
        tid = _ready_card_with(conn, "CONSENT-GATED: install the rewritten plists",
                               "bootout and bootstrap the agent")
        assert kbd.check_consent_guard(conn, tid) == "consent-refused:no_ref"
        assert conn.execute("SELECT block_kind FROM tasks WHERE id = ?",
                            (tid,)).fetchone()["block_kind"] == "needs_input"


def test_dispatch_guard_passes_a_backed_card(home, approvals, monkeypatch):
    monkeypatch.setenv(cg.APPROVALS_DB_ENV, approvals)
    with kbc.connect() as conn:
        tid = _ready_card_with(conn, "Operator approved the cap change (APR-0007)", "go")
        assert kbd.check_consent_guard(conn, tid) is None
        assert conn.execute("SELECT status FROM tasks WHERE id = ?",
                            (tid,)).fetchone()["status"] == "ready"


def test_dispatch_guard_dry_run_reports_without_writing(home, approvals, monkeypatch):
    monkeypatch.setenv(cg.APPROVALS_DB_ENV, approvals)
    with kbc.connect() as conn:
        tid = _ready_card_with(conn, "Rotate the bloom filters", "nothing asserted yet")
        # The claim arrives by amendment: the FILING door would have refused it,
        # which is exactly why the RUN door re-evaluates the card's live text.
        conn.execute("UPDATE tasks SET title = ? WHERE id = ?", ("operator approved this", tid))
        conn.commit()
        assert kbd.check_consent_guard(conn, tid, dry_run=True) == "consent-refused:no_ref"
        assert conn.execute("SELECT status FROM tasks WHERE id = ?",
                            (tid,)).fetchone()["status"] == "ready"
