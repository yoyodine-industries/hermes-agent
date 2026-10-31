"""Spawn gate: a worktree anchor must be a git repository (card t_e5ff985b).

The incident these tests pin (measured 2026-10-18): 26 open cards on the migrations + platform
boards carried ``workspace_path = /opt/hermes_sandbox/platform/<program>/.worktrees/<id>``.
That path is the program's STATE folder — LOOPING / TASKS / EVIDENCE — a plain folder, never a
repo, because the project had been registered with ``primary`` pointing at the state folder
instead of at the repo its work lands in.

Every claim therefore died inside ``resolve_workspace``, the dispatcher retried ONCE, charged
the card, and left it ``ready`` looking as though it were merely waiting its turn. Seven
workers idle, throughput down to 5 cards/day, and nothing on the card said why.

What is asserted here, all through the real seams (no re-implemented logic):

* the birth door refuses a project whose primary is an EXISTING folder that is not a repo, with
  a message that LEADS with ``spawn_failed: no repo at <path>`` and names the repair;
* the claim door raises the distinct ``WorktreeAnchorNotRepo`` class, not a bare ValueError;
* the one classifier verdicts it ``workspace_not_repo`` — deterministic, never prose-parsed;
* a card failing that way is BLOCKED ON THE FIRST ATTEMPT (``force_trip``) even with a generous
  ``failure_limit``, and carries the marker as a comment on the card itself;
* the happy path is untouched: a real repo primary still anchors
  ``<repo>/.worktrees/<task-id>``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import projects_db as pdb

MARKER = kbw.SPAWN_FAILED_NO_REPO_PREFIX


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Gate Test",
         "-c", "user.email=gate@example.com", "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    )


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(root)],
                   check=True, capture_output=True, text=True)
    (root / "README.md").write_text("gate\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    return root


def _make_plain_folder(root: Path) -> Path:
    """The defect's exact shape: a real folder (docs/state) that is not a repo."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "LOOPING.md").write_text("state, not source\n", encoding="utf-8")
    return root


def _project_with(folder: Path, name: str = "State Folder Program"):
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name=name, folders=[str(folder)])
        return pdb.get_project(pc, pid)


# --------------------------------------------------------------------------- birth door


def test_create_defaults_a_plain_folder_primary_to_dir(kanban_home, tmp_path):
    """A.2 (t_e61be4f3): the DEFAULT for a plain-folder project is ``dir:<owner>``, not a worktree.

    This supersedes the t_e5ff985b birth REFUSAL for the default path — refusing was the right
    instant fix for a card that could never claim, but it also made a legitimate shape (a
    doc+code project whose primary is a folder, with no repo anywhere) uncreatable unless the
    caller happened to know ``--workspace dir:<owner>``. Measured before the change:
    ``--workspace dir:<owner>`` always worked, so only the default and the remedy were wrong.
    The t_e5ff985b guard itself survives where it applies: a card explicitly anchored at
    ``<plain folder>/.worktrees/<id>`` still refuses at claim (see the claim-door test below).
    """
    state_dir = _make_plain_folder(tmp_path / "platform" / "state-program")
    proj = _project_with(state_dir)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="lands in the folder", project_id=proj.slug)
        task = kb.get_task(conn, tid)

    assert task.workspace_kind == "dir"
    assert task.workspace_path == str(state_dir)        # anchored AT the project, not under it
    assert not task.branch_name                          # no branch: nothing is checked out
    assert not (state_dir / ".worktrees").exists()       # nothing minted under a non-repo
    # And the anchor RESOLVES: a dir-kind card needs no git repo at all.
    assert Path(kbw.resolve_workspace(task)) == state_dir


def test_create_keeps_the_deferred_worktree_for_a_primary_that_does_not_exist_yet(
    kanban_home, tmp_path,
):
    """Registering a project BEFORE cloning its repo is a legitimate order — unchanged."""
    future = tmp_path / "repo-not-cloned-yet"
    proj = _project_with(future, name="Future Repo")

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="clone first", project_id=proj.slug)
        task = kb.get_task(conn, tid)

    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(future / ".worktrees" / tid)


def test_create_still_works_when_the_primary_is_a_real_repo(kanban_home, tmp_path):
    """Regression: the happy path keeps its deterministic path + branch."""
    repo = _make_repo(tmp_path / "repo")
    proj = _project_with(repo, name="Real Repo")

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="add login", project_id=proj.slug)
        task = kb.get_task(conn, tid)

    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(repo / ".worktrees" / tid)
    assert task.branch_name == f"{proj.slug}/{tid}-add-login"


