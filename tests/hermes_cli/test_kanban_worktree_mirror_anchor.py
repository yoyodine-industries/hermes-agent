"""Per-card worktrees anchored on a canonical clone / bare mirror.

Card t_1fc64895: a cargo-cult per-card ``git clone`` costs GB per card. The
supported shape is ONE canonical clone (or bare mirror) per repo, with a linked
worktree per card that shares its object store and is released on completion.

Before this change the workspace resolver could not express that at all:

* an explicit ``workspace_path`` OUTSIDE any repo (e.g. under the owning
  profile's work dir) raised "not inside a git repo"; and
* a **bare** canonical mirror was not a valid board anchor
  (``git rev-parse --show-toplevel`` is empty for a bare repo).

``_cleanup_worktree_workspace`` additionally refused any worktree whose common
dir was not literally named ``.git``, so a bare-mirror worktree was never
released. Each behaviour is pinned below.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hermes_cli import kanban_db_workspace as kbw


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    assert _git(path, "init", "-q", "-b", "main").returncode == 0
    assert _git(path, "config", "user.email", "t@example.invalid").returncode == 0
    assert _git(path, "config", "user.name", "t").returncode == 0
    (path / "f.txt").write_text("hello\n", encoding="utf-8")
    assert _git(path, "add", "-A").returncode == 0
    assert _git(path, "commit", "-q", "-m", "init").returncode == 0
    return path


def _make_bare_mirror(source: Path, dest: Path) -> Path:
    assert subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(dest)],
        capture_output=True, text=True, timeout=60,
    ).returncode == 0
    return dest


class _Task:
    def __init__(self, tid: str, workspace_path=None, branch_name=None):
        self.id = tid
        self.workspace_kind = "worktree"
        self.workspace_path = workspace_path
        self.branch_name = branch_name


def _pin_board_anchor(monkeypatch, anchor: Path) -> None:
    monkeypatch.setattr(
        kbw._kb, "read_board_metadata",
        lambda board: {"default_workdir": str(anchor)},
        raising=True,
    )


def _head_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_explicit_target_outside_repo_anchors_on_board_clone(tmp_path, monkeypatch):
    """A lane-dir target (not inside any repo) anchors on the board's clone."""
    clone = _make_repo(tmp_path / "canonical")
    lane = tmp_path / "work" / "platform-coder"
    lane.mkdir(parents=True)
    _pin_board_anchor(monkeypatch, clone)

    target = lane / "t_aaaa0001"
    resolved, branch = kbw._resolve_worktree_workspace(
        _Task("t_aaaa0001", workspace_path=str(target)), board="defcon")

    assert Path(resolved) == target
    assert (target / ".git").is_file()          # a linked worktree, not a clone
    assert _head_commit(target) == _head_commit(clone)
    assert branch == "wt/t_aaaa0001"


def test_bare_mirror_is_a_valid_board_anchor(tmp_path, monkeypatch):
    """With no workspace_path a bare mirror yields ``<mirror>.worktrees/<id>``."""
    clone = _make_repo(tmp_path / "canonical")
    mirror = _make_bare_mirror(clone, tmp_path / "mirror.git")
    _pin_board_anchor(monkeypatch, mirror)

    resolved, _ = kbw._resolve_worktree_workspace(_Task("t_aaaa0002"), board="defcon")

    assert Path(resolved) == tmp_path / "mirror.git.worktrees" / "t_aaaa0002"
    assert (Path(resolved) / ".git").is_file()
    assert _head_commit(Path(resolved)) == _head_commit(clone)


def test_bare_mirror_anchor_with_explicit_lane_target(tmp_path, monkeypatch):
    """A bare mirror anchors a worktree placed under the owning profile dir."""
    clone = _make_repo(tmp_path / "canonical")
    mirror = _make_bare_mirror(clone, tmp_path / "mirror.git")
    lane = tmp_path / "work" / "platform-coder"
    lane.mkdir(parents=True)
    _pin_board_anchor(monkeypatch, mirror)

    target = lane / "t_aaaa0003"
    resolved, _ = kbw._resolve_worktree_workspace(
        _Task("t_aaaa0003", workspace_path=str(target)), board="defcon")

    assert Path(resolved) == target
    assert (target / ".git").is_file()


def test_release_removes_clean_bare_mirror_worktree(tmp_path, monkeypatch):
    """Completion releases a clean worktree of a bare mirror (and its branch)."""
    clone = _make_repo(tmp_path / "canonical")
    mirror = _make_bare_mirror(clone, tmp_path / "mirror.git")
    lane = tmp_path / "lane"
    lane.mkdir()
    _pin_board_anchor(monkeypatch, mirror)

    target = lane / "t_aaaa0004"
    kbw._resolve_worktree_workspace(
        _Task("t_aaaa0004", workspace_path=str(target)), board="defcon")

    kbw._cleanup_worktree_workspace("t_aaaa0004", str(target), None)

    assert not target.exists(), "clean worktree of a bare mirror must be released"
    refs = _git(mirror, "for-each-ref", "--format=%(refname)", "refs/heads").stdout
    assert "wt/t_aaaa0004" not in refs


