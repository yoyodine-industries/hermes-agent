"""A project-linked card is never BORN inside a guard-protected tree (t_81c03c9a).

Two subsystems were each right on their own: the shared-trees guard protects the
live checkouts, and the dispatcher anchors a project card's worktree under the
project's own repo. Together they produced a card whose workspace sits inside a
tree the guard refuses to let the lane write - the card could not do the one
thing it exists to do.

The seam that CHOSE the path is the one that yields, and it ASKS the guard's own
declaration of the protected roots rather than re-implementing the rule. These
cases pin all four required legs on BOTH doors (birth and retarget):

1. a card born onto a guard-protected project repo lands OUTSIDE that root;
2. that workspace is a LINKED worktree of the project's canonical repo (``git
   worktree list --porcelain`` names it), never a clone;
3. the guard still REFUSES a lane write into the repo itself and ADMITS the
   chosen lane area - both sides in one test, so the fix can never be satisfied
   by loosening the guard;
4. an unprotected project repo keeps today's ``<repo>/.worktrees/<id>``.

The guard's declaration is injected here (a two-line module declaring the roots),
which is the same interface the real guard exposes: the constants, not a copy of
the rule.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import projects_db as pdb

_REAL_GUARD = Path(
    os.environ.get("HERMES_SHARED_TREES_GUARD")
    or "/opt/hermes_prod/yaan-majordomo/ops-checks/guard-shared-trees.py"
)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.invalid"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"],
                   check=True, capture_output=True)
    (path / "f.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"],
                   check=True, capture_output=True)
    return path


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
    kbw._GUARD_ROOTS_CACHE.clear()
    return home


def _declare_guard(path: Path, roots: list[Path]) -> Path:
    """A guard module whose DECLARATION names *roots* -- the only interface used."""
    decl = [str(r) for r in roots]
    path.write_text(
        "#!/usr/bin/env python3\n"
        f"_SHARED_SOURCE_ROOTS = {decl!r}\n"
        "_LIVE_ROOTS = []\n"
        "_REPAIR_ROOTS = []\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    """A sandbox root, one protected repo inside it, and the guard declaring it."""
    sandbox = tmp_path / "sandbox"
    protected = _repo(sandbox / "yaan-platform")
    guard = _declare_guard(tmp_path / "guard.py", [protected])
    monkeypatch.setenv("HERMES_SHARED_TREES_GUARD", str(guard))
    monkeypatch.setenv("HERMES_SANDBOX_ROOT", str(sandbox))
    kbw._GUARD_ROOTS_CACHE.clear()
    return {"sandbox": sandbox, "protected": protected, "guard": guard}


def test_birth_and_retarget_land_outside_a_protected_project_repo(fresh_home, guarded):
    """Leg 1 on BOTH doors: neither seam may land the card inside the root."""
    protected = guarded["protected"]
    with pdb.connect_closing() as pconn:
        proj_id = pdb.create_project(pconn, name="Platform", primary_path=str(protected))

    kb.create_board("migrations", name="Migrations", default_workdir=str(protected))
    conn = kbc.connect(board="migrations")
    try:
        # BIRTH
        tid = kb.create_task(
            conn, title="seam", board="migrations", assignee="platform-coder",
            workspace_kind="worktree", project_id=proj_id)
        born = kb.get_task(conn, tid)
        assert born.workspace_kind == "worktree"
        expected = guarded["sandbox"] / "platform" / "work" / "platform-coder" / tid
        assert born.workspace_path == str(expected), (
            f"born at {born.workspace_path!r}, inside the protected root "
            f"{protected}: the birth seam must yield"
        )
        assert kbw.protected_root_for(born.workspace_path) is None

        # RETARGET: a card mis-born onto a plain dir, re-pointed at the project.
        other = _repo(guarded["sandbox"] / "elsewhere")
        tid2 = kb.create_task(
            conn, title="seam-retarget", board="migrations", assignee="platform-coder",
            workspace_kind="worktree", workspace_path=str(other))
        out = kb.retarget_task(conn, tid2, project=proj_id, reason="repoint", actor="platform-coder")
        new_path = out["new"]["workspace_path"]
        assert new_path == str(guarded["sandbox"] / "platform" / "work" / "platform-coder" / tid2), (
            f"retarget landed at {new_path!r}: retarget is what a lane reaches for "
            f"AFTER spawn_failed, so it may not re-open the same door"
        )
        assert kbw.protected_root_for(new_path) is None
    finally:
        conn.close()


def test_the_workspace_is_a_linked_worktree_not_a_clone(fresh_home, guarded):
    """Leg 2: the out-of-tree workspace shares the project's object store."""
    protected = guarded["protected"]
    with pdb.connect_closing() as pconn:
        proj_id = pdb.create_project(pconn, name="Platform", primary_path=str(protected))

    kb.create_board("migrations", name="Migrations", default_workdir=str(protected))
    conn = kbc.connect(board="migrations")
    try:
        tid = kb.create_task(
            conn, title="linked", board="migrations", assignee="platform-coder",
            workspace_kind="worktree", project_id=proj_id)
        task = kb.get_task(conn, tid)
        workspace = kbw.resolve_workspace(task, board="migrations")
        assert Path(workspace).exists(), "the workspace must be materialised"

        listing = subprocess.run(
            ["git", "-C", str(protected), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, check=True).stdout
        named = [ln.split(" ", 1)[1] for ln in listing.splitlines() if ln.startswith("worktree ")]
        assert str(Path(workspace)) in named or str(Path(workspace).resolve()) in named, (
            f"{workspace} is not named by the anchor's `git worktree list`: it was "
            f"CLONED, which is the disk tap t_bfc54177 exists to stop. Named: {named}"
        )
        assert (Path(workspace) / ".git").is_file(), (
            "a linked worktree carries a .git FILE pointing at the anchor's "
            "object store; a clone carries a directory"
        )
    finally:
        conn.close()


@pytest.mark.skipif(not _REAL_GUARD.exists(), reason="no shared-trees guard on this host")
def test_the_guard_still_refuses_the_repo_and_admits_the_lane_area(fresh_home, tmp_path):
    """Leg 3, BOTH sides: the chosen path is only safe because the guard admits it.

    Asserting one side alone would pass on a loosened guard, so the same guard
    process is asked twice: a lane write into the protected repo (must be
    refused) and a lane write into its own work area (must be admitted).
    """
    protected = Path("/opt/hermes_sandbox/yaan-platform")
    if not protected.exists():
        pytest.skip("the protected checkout this host's guard declares is absent")

    def ask(target: Path) -> subprocess.CompletedProcess:
        payload = json.dumps({
            "hook_event_name": "pre_tool_call",
            "tool_name": "write_file",
            "tool_input": {"path": str(target)},
            "profile": "platform-coder",
            "cwd": str(pathlib_cwd()),
        })
        return subprocess.run([sys.executable, str(_REAL_GUARD)], input=payload,
                              capture_output=True, text=True, timeout=120)

    inside = ask(protected / "hermes_cli" / "kanban_db.py")
    assert inside.returncode == 2 and '"block"' in inside.stdout, (
        f"the guard must still refuse a lane write into {protected}: "
        f"rc={inside.returncode} out={inside.stdout[:400]!r}"
    )
    lane = Path("/opt/hermes_sandbox/platform/work/platform-coder/probe-card")
    admitted = ask(lane / "notes.md")
    assert admitted.returncode == 0, (
        f"the guard must ADMIT the lane's own work area {lane}, which is where the "
        f"seam now anchors the card: rc={admitted.returncode} out={admitted.stdout[:400]!r}"
    )


def pathlib_cwd() -> Path:
    return Path.cwd()


def test_an_unprotected_project_repo_keeps_todays_path(fresh_home, guarded):
    """Leg 4: the change is scoped -- nothing moves for an unprotected project."""
    plain = _repo(fresh_home.parent / "plain-project")
    with pdb.connect_closing() as pconn:
        proj_id = pdb.create_project(pconn, name="Plain", primary_path=str(plain))

    kb.create_board("plain", name="Plain", default_workdir=str(plain))
    conn = kbc.connect(board="plain")
    try:
        tid = kb.create_task(
            conn, title="plain", board="plain", assignee="platform-coder",
            workspace_kind="worktree", project_id=proj_id)
        assert kb.get_task(conn, tid).workspace_path == str(plain / ".worktrees" / tid)
    finally:
        conn.close()


def test_a_protected_repo_with_no_way_out_is_refused_by_name(fresh_home, guarded):
    """The refusal leg: name the project, the attempted path and the root."""
    protected = guarded["protected"]
    with pdb.connect_closing() as pconn:
        proj_id = pdb.create_project(pconn, name="Platform", primary_path=str(protected))
    kb.create_board("migrations", name="Migrations", default_workdir=str(protected))
    conn = kbc.connect(board="migrations")
    try:
        with pytest.raises(kbw.WorkspaceInsideGuardedTree) as exc:
            # "bootstrap" has no dash and no lane-domains row: no lane area.
            kb.create_task(
                conn, title="nowhere", board="migrations", assignee="bootstrap",
                workspace_kind="worktree", project_id=proj_id)
        message = str(exc.value)
        assert proj_id.split("_")[-1][:6] in message or "platform" in message.lower()
        assert str(protected) in message
        assert ".worktrees" in message
    finally:
        conn.close()