def test_the_refusal_names_the_dir_remedy_before_the_repo_one(tmp_path):
    """The remedy comes from the door that KNOWS the convention (estate refusal standard).

    A plain folder is a legitimate project primary, so "give the project a real repo" is the
    WRONG first fix for this class: the message must name ``--workspace dir:<anchor>`` first,
    and only then the repo-primary / retarget repairs for cards that really need a worktree.
    """
    anchor = _make_plain_folder(tmp_path / "platform" / "state-program")
    msg = kbw.no_repo_refusal(
        anchor=anchor, task_id="t_deadbeef", project_id="p_x", project_slug="state-program",
    )
    assert msg.startswith(f"{MARKER}{anchor}")
    assert f"--workspace dir:{anchor}" in msg
    assert msg.index("--workspace dir:") < msg.index("hermes project add-folder")
    assert "t_deadbeef" in msg and "state-program" in msg


# --------------------------------------------------------------------------- claim door


def test_claim_refuses_a_card_anchored_on_a_plain_folder(kanban_home, tmp_path):
    """The claim door raises the distinct class, not a bare ValueError."""
    state_dir = _make_plain_folder(tmp_path / "platform" / "state-program")

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="pre-existing dead card", workspace_kind="worktree",
            workspace_path=str(state_dir / ".worktrees" / "t_deadbeef"),
        )
        task = kb.get_task(conn, tid)

        with pytest.raises(kbw.WorktreeAnchorNotRepo) as excinfo:
            kbw.resolve_workspace(task)

    err = excinfo.value
    assert err.anchor == str(state_dir / ".worktrees" / "t_deadbeef")
    assert err.task_id == tid
    assert str(err).startswith(f"{MARKER}{state_dir / '.worktrees' / 't_deadbeef'}")


# --------------------------------------------------------------------------- classifier


def test_the_one_classifier_verdicts_a_no_repo_anchor(kanban_home, tmp_path):
    """Deterministic: the exception class decides, never the message prose."""
    exc = kbw.WorktreeAnchorNotRepo(_make_plain_folder(tmp_path / "plain"), task_id="t_x")
    assert kbd.classify_spawn_failure(exc) == kbd.SPAWN_FAILURE_WORKSPACE_NOT_REPO
    # A generic ValueError that merely TALKS about a repo is still the card's own failure.
    assert kbd.classify_spawn_failure(
        ValueError("spawn_failed: no repo at /nope")) == kbd.SPAWN_FAILURE_CARD


# --------------------------------------------------------------------------- dispatcher


def test_a_no_repo_spawn_blocks_on_the_first_attempt_and_says_why(
    kanban_home, all_assignees_spawnable, tmp_path,
):
    """DoD 4: the card is never left ``ready``, and the reason is ON the card."""
    state_dir = _make_plain_folder(tmp_path / "platform" / "state-program")

    def _must_not_spawn(task, workspace, board=None):  # pragma: no cover - seam fails first
        raise AssertionError("the spawn must never be reached: the anchor is not a repo")

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="dead anchor", assignee="a", workspace_kind="worktree",
            workspace_path=str(state_dir / ".worktrees" / "t_deadbeef"),
        )
        # A GENEROUS limit: a normal card would still be retryable after one failure. This one
        # must be parked immediately, because no retry can turn a folder into a repo.
        res = kbd.dispatch_once(conn, spawn_fn=_must_not_spawn, failure_limit=5)

        row = conn.execute(
            "SELECT status, block_kind, consecutive_failures, last_failure_error "
            "FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert res.spawned == []
        assert tid in res.auto_blocked
        assert row["status"] == "blocked"          # NOT ready
        assert row["consecutive_failures"] == 1    # charged exactly once, no retry loop
        assert row["last_failure_error"].startswith(MARKER)  # front-loaded for the 500-char cut

        comments = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ? ORDER BY id", (tid,),
        ).fetchall()
        assert [c["author"] for c in comments] == ["dispatcher"]
        body = comments[0]["body"]
        assert body.startswith(MARKER)
        assert str(state_dir / ".worktrees" / "t_deadbeef") in body
        assert f"hermes kanban retarget {tid}" in body
        assert "Blocked on the FIRST attempt" in body
