"""Tests for worktree workspace teardown at task completion/archive.

Covers the ownership gap where kanban ``worktree`` workspaces were never
reaped by anything: ``_cleanup_workspace`` preserved them by design, the CLI
startup pruner explicitly skips ``t_*`` worktrees ("dispatcher-driven
lifecycle"), and ``kanban gc`` only swept scratch. A completed or archived
task's linked worktree is now removed when — and only when — it provably
holds no work: clean working tree and every commit reachable from a
remote-tracking ref. Any doubt preserves the worktree.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_connect as kbc


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


def test_cleanup_leaves_a_worktree_cwd_before_removal(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows cannot remove a worktree that is the process's current directory."""
    wt = _make_worktree(repo, "t_cwd112425")
    real_git = kbw._git

    def windows_git(repo_root: Path, *args: str, timeout: int) -> subprocess.CompletedProcess:
        if args[:2] == ("worktree", "remove") and Path.cwd().is_relative_to(wt):
            return subprocess.CompletedProcess(
                ["git", *args], 1, stderr="Permission denied: current directory"
            )
        return real_git(repo_root, *args, timeout=timeout)

    monkeypatch.setattr(kbw, "_git", windows_git)
    monkeypatch.chdir(wt)
    kbw._cleanup_worktree_workspace("t_cwd112425", str(wt))

    assert Path.cwd() == repo
    assert not wt.exists()


