"""Tests for worktree workspace teardown at task completion/archive, the
residue sweep for cards that ended without that hook, and the identity marker a
parked tree carries.

Covers the ownership gap where kanban ``worktree`` workspaces were never
reaped by anything: ``_cleanup_workspace`` preserved them by design, the CLI
startup pruner explicitly skips ``t_*`` worktrees ("dispatcher-driven
lifecycle"), and ``kanban gc`` only swept scratch. The terminal reap now fires
whenever the tree holds no *unrecoverable* state: a clean tree whose HEAD either
rides a branch that survives the removal — any non-``wt/`` card branch
(``<project-slug>/<task-id>``) keeps its commits, so an unpushed project branch
no longer pins the tree on disk — or has no commits unreachable from a
remote-tracking ref. Commits are protected by KEEPING THE BRANCH, not by keeping
the tree; a dirty tree, or a detached HEAD whose commits no ref points at, is
still preserved. ``sweep_terminal_worktree_workspaces`` reaps the residue of
cards that ended without the completion/archive hook.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project repo with a remote whose history is fully pushed."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    _git("-C", str(project), "push", "origin", "HEAD")
    return project


def _make_worktree(repo: Path, task_id: str, branch: str | None = None) -> Path:
    target = repo / ".worktrees" / task_id
    kbw._ensure_git_worktree(repo, target, branch or f"wt/{task_id}")
    return target


def _branch_exists(repo: Path, branch: str) -> bool:
    out = _git("-C", str(repo), "branch", "--list", branch)
    return bool(out.strip())


def _ref_sha(repo: Path, ref: str) -> str | None:
    """The sha a ref points at, or ``None`` when the ref does not exist."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _commit(wt: Path, name: str = "work.txt") -> str:
    """Commit one file in the worktree; returns the new commit sha."""
    (wt / name).write_text(f"local work in {name}\n", encoding="utf-8")
    _git("-C", str(wt), "add", name)
    _git("-C", str(wt), "commit", "-m", f"local work in {name}")
    return _git("-C", str(wt), "rev-parse", "HEAD").strip()


def _commit_object_exists(repo: Path, sha: str) -> bool:
    """Whether the commit object still exists in the repo's object store."""
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# _cleanup_worktree_workspace unit behavior
# ---------------------------------------------------------------------------


def test_clean_pushed_worktree_removed(repo: Path) -> None:
    wt = _make_worktree(repo, "t_aaaa1111")
    kbw._cleanup_worktree_workspace("t_aaaa1111", str(wt))
    assert not wt.exists()
    # auto-generated task branch goes with it
    assert not _branch_exists(repo, "wt/t_aaaa1111")
    # main checkout untouched
    assert (repo / "README.md").exists()


