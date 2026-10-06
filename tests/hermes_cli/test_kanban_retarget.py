"""``hermes kanban retarget`` — the recovery door for a mis-born card (t_5b296c31).

A card's project + workspace binding is fixed at ``create_task`` time and, before
this verb, could only be corrected by archive+recreate (which severs whatever the
card carries - SDLC flow items, parent edges) or hand-written SQL. These cases pin
the door: it re-points a card onto the right project, records the before/after as a
``retargeted`` event, works on a parked (blocked) card, and refuses the moves that
would be worse than doing nothing (an unknown project, a terminal card, a live
worker's claim).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import projects_db as pdb


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _retargeted_event(conn, tid):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'retargeted' "
        "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    return json.loads(row["payload"]) if row else None


def test_retarget_repoints_a_mis_born_card_onto_its_project(fresh_home, tmp_path):
    """The RECOVER leg, on the exact mis-birth shape.

    A card born with no project onto a board whose ``default_workdir`` is a repo it
    must NOT work in - here standing in for the ops board defaulting to the
    hermes-agent INSTALL (the shape of t_a2b2274d <- sdlc-ops-0ad1c5). ``retarget``
    re-points it to the project that owns the change and RECORDS the move, on a
    BLOCKED card, leaving it parked.
    """
    wrong = tmp_path / "hermes-agent"
    wrong.mkdir()
    right = tmp_path / "yaan-web-services-dashboard"
    right.mkdir()
    with pdb.connect_closing() as pconn:
        proj_id = pdb.create_project(pconn, name="Dashboard", primary_path=str(right))

    kb.create_board("opslike", name="OpsLike", default_workdir=str(wrong))
    conn = kbc.connect(board="opslike")
    try:
        # Reproduce the mis-birth the live kernel produces: a kind-less card on a
        # board that pins a repo is upgraded to a WORKTREE in the board default (the
        # fleet override that made the ops board default to the hermes-agent install;
        # upstream HEAD does not do this, so the shape is built directly here).
        tid = kb.create_task(
            conn, title="clone-placement enforcement", board="opslike",
            workspace_kind="worktree", workspace_path=str(wrong))
        born = kb.get_task(conn, tid)
        assert born.workspace_kind == "worktree"
        assert born.workspace_path == str(wrong)
        assert born.project_id is None                       # the mis-birth

        # Park it the way the operator's card was parked before the fix.
        kb.block_task(conn, tid, reason="mis-born", kind="needs_input")
        assert kb.get_task(conn, tid).status == "blocked"

        out = kb.retarget_task(
            conn, tid, project=proj_id,
            reason="re-point to the engine's repo (was mis-born to the install)",
            actor="platform-coder")
        assert out["changed"] is True
        assert (out["old"]["workspace_path"] or "").startswith(str(wrong))
        assert out["new"]["workspace_path"] == str(right / ".worktrees" / tid)

        got = kb.get_task(conn, tid)
        assert got.project_id == proj_id
        assert got.workspace_kind == "worktree"
        assert got.workspace_path == str(right / ".worktrees" / tid)
        assert got.status == "blocked"                       # a parked card stays parked

        payload = _retargeted_event(conn, tid)
        assert payload is not None, "the move must be recorded"
        assert payload["old"]["workspace_path"].startswith(str(wrong))
        assert payload["new"]["project_id"] == proj_id
        assert payload["actor"] == "platform-coder"
        assert "engine's repo" in (payload["reason"] or "")
    finally:
        conn.close()


def test_retarget_refuses_an_unknown_project_and_writes_nothing(fresh_home, tmp_path):
    """A named-but-unknown project is REFUSED (unlike create's silent drop): a
    retarget that quietly kept the old binding would defeat the verb."""
    repo = tmp_path / "repo"
    repo.mkdir()
    kb.create_board("b1", name="B1", default_workdir=str(repo))
    conn = kbc.connect(board="b1")
    try:
        tid = kb.create_task(conn, title="x", board="b1")
        before = kb.get_task(conn, tid)
        with pytest.raises(ValueError) as exc:
            kb.retarget_task(conn, tid, project="p_does_not_exist", reason="typo")
        assert "not found" in str(exc.value)
        after = kb.get_task(conn, tid)
        assert (after.project_id, after.workspace_kind, after.workspace_path) == (
            before.project_id, before.workspace_kind, before.workspace_path)
        assert _retargeted_event(conn, tid) is None
    finally:
        conn.close()


def test_retarget_refuses_a_terminal_card(fresh_home):
    kb.create_board("b2", name="B2")
    conn = kbc.connect(board="b2")
    try:
        tid = kb.create_task(conn, title="done card", board="b2")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        conn.commit()
        with pytest.raises(ValueError) as exc:
            kb.retarget_task(conn, tid, project="none", reason="too late")
        assert "terminal" in str(exc.value)
    finally:
        conn.close()


def test_retarget_clears_the_link_and_goes_scratch(fresh_home, tmp_path):
    """``--project none`` detaches: the card owns no repo again (scratch)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with pdb.connect_closing() as pconn:
        proj_id = pdb.create_project(pconn, name="R", primary_path=str(repo))
    kb.create_board("b3", name="B3")
    conn = kbc.connect(board="b3")
    try:
        tid = kb.create_task(conn, title="linked", board="b3", project_id=proj_id)
        assert kb.get_task(conn, tid).workspace_kind == "worktree"
        out = kb.retarget_task(conn, tid, project="none", reason="detach")
        got = kb.get_task(conn, tid)
        assert got.project_id is None
        assert got.workspace_kind == "scratch"
        assert got.workspace_path is None
        assert out["new"]["branch_name"] is None
    finally:
        conn.close()


def test_retarget_noop_records_no_event(fresh_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pdb.connect_closing() as pconn:
        proj_id = pdb.create_project(pconn, name="R2", primary_path=str(repo))
    kb.create_board("b4", name="B4")
    conn = kbc.connect(board="b4")
    try:
        tid = kb.create_task(conn, title="unbound", board="b4")
        first = kb.retarget_task(conn, tid, project=proj_id, reason="noop-1")
        assert first["changed"] is True
        second = kb.retarget_task(conn, tid, project=proj_id, reason="noop-2")
        assert second["changed"] is False
        # exactly one event: the idempotent re-run must not append a second
        n = conn.execute(
            "SELECT count(*) AS n FROM task_events WHERE task_id = ? AND kind = 'retargeted'",
            (tid,)).fetchone()["n"]
        assert n == 1
    finally:
        conn.close()


def test_retarget_refuses_a_live_claim_unless_forced(fresh_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pdb.connect_closing() as pconn:
        proj_id = pdb.create_project(pconn, name="R3", primary_path=str(repo))
    kb.create_board("b5", name="B5")
    conn = kbc.connect(board="b5")
    try:
        tid = kb.create_task(conn, title="busy", board="b5")
        conn.execute(
            "UPDATE tasks SET claim_lock = ?, claim_expires = ? WHERE id = ?",
            ("host:123:abc", int(time.time()) + 600, tid))
        conn.commit()
        with pytest.raises(ValueError) as exc:
            kb.retarget_task(conn, tid, project=proj_id, reason="race")
        assert "live claim" in str(exc.value)
        forced = kb.retarget_task(conn, tid, project=proj_id, reason="worker gone", force=True)
        assert forced["changed"] is True
    finally:
        conn.close()


def test_cli_retarget_verb_is_wired(fresh_home, tmp_path):
    """The verb is reachable end to end through the SAME entry the CLI and the
    gateway use (``run_slash`` drives ``build_parser`` + the handler map)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with pdb.connect_closing() as pconn:
        pdb.create_project(pconn, name="CLIProj", primary_path=str(repo))
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cli target")
    out = kc.run_slash("retarget %s --project CLIProj --reason 'cli wiring'" % tid)
    assert "Retargeted %s" % tid in out, out
    with kbc.connect() as conn:
        got = kb.get_task(conn, tid)
    assert got.project_id is not None
    assert (got.workspace_path or "").startswith(str(repo))