def test_cleanup_proceeds_when_cwd_was_deleted(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred parent cleanup (#33774) runs after the child's scratch cwd was
    rmtree'd; a dead cwd must not preserve a clean, pushed worktree."""
    wt = _make_worktree(repo, "t_deadcwd113073")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    scratch.rmdir()

    kbw._cleanup_worktree_workspace("t_deadcwd113073", str(wt))

    assert not wt.exists()
    assert not _branch_exists(repo, "wt/t_deadcwd113073")


def test_cleanup_retries_worktree_removal_once(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brief Windows directory-handle delay gets one safe retry."""
    wt = _make_worktree(repo, "t_retry112425")
    real_git = kbw._git
    attempts = 0

    def delayed_remove(repo_root: Path, *args: str, timeout: int) -> subprocess.CompletedProcess:
        nonlocal attempts
        if args[:2] == ("worktree", "remove"):
            attempts += 1
            if attempts == 1:
                return subprocess.CompletedProcess(
                    ["git", *args], 1, stderr="Permission denied: handle pending"
                )
        return real_git(repo_root, *args, timeout=timeout)

    monkeypatch.setattr(kbw, "_git", delayed_remove)
    monkeypatch.setattr(kbw.time, "sleep", lambda _delay: None)
    kbw._cleanup_worktree_workspace("t_retry112425", str(wt))

    assert attempts == 2
    assert not wt.exists()


def _archives_for(home: Path, task_id: str) -> list[Path]:
    root = home / "archive" / "worktree-prune"
    return sorted(root.glob(f"{task_id}-*")) if root.is_dir() else []


def test_untracked_only_worktree_archived_then_removed(
    kanban_home: Path, repo: Path
) -> None:
    """An UNTRACKED-only tree is reclaimed: its files are archived, then the
    tree goes. Preserve-in-place is what grew the corpus .worktrees to 2.9 GB —
    an agent's half-written scratch files are not a reason to leak a checkout
    forever, they are a reason to copy them out first.
    """
    wt = _make_worktree(repo, "t_bbbb2222")
    (wt / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    kbw._cleanup_worktree_workspace("t_bbbb2222", str(wt))

    assert not wt.exists()
    archives = _archives_for(kanban_home, "t_bbbb2222")
    assert archives, "the untracked file must be archived, never silently discarded"
    assert (archives[0] / "wip.txt").read_text(encoding="utf-8") == "uncommitted\n"


def test_untracked_path_with_space_archived_then_removed(
    kanban_home: Path, repo: Path
) -> None:
    """A path holding a SPACE must survive the round trip.

    ``git status --porcelain`` C-quotes such a path (``"qa notes.md"``), so the
    old line-slice produced a rel-path that never joined back onto the tree:
    the archive came out empty, nothing was cleared, and ``worktree remove``
    failed on the still-dirty tree — every such card leaked its checkout
    forever.
    """
    wt = _make_worktree(repo, "t_space1234")
    (wt / "qa notes.md").write_text("with a space\n", encoding="utf-8")
    kbw._cleanup_worktree_workspace("t_space1234", str(wt))

    assert not wt.exists()
    archives = _archives_for(kanban_home, "t_space1234")
    assert archives, "a spaced untracked path must be archived, not leak the tree"
    assert (archives[0] / "qa notes.md").read_text(encoding="utf-8") == "with a space\n"


def test_untracked_symlink_archived_as_link_then_removed(
    kanban_home: Path, repo: Path
) -> None:
    """A tree whose only untracked content is a SYMLINK is archived, not lost.

    The old archive loop skipped symlinks outright, so the tree was cleared
    (and removed) with the link never captured while the caller logged that
    the scratch had been preserved. The link must be recreated AS a link.
    """
    wt = _make_worktree(repo, "t_link12345")
    (wt / "target.txt").write_text("points here\n", encoding="utf-8")
    (wt / "alias").symlink_to("target.txt")
    kbw._cleanup_worktree_workspace("t_link12345", str(wt))

    assert not wt.exists()
    archives = _archives_for(kanban_home, "t_link12345")
    assert archives, "an untracked symlink must be archived, not silently dropped"
    assert (archives[0] / "alias").is_symlink()
    assert os.readlink(archives[0] / "alias") == "target.txt"


def test_tracked_modification_preserved_in_place(kanban_home: Path, repo: Path) -> None:
    """Tracked work is real work: preserve in place, do not archive-and-delete."""
    wt = _make_worktree(repo, "t_track9999")
    (wt / "README.md").write_text("edited in place\n", encoding="utf-8")
    kbw._cleanup_worktree_workspace("t_track9999", str(wt))

    assert wt.is_dir()
    assert (wt / "README.md").read_text(encoding="utf-8") == "edited in place\n"
    assert not _archives_for(kanban_home, "t_track9999")


def test_unpushed_commits_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_cccc3333")
    (wt / "work.txt").write_text("committed but not pushed\n", encoding="utf-8")
    _git("-C", str(wt), "add", "work.txt")
    _git("-C", str(wt), "commit", "-m", "local work")
    kbw._cleanup_worktree_workspace("t_cccc3333", str(wt))
    assert wt.is_dir()


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
    import cli

    wt = _make_worktree(repo, "t_gggg7777")
    (wt / "late-wip.txt").write_text("dirtied after the check\n", encoding="utf-8")
    # Pre-check lies (as if the file appeared just after it ran) — real git
    # must still refuse the removal.
    from hermes_cli import worktree_ops

    monkeypatch.setattr(worktree_ops, "_worktree_is_dirty", lambda _p, *_a, **_k: False)
    kbw._cleanup_worktree_workspace("t_gggg7777", str(wt))
    assert wt.is_dir()
    assert (wt / "late-wip.txt").exists()


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


def test_complete_task_archives_untracked_worktree(
    kanban_home: Path, repo: Path
) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")
    assert not wt.exists()
    archives = _archives_for(kanban_home, tid)
    assert archives
    assert (archives[0] / "wip.txt").exists()


def test_stub_sibling_board_does_not_block_cleanup(
    kanban_home: Path, repo: Path
) -> None:
    """A sibling board DB with no ``tasks`` table must not defer every cleanup.

    The live home carries ``boards/default`` and ``boards/_archived`` stubs with
    no ``tasks`` table. Scanning them raised ``sqlite3.Error``, which the caller
    reads as "cannot tell whether another task uses this path" — so
    ``_cleanup_workspace`` deferred for EVERY card on EVERY board and no
    worktree was ever reclaimed. 203 done/archived rows kept a live workspace.
    """
    stub = kanban_home / "kanban" / "boards" / "default" / "kanban.db"
    stub.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(stub)
    conn.execute("CREATE TABLE kanban_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        assert kbw._workspace_in_use_by_other(conn, tid, wt) is None


def test_archive_task_reaps_clean_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        assert kb.archive_task(conn, tid, reason="superseded")
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


def _set_status(conn, tid: str, status: str) -> None:
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))


def test_prune_reclaims_tree_of_terminal_card(kanban_home: Path, repo: Path) -> None:
    """`hermes worktree prune` must classify a t_<hex> tree by its card's STATUS.

    It used to skip every kanban task tree by NAME alone, so the ~177 trees
    whose cards were already done/archived could never be reclaimed by the one
    instrument an operator runs. The branch is kept on the terminal path: the
    card's PR or its landing door may still need it.
    """
    from hermes_cli import worktree_gc as gc

    with kbc.connect_closing() as conn:
        tid, _wt = _worktree_task(conn, repo)
        _set_status(conn, tid, "done")

    records = {r.name: r for r in gc.audit_worktrees(str(repo), with_sizes=False)}
    record = records[tid]
    assert record.verdict in {"reap", "reap-archive", "reap-keep-branch"}, record.reason
    assert "done" in record.reason
    assert record.verdict != "keep"


def test_prune_keeps_tree_of_live_card(kanban_home: Path, repo: Path) -> None:
    """Row existence is not enough either: a live card's tree stays."""
    from hermes_cli import worktree_gc as gc

    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _set_status(conn, tid, "running")

    records = {r.name: r for r in gc.audit_worktrees(str(repo), with_sizes=False)}
    record = records[tid]
    assert record.verdict == "keep"
    assert "running" in record.reason
    assert wt.is_dir()


def test_prune_resolves_board_from_a_profile_home(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pruner must find the SHARED board while running under a profile.

    Boards are shared across profiles by design, so ``_card_status_for_tree``
    resolves through ``kanban_db.kanban_home()`` (``HERMES_KANBAN_HOME`` else the
    Hermes root), never the profile's ``HERMES_HOME``. Resolving through the
    profile home made the pruner blind to every card: measured 0/173 resolved,
    so ``prune`` returned the base 5-reap result and never touched the 122
    reclaimable card trees. Both resolutions are asserted here — the profile
    home alone, and the profile home with an explicit board root.
    """
    from hermes_cli import worktree_gc as gc

    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _set_status(conn, tid, "done")

    profile_home = kanban_home / "profiles" / "platform-coder"
    profile_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    # 1. Profile HERMES_HOME only: the board root is still derived correctly.
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    record = {r.name: r for r in gc.audit_worktrees(str(repo), with_sizes=False)}[tid]
    assert "no board row" not in record.reason, record.reason
    assert record.verdict != "keep", record.reason

    # 2. Profile HERMES_HOME plus an explicit shared board root.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    record = {r.name: r for r in gc.audit_worktrees(str(repo), with_sizes=False)}[tid]
    assert "no board row" not in record.reason, record.reason
    assert record.verdict != "keep", record.reason
    assert wt.is_dir(), "the audit alone must never remove anything"


def _running_scratch_parent(conn, title: str = "parent") -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title)
    assert kb.claim_task(conn, tid) is not None  # -> running
    ws = Path(kbw.resolve_workspace(kb.get_task(conn, tid)))
    kbw.set_workspace_path(conn, tid, ws)
    return tid, ws


@pytest.mark.parametrize("live_status", ["running", "blocked", "review"])
def test_live_parent_scratch_survives_last_child_archived(
    kanban_home: Path, live_status: str
) -> None:
    """#133501: the deferred sweep must not reap a still-live parent's scratch.

    ``_try_cleanup_parent_workspaces`` (#33774) exists for a parent that is
    already terminal while children still need its handoff files. A parent
    still running/blocked/review owns its workspace until its own terminal
    transition, so archiving its last active child must not delete it.
    """
    with kbc.connect_closing() as conn:
        parent, ws = _running_scratch_parent(conn)
        (ws / "work.txt").write_text("in progress", encoding="utf-8")
        child = kb.create_task(conn, title="probe")
        kb.link_tasks(conn, parent, child)
        if live_status == "blocked":
            assert kb.block_task(conn, parent, reason="waiting")
        elif live_status == "review":
            assert kb.request_review(conn, parent, summary="please review", force=True)
        assert kb.archive_task(conn, child, reason="superseded")
        assert kb.get_task(conn, parent).status == live_status
    assert (ws / "work.txt").is_file()


def test_live_parent_worktree_survives_last_child_archived(
    kanban_home: Path, repo: Path
) -> None:
    """#133501, worktree flavour: a running parent's clean linked worktree —
    its worker's cwd — must not be removed because a child got archived."""
    with kbc.connect_closing() as conn:
        parent, parent_wt = _worktree_task(conn, repo, title="live parent")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        assert kb.claim_task(conn, parent, claimer="worker") is not None
        child = kb.create_task(conn, title="probe")
        kb.link_tasks(conn, parent, child)
        assert kb.archive_task(conn, child, reason="superseded")
        assert kb.get_task(conn, parent).status == "running"
    assert parent_wt.is_dir()


def test_terminal_scratch_parent_still_swept_after_last_child(
    kanban_home: Path,
) -> None:
    """#33774 unchanged by the #133501 guard: a parent that finished while a
    child was still active still gets its scratch reaped by the last child's
    completion."""
    with kbc.connect_closing() as conn:
        parent, ws = _running_scratch_parent(conn)
        (ws / "handoff.txt").write_text("x", encoding="utf-8")
        child = kb.create_task(conn, title="probe")
        kb.link_tasks(conn, parent, child)
        assert kb.complete_task(conn, parent, summary="parent done")
        assert ws.is_dir()  # live child still needs the handoff files
        assert kb.archive_task(conn, child, reason="superseded")
    assert not ws.exists()