def test_release_preserves_dirty_bare_mirror_worktree(tmp_path, monkeypatch):
    """Dirty work is never destroyed, even off a bare mirror."""
    clone = _make_repo(tmp_path / "canonical")
    mirror = _make_bare_mirror(clone, tmp_path / "mirror.git")
    lane = tmp_path / "lane"
    lane.mkdir()
    _pin_board_anchor(monkeypatch, mirror)

    target = lane / "t_aaaa0005"
    kbw._resolve_worktree_workspace(
        _Task("t_aaaa0005", workspace_path=str(target)), board="defcon")
    (target / "f.txt").write_text("uncommitted\n", encoding="utf-8")

    kbw._cleanup_worktree_workspace("t_aaaa0005", str(target), None)

    assert target.exists(), "dirty worktree must be preserved"


def test_release_removes_clean_worktree_when_mirror_head_is_not_trunk(tmp_path, monkeypatch):
    """Regression (review round 1): a bare mirror's HEAD may sit on a NON-trunk
    branch, and a card worktree is cut from that HEAD.

    ``git clone --bare`` records the source's checked-out branch as the mirror's
    HEAD. Judging ``_worktree_has_unpushed_commits`` against the local trunk
    (``main``) alone then reports a clean, fully-published worktree as unpushed
    forever, so card close silently never releases it. Baselining on the
    anchor's own refs fixes it.
    """
    clone = _make_repo(tmp_path / "canonical")
    assert _git(clone, "checkout", "-q", "-b", "card-t_5892a108").returncode == 0
    (clone / "f.txt").write_text("off-trunk\n", encoding="utf-8")
    assert _git(clone, "add", "-A").returncode == 0
    assert _git(clone, "commit", "-q", "-m", "off trunk").returncode == 0
    mirror = _make_bare_mirror(clone, tmp_path / "mirror.git")
    assert _git(mirror, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/card-t_5892a108"
    # The case only discriminates while the mirror HEAD is ahead of `main`.
    main_tip = _git(mirror, "rev-parse", "refs/heads/main").stdout.strip()
    assert _head_commit(mirror) != main_tip

    lane = tmp_path / "lane"
    lane.mkdir()
    _pin_board_anchor(monkeypatch, mirror)
    target = lane / "t_aaaa0007"
    kbw._resolve_worktree_workspace(
        _Task("t_aaaa0007", workspace_path=str(target)), board="defcon")
    assert _head_commit(target) != main_tip

    kbw._cleanup_worktree_workspace("t_aaaa0007", str(target), None)

    assert not target.exists(), (
        "a clean worktree published in the bare anchor (its HEAD branch) must be "
        "released even when the anchor HEAD is not `main`"
    )


def test_release_preserves_worktree_with_commit_unique_to_its_branch(tmp_path, monkeypatch):
    """Safety half: a commit that exists only on the card's own branch is real
    work and must never be released, however the anchor is baselined."""
    clone = _make_repo(tmp_path / "canonical")
    mirror = _make_bare_mirror(clone, tmp_path / "mirror.git")
    lane = tmp_path / "lane"
    lane.mkdir()
    _pin_board_anchor(monkeypatch, mirror)
    target = lane / "t_aaaa0008"
    kbw._resolve_worktree_workspace(
        _Task("t_aaaa0008", workspace_path=str(target)), board="defcon")
    (target / "f.txt").write_text("unique\n", encoding="utf-8")
    assert _git(target, "add", "-A").returncode == 0
    assert _git(target, "commit", "-q", "-m", "unique work").returncode == 0

    kbw._cleanup_worktree_workspace("t_aaaa0008", str(target), None)

    assert target.exists(), "a commit unique to the card branch must be preserved"


def test_release_removes_worktree_published_by_another_anchor_ref(tmp_path, monkeypatch):
    """``published in that anchor`` means reachable from any of its refs OTHER
    than the worktree's own branch -- so a commit that only looks unique because
    no *remote* holds it is still released once an anchor branch carries it."""
    clone = _make_repo(tmp_path / "canonical")
    mirror = _make_bare_mirror(clone, tmp_path / "mirror.git")
    lane = tmp_path / "lane"
    lane.mkdir()
    _pin_board_anchor(monkeypatch, mirror)
    target = lane / "t_aaaa0009"
    kbw._resolve_worktree_workspace(
        _Task("t_aaaa0009", workspace_path=str(target)), board="defcon")
    (target / "f.txt").write_text("carried elsewhere\n", encoding="utf-8")
    assert _git(target, "add", "-A").returncode == 0
    assert _git(target, "commit", "-q", "-m", "carried elsewhere").returncode == 0
    # Publish that commit on another ref of the same anchor.
    assert _git(mirror, "branch", "published", "wt/t_aaaa0009").returncode == 0

    kbw._cleanup_worktree_workspace("t_aaaa0009", str(target), None)

    assert not target.exists(), "a commit published on another anchor ref must be released"


def test_release_never_removes_plain_dir(tmp_path):
    """The safety guard: a plain dir inside a repo is not a linked worktree."""
    repo = _make_repo(tmp_path / "repo")
    plain = repo / "notes"
    plain.mkdir()
    (plain / "x.txt").write_text("keep\n", encoding="utf-8")

    kbw._cleanup_worktree_workspace("t_aaaa0006", str(plain), None)

    assert plain.exists()