def test_dirty_worktree_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_bbbb2222")
    (wt / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    kbw._cleanup_worktree_workspace("t_bbbb2222", str(wt))
    assert wt.is_dir()
    assert (wt / "wip.txt").exists()
    # the surviving tree states why it is still there
    marker = (wt / kbw._WORKTREE_MARKER_NAME).read_text(encoding="utf-8")
    assert "t_bbbb2222" in marker
    assert "uncommitted" in marker


def test_unpushed_commits_reaped_with_branch_kept(repo: Path) -> None:
    """Commits ride the BRANCH: an unpushed ``wt/`` card branch no longer pins
    the tree on disk, and the commit stays resolvable through that branch."""
    wt = _make_worktree(repo, "t_cccc3333")
    sha = _commit(wt)
    kbw._cleanup_worktree_workspace("t_cccc3333", str(wt))
    assert not wt.exists()
    assert _ref_sha(repo, "refs/heads/wt/t_cccc3333") == sha
    assert _commit_object_exists(repo, sha)


def test_project_branch_unpushed_commits_reaped_and_reachable(repo: Path) -> None:
    """A project-linked card's branch is never deleted, so the tree is reapable
    even with unpushed commits — the branch ref holds them."""
    wt = _make_worktree(repo, "t_hhhh8888", branch="proj/t_hhhh8888")
    sha = _commit(wt)
    kbw._cleanup_worktree_workspace("t_hhhh8888", str(wt), "proj/t_hhhh8888")
    assert not wt.exists()
    assert _ref_sha(repo, "refs/heads/proj/t_hhhh8888") == sha
    assert _commit_object_exists(repo, sha)


def test_fully_pushed_wt_branch_and_tree_both_removed(repo: Path) -> None:
    wt = _make_worktree(repo, "t_iiii9999")
    sha = _commit(wt)
    _git("-C", str(wt), "push", "origin", "HEAD:refs/heads/wt/t_iiii9999")
    kbw._cleanup_worktree_workspace("t_iiii9999", str(wt))
    assert not wt.exists()
    assert not _branch_exists(repo, "wt/t_iiii9999")
    # the commit survives in the remote-tracking ref that the push updated
    assert _ref_sha(repo, "refs/remotes/origin/wt/t_iiii9999") == sha
    assert _commit_object_exists(repo, sha)


def test_detached_head_with_unpushed_commits_preserved(repo: Path) -> None:
    """No ref holds a detached HEAD's commits, so the tree is the only copy."""
    wt = _make_worktree(repo, "t_jjjj0000")
    _git("-C", str(wt), "checkout", "--detach")
    sha = _commit(wt)
    assert _git("-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    kbw._cleanup_worktree_workspace("t_jjjj0000", str(wt))
    assert wt.is_dir()
    # the branch stayed at the base commit: removing the tree would lose `sha`
    assert _ref_sha(repo, "refs/heads/wt/t_jjjj0000") != sha


def test_detached_clean_head_reaped(repo: Path) -> None:
    """A detached HEAD with nothing unreachable is still just a clean tree."""
    wt = _make_worktree(repo, "t_oooo1111")
    _git("-C", str(wt), "checkout", "--detach")
    assert _git("-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    kbw._cleanup_worktree_workspace("t_oooo1111", str(wt))
    assert not wt.exists()


def test_custom_branch_survives_worktree_removal(repo: Path) -> None:
    wt = _make_worktree(repo, "t_dddd4444", branch="feature/custom")
    kbw._cleanup_worktree_workspace("t_dddd4444", str(wt), "feature/custom")
    assert not wt.exists()
    # only auto-generated wt/* branches are deleted
    assert _branch_exists(repo, "feature/custom")


def test_main_checkout_never_removed(repo: Path) -> None:
    kbw._cleanup_worktree_workspace("t_eeee5555", str(repo))
    assert repo.is_dir()
    assert (repo / "README.md").exists()


def test_non_git_dir_preserved(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-worktree"
    plain.mkdir()
    kbw._cleanup_worktree_workspace("t_ffff6666", str(plain))
    assert plain.is_dir()


def test_tree_dirtied_between_check_and_removal_preserved(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU: a tree that becomes dirty after the pre-check is NOT removed.

    Simulates the race by making the pre-check see a clean tree while the
    tree is actually dirty when ``git worktree remove`` runs. Without
    ``--force``, git's own dirty guard re-verifies at removal time and the
    removal fails safe.
    """
    wt = _make_worktree(repo, "t_gggg7777")
    (wt / "late-wip.txt").write_text("dirtied after the check\n", encoding="utf-8")
    # Pre-check lies (as if the file appeared just after it ran) — real git
    # must still refuse the removal.
    from hermes_cli import worktree_ops

    monkeypatch.setattr(worktree_ops, "_worktree_is_dirty", lambda _p: False)
    kbw._cleanup_worktree_workspace("t_gggg7777", str(wt))
    assert wt.is_dir()
    assert (wt / "late-wip.txt").exists()


# ---------------------------------------------------------------------------
# Part 3: the parked tree identifies itself
# ---------------------------------------------------------------------------


def test_marker_written_excluded_and_invisible_to_git_status(repo: Path) -> None:
    wt = _make_worktree(repo, "t_kkkk1111")
    marker = wt / kbw._WORKTREE_MARKER_NAME
    assert marker.is_file()
    text = marker.read_text(encoding="utf-8")
    # names the task, its branch and the canonical checkout it is not
    assert "t_kkkk1111" in text
    assert "wt/t_kkkk1111" in text
    assert str(repo.resolve()) in text
    # invisible to git: no untracked marker, so the tree still reads clean
    assert _git("-C", str(wt), "status", "--porcelain") == ""
    # ... because it is listed in the repo's common .git/info/exclude, which is
    # exactly where `git rev-parse --git-path info/exclude` points from the tree
    exclude = Path(_git("-C", str(wt), "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = wt / exclude
    entries = exclude.read_text(encoding="utf-8").split()
    assert kbw._WORKTREE_MARKER_NAME in entries
    assert exclude.resolve() == (repo / ".git" / "info" / "exclude").resolve()


def test_marker_rewritten_idempotently_on_existing_tree(repo: Path) -> None:
    wt = _make_worktree(repo, "t_mmmm2222")
    marker = wt / kbw._WORKTREE_MARKER_NAME
    marker.unlink()
    # the "tree already exists" early return re-stamps it
    _make_worktree(repo, "t_mmmm2222")
    assert marker.is_file()
    assert _git("-C", str(wt), "status", "--porcelain") == ""


def test_marker_skipped_when_name_is_tracked(repo: Path) -> None:
    tracked = repo / kbw._WORKTREE_MARKER_NAME
    tracked.write_text("tracked marker content\n", encoding="utf-8")
    _git("-C", str(repo), "add", kbw._WORKTREE_MARKER_NAME)
    _git("-C", str(repo), "commit", "-m", "project ships its own marker file")
    wt = _make_worktree(repo, "t_llll3333")
    # tracked content is never clobbered, and the tracked file stays tracked
    assert (wt / kbw._WORKTREE_MARKER_NAME).read_text(encoding="utf-8") == "tracked marker content\n"
    assert _git("-C", str(wt), "status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# Lifecycle integration: complete / archive / deferred parents
# ---------------------------------------------------------------------------


def _worktree_task(conn, repo: Path, title: str = "wt-task") -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title, assignee="worker")
    wt = _make_worktree(repo, tid)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, "
            "branch_name=? WHERE id=?",
            (str(wt), f"wt/{tid}", tid),
        )
    return tid, wt


def test_complete_task_reaps_clean_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")
    assert not wt.exists()
    assert not _branch_exists(repo, f"wt/{tid}")


def test_complete_task_preserves_dirty_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")
    assert wt.is_dir()
    assert (wt / "wip.txt").exists()


def test_archive_task_reaps_clean_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        assert kb.archive_task(conn, tid)
    assert not wt.exists()


def test_parent_worktree_deferred_until_children_done(
    kanban_home: Path, repo: Path
) -> None:
    with kbc.connect_closing() as conn:
        parent, parent_wt = _worktree_task(conn, repo, title="parent")
        child = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent, child)

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        assert kb.claim_task(conn, parent, claimer="worker") is not None
        assert kb.complete_task(conn, parent, summary="parent done")
        # child still active -> parent worktree must survive for handoff
        assert parent_wt.is_dir()

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.complete_task(conn, child, summary="child done")
    # last child terminal -> deferred parent worktree reaped
    assert not parent_wt.exists()


# ---------------------------------------------------------------------------
# Part 2: sweeping the residue of cards that never ran the reap hook
# ---------------------------------------------------------------------------


def _terminal_worktree_task(
    conn,
    repo: Path,
    *,
    title: str = "sweep-task",
    status: str = "done",
    age_hours: float = 24.0,
) -> tuple[str, Path]:
    """A card that ended without the completion hook's reap (e.g. `done` was
    written by a crash-recovery path, or it failed/cancelled outright)."""
    tid, wt = _worktree_task(conn, repo, title=title)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
            (status, int(time.time() - age_hours * 3600), tid),
        )
    return tid, wt


def test_sweep_reaps_terminal_card_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        _tid, wt = _terminal_worktree_task(conn, repo)
        summary = kbw.sweep_terminal_worktree_workspaces(conn)
    assert not wt.exists()
    assert summary["removed"] == [str(wt)]
    assert summary["scanned"] == 1
    assert summary["preserved"] == {}


def test_sweep_leaves_non_terminal_row_untouched(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        _tid, wt = _worktree_task(conn, repo)
        summary = kbw.sweep_terminal_worktree_workspaces(conn)
    assert wt.is_dir()
    assert summary["removed"] == []
    assert summary["scanned"] == 0


def test_sweep_leaves_recent_terminal_row_untouched(kanban_home: Path, repo: Path) -> None:
    """Younger than ``min_age_hours``: the completion hook's job, not ours."""
    with kbc.connect_closing() as conn:
        _tid, wt = _terminal_worktree_task(conn, repo, age_hours=0.0)
        summary = kbw.sweep_terminal_worktree_workspaces(conn, min_age_hours=6.0)
    assert wt.is_dir()
    assert summary["scanned"] == 0


def test_sweep_skips_missing_dir_without_raising(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        _tid, wt = _terminal_worktree_task(conn, repo)
        shutil.rmtree(wt)
        summary = kbw.sweep_terminal_worktree_workspaces(conn)
    assert summary["removed"] == []
    assert summary["skipped"] == 1


def test_sweep_skips_non_canonical_path(kanban_home: Path, repo: Path, tmp_path: Path) -> None:
    """Only ``<repo>/.worktrees/<owning task id>`` is ever touched."""
    elsewhere = tmp_path / "not-a-kanban-tree"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("keep me\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        tid, _wt = _terminal_worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(elsewhere), tid))
        summary = kbw.sweep_terminal_worktree_workspaces(conn)
    assert elsewhere.is_dir()
    assert (elsewhere / "precious.txt").exists()
    assert summary["removed"] == []
    assert summary["skipped"] == 1


def test_sweep_skips_basename_mismatch(kanban_home: Path, repo: Path) -> None:
    """A tree named for a different card is not ours to reap."""
    other = repo / ".worktrees" / "t_zzzz9999"
    kbw._ensure_git_worktree(repo, other, "wt/t_zzzz9999")
    with kbc.connect_closing() as conn:
        tid, _wt = _terminal_worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(other), tid))
        summary = kbw.sweep_terminal_worktree_workspaces(conn)
    assert other.is_dir()
    assert summary["removed"] == []
    assert summary["skipped"] == 1


def test_sweep_respects_limit(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        made = [_terminal_worktree_task(conn, repo, title=f"sweep-{i}") for i in range(3)]
        summary = kbw.sweep_terminal_worktree_workspaces(conn, limit=1)
    assert len(summary["removed"]) == 1
    assert sum(1 for _tid, wt in made if wt.exists()) == 2


def test_sweep_reports_preserved_with_reason(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        _tid, wt = _terminal_worktree_task(conn, repo)
        (wt / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        summary = kbw.sweep_terminal_worktree_workspaces(conn)
    assert wt.is_dir()
    assert summary["removed"] == []
    assert str(wt) in summary["preserved"]


def test_sweep_dry_run_mutates_nothing(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _terminal_worktree_task(conn, repo)
        planned = kbw.sweep_terminal_worktree_workspaces(conn, dry_run=True)
        assert wt.is_dir()
        assert _branch_exists(repo, f"wt/{tid}")
        assert planned["removed"] == [str(wt)]
        # the real pass then does exactly what the dry run reported
        executed = kbw.sweep_terminal_worktree_workspaces(conn)
    assert not wt.exists()
    assert executed["removed"] == planned["removed"]


def test_sweep_gate_is_per_board_db(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hourly gate is memoized per board DB, never per process.

    ``dispatch_once`` is per-board (its tick lock is keyed on the resolved DB
    path), so one module-level stamp would let whichever board ticked first each
    hour sweep, leaving every sibling board's residue on disk forever.
    """
    monkeypatch.setattr(dispatch, "_LAST_TERMINAL_WORKTREE_SWEEP", {})
    kb.init_db(board="alpha")
    kb.init_db(board="beta")

    with kbc.connect_closing(board="alpha") as conn:
        _tid, first = _terminal_worktree_task(conn, repo, title="alpha-residue")
        dispatch._maybe_sweep_terminal_worktrees(conn, board="alpha")
        assert not first.exists()
        # Second call for the SAME board inside the window: no re-sweep, even
        # with fresh residue on disk.
        _tid2, second = _terminal_worktree_task(conn, repo, title="alpha-residue-2")
        dispatch._maybe_sweep_terminal_worktrees(conn, board="alpha")
        assert second.is_dir()

    with kbc.connect_closing(board="beta") as conn:
        _tid3, sibling = _terminal_worktree_task(conn, repo, title="beta-residue")
        # Un-touched by alpha's window: the sibling board sweeps on its own tick.
        dispatch._maybe_sweep_terminal_worktrees(conn, board="beta")
        assert not sibling.exists()
    # ... because each board's memo key is its own resolved DB path.
    assert dispatch._terminal_worktree_sweep_key("alpha") != (
        dispatch._terminal_worktree_sweep_key("beta")
    )


def test_dry_run_tick_does_not_sweep(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry-run tick mutates nothing — not even the hourly residue sweep."""
    monkeypatch.setattr(dispatch, "_LAST_TERMINAL_WORKTREE_SWEEP", {})
    with kbc.connect_closing() as conn:
        tid, wt = _terminal_worktree_task(conn, repo)
        result = dispatch.dispatch_once(conn, dry_run=True)
        assert result.skipped_locked is False
        assert wt.is_dir()
        assert _branch_exists(repo, f"wt/{tid}")
        # The gate was not consumed, so the next REAL tick still does the work.
        dispatch.dispatch_once(conn)
    assert not wt.exists()
    assert not _branch_exists(repo, f"wt/{tid}")


def test_sweep_runs_outside_the_dispatch_lock(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep is a post-lock extra, so it can never extend the lock hold.

    A sibling dispatcher holding the board's tick lock skips the tick; the sweep
    still runs (and does its git work) while the lock is demonstrably held by
    another handle — i.e. outside the single-writer critical section.
    """
    monkeypatch.setattr(dispatch, "_LAST_TERMINAL_WORKTREE_SWEEP", {})
    db_path = kb.kanban_db_path()
    with kbc.connect_closing() as conn:
        _tid, wt = _terminal_worktree_task(conn, repo)
        with kbc._dispatch_tick_lock(db_path) as held:
            assert held is True
            result = dispatch.dispatch_once(conn)
            assert result.skipped_locked is True
            assert not wt.exists()


def test_gc_dry_run_reports_and_gc_reaps(kanban_home: Path, repo: Path, capsys: pytest.CaptureFixture) -> None:
    """`hermes kanban gc --dry-run` reports; the real run reaps."""
    with kbc.connect_closing() as conn:
        _tid, wt = _terminal_worktree_task(conn, repo)
    args = argparse.Namespace(event_retention_days=30, log_retention_days=30, dry_run=True)
    assert kanban_ops._cmd_gc(args) == 0
    assert wt.is_dir()
    assert "dry-run" in capsys.readouterr().out.lower()

    args = argparse.Namespace(event_retention_days=30, log_retention_days=30, dry_run=False)
    assert kanban_ops._cmd_gc(args) == 0
    assert not wt.exists()
    assert "GC complete" in capsys.readouterr().out
