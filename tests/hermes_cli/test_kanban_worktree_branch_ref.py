"""A worktree branch the dispatcher stores is a ref git must ACCEPT.

Card t_2f637b62: branch derivation built a name straight from the card title, so
an ordinary title (one ending in ``.``, one holding ``..``, one ending in
``.lock``, or a 40-char cut landing right before a ``.``) produced a ref
``git check-ref-format`` rejects. ``git worktree add`` then failed for the WHOLE
ref and the dispatch died with a ``workspace: ...`` spawn failure before any
checkout existed — the card could never run, and nothing told anyone why.

Two layers are pinned here:

* derivation (``projects_db.branch_name_for``) sanitizes what it builds, and
* the dispatch seam (``kanban_db_workspace.safe_branch_name``) verifies with git
  itself and repairs a name that was stored before the fix (or written straight
  to the column by an API caller).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import projects_db as pdb


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )


def _git_accepts(name: str) -> bool:
    return subprocess.run(
        ["git", "check-ref-format", "--branch", name], capture_output=True
    ).returncode == 0


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    assert _git(path, "init", "-q", "-b", "main").returncode == 0
    assert _git(path, "config", "user.email", "t@example.invalid").returncode == 0
    assert _git(path, "config", "user.name", "t").returncode == 0
    (path / "f.txt").write_text("hello\n", encoding="utf-8")
    assert _git(path, "add", "-A").returncode == 0
    assert _git(path, "commit", "-q", "-m", "init").returncode == 0
    return path


class _Task:
    def __init__(self, tid: str, workspace_path=None, branch_name=None):
        self.id = tid
        self.workspace_kind = "worktree"
        self.workspace_path = workspace_path
        self.branch_name = branch_name


def test_sanitizer_leaves_a_valid_name_byte_identical():
    """No existing branch moves: identity on everything git already accepts."""
    for name in (
        "web-app/t_abc123-add-login",
        "web-app/t_abc123",
        "wt/t_abc123",
        "feature/custom",
        "p/t_x-a.LOCK",  # git only reserves lowercase '.lock'
        "p/t_x-a.lock.d",
        "p/t_x--",
    ):
        assert _git_accepts(name), name
        assert pdb.sanitize_branch_name(name) == name


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("p/t_x-dot.", "p/t_x-dot"),  # title ended in '.' (or the cut did)
        ("p/t_x-a..b", "p/t_x-a.b"),  # '..' may not appear anywhere
        ("p/t_x-name.lock", "p/t_x-name-lock"),  # '.lock' endings are reserved
        ("p//t_x", "p/t_x"),  # no empty component
        ("p/t_x/", "p/t_x"),  # no trailing separator
        ("-lead/t_x", "lead/t_x"),  # a ref may not begin with '-'
        ("p/.t_x/a", "p/t_x/a"),  # a component may not begin with '.'
        ("p/t_x-@{", "p/t_x--"),  # '@{' is reserved
        ("p/t_x-a b~c", "p/t_x-a-b-c"),  # space and '~' are not allowed
        ("p/t_x-...", "p/t_x-"),  # a run of dots never ends the ref in '.'
        ("..", ""),  # unrecoverable: caller's fallback decides
    ],
)
def test_sanitizer_repairs_what_git_refuses(raw, expected):
    assert not _git_accepts(raw), f"{raw!r} is expected to be refused by git"
    assert pdb.sanitize_branch_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["p/t_x-dot.", "p/t_x-a..b", "p/t_x-name.lock", "-lead", "p//t_x/..", "p/t_x-@{"],
)
def test_sanitizer_is_idempotent(raw):
    once = pdb.sanitize_branch_name(raw, fallback="wt/t_x")
    assert _git_accepts(once)
    assert pdb.sanitize_branch_name(once, fallback="wt/t_x") == once


def test_unrecoverable_name_takes_the_callers_fallback():
    assert pdb.sanitize_branch_name("..", fallback="wt/t_aaaa") == "wt/t_aaaa"
    assert _git_accepts(pdb.sanitize_branch_name("/", fallback="wt/t_aaaa"))


def test_stored_ref_git_rejects_is_repaired_instead_of_killing_the_dispatch(tmp_path):
    """The defect: this raised ``git worktree add failed ...`` and the card died."""
    repo = _make_repo(tmp_path / "repo")
    target = repo / ".worktrees" / "t_bbbb0001"

    resolved, branch = kbw._resolve_worktree_workspace(
        _Task(
            "t_bbbb0001",
            workspace_path=str(target),
            branch_name="web-app/t_bbbb0001-package.",
        )
    )

    assert _git_accepts(branch)
    assert branch == "web-app/t_bbbb0001-package"
    assert (Path(resolved) / ".git").is_file()
    assert kbw._git_current_branch(Path(resolved)) == branch


def test_stored_name_that_is_already_valid_is_not_rewritten(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    target = repo / ".worktrees" / "t_bbbb0002"

    _, branch = kbw._resolve_worktree_workspace(
        _Task("t_bbbb0002", workspace_path=str(target), branch_name="web-app/t_bbbb0002-fine")
    )

    assert branch == "web-app/t_bbbb0002-fine"
    assert kbw._git_current_branch(target) == branch


def test_unrecoverable_stored_name_falls_back_to_the_task_slug(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    target = repo / ".worktrees" / "t_bbbb0003"

    _, branch = kbw._resolve_worktree_workspace(
        _Task("t_bbbb0003", workspace_path=str(target), branch_name="..")
    )

    assert branch == "wt/t_bbbb0003"
    assert kbw._git_current_branch(target) == branch


def test_repair_is_stable_across_dispatches(tmp_path):
    """A repaired name is what the dispatcher persists, so a retry must not move it."""
    repo = _make_repo(tmp_path / "repo")
    target = repo / ".worktrees" / "t_bbbb0004"
    stored = "web-app/t_bbbb0004-end."

    resolved, branch = kbw._resolve_worktree_workspace(
        _Task("t_bbbb0004", workspace_path=str(target), branch_name=stored)
    )
    assert branch == "web-app/t_bbbb0004-end"
    assert kbw.safe_branch_name(branch, task_id="t_bbbb0004") == branch

    # The retry sees the repaired name the dispatcher stored: reuse, no re-create.
    again_path, again = kbw._resolve_worktree_workspace(
        _Task("t_bbbb0004", workspace_path=str(target), branch_name=branch)
    )
    assert (Path(again_path), again) == (Path(resolved), branch)
