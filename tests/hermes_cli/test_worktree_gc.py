"""Behavior contracts for hermes_cli.worktree_gc (attended reclaim).

Each guard gets its own contract against a REAL git repo fixture (no mocks —
the entire value of these tests is exercising actual git verdicts):

- clean + fully merged tree        → reap
- untracked-only dirt              → reap-archive (files archived, then removed)
- tracked modifications            → keep, any age
- unique unpushed commits          → keep
- patch-equivalent commits (rebase/squash-merge leak) → reap
- live-locked tree                 → keep
- kanban t_<hex> tree              → keep (owned by kanban gc)
- kanban tree, card resolves       → judged by the card's STATUS (terminal → reap)
- terminal card, deletions only, head on trunk → reap-archive
- terminal card, ANY other dirt    → keep
- kanban tree with no board row    → keep, flagged ATTENTION by the printer
- branch GC: merged branch deleted, unique-commit branch kept,
  checked-out branch kept, protected names kept
- reclaim operates ONLY on the frozen audit list (concurrent-session trap)
"""

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli import worktree_gc


def _git(args, cwd, env=None):
    e = dict(os.environ)
    e.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    if env:
        e.update(env)
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=str(cwd), env=e,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """origin (bare) + clone with .worktrees/, HOME redirected for archives."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(["init", "--bare", "-b", "main"], origin)

    clone = tmp_path / "repo"
    _git(["clone", str(origin), str(clone)], tmp_path)
    (clone / "README.md").write_text("hello\n")
    _git(["add", "."], clone)
    _git(["commit", "-m", "init"], clone)
    _git(["push", "origin", "main"], clone)
    # origin/HEAD so upstream resolution works like a real clone.
    _git(["remote", "set-head", "origin", "main"], clone)
    (clone / ".worktrees").mkdir()
    return clone


@pytest.fixture
def local_repo(tmp_path, monkeypatch):
    """``git init`` repo with NO remote at all (the #111895 shape), HOME redirected."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = tmp_path / "local-repo"
    _git(["init", "-b", "main", str(repo)], tmp_path)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "init"], repo)
    (repo / ".worktrees").mkdir()
    return repo


def _add_worktree(repo_path, name, branch=None):
    tree = repo_path / ".worktrees" / name
    branch = branch or f"hermes/{name}"
    _git(["worktree", "add", str(tree), "-b", branch], repo_path)
    return tree, branch


def _verdict(records, name):
    match = [record for record in records if record.name == name]
    assert match, f"no record for {name}"
    return match[0]


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A throwaway kanban board root (``HERMES_KANBAN_HOME``) with a ``tasks`` table.

    Card resolution goes through ``kanban_db.kanban_home()``, which honours
    ``HERMES_KANBAN_HOME``, so this keeps the machine's real boards — and their
    ~thousands of cards — out of every verdict exercised here. The returned
    callable sets a card's status; an id never passed to it has NO board row.
    """
    home = tmp_path / "kanban-home"
    home.mkdir()
    db = home / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))

    def _set(task_id: str, status: str) -> None:
        conn = sqlite3.connect(db)
        with conn:
            conn.execute("INSERT OR REPLACE INTO tasks (id, status) VALUES (?, ?)",
                         (task_id, status))
        conn.close()

    return _set


class TestAuditVerdicts:
    def test_clean_merged_tree_reaps(self, repo):
        _add_worktree(repo, "hermes-clean")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        assert _verdict(records, "hermes-clean").verdict == "reap"

    def test_tracked_modifications_keep(self, repo):
        tree, _ = _add_worktree(repo, "hermes-dirty")
        (tree / "README.md").write_text("edited\n")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "hermes-dirty")
        assert record.verdict == "keep"
        assert "tracked" in record.reason

    def test_untracked_only_is_reap_archive(self, repo):
        tree, _ = _add_worktree(repo, "hermes-scratch")
        (tree / "PR_BODY_DRAFT.md").write_text("draft\n")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "hermes-scratch")
        assert record.verdict == "reap-archive"
        assert record.untracked == ["PR_BODY_DRAFT.md"]

    def test_unique_unpushed_commits_keep(self, repo):
        tree, _ = _add_worktree(repo, "hermes-work")
        (tree / "new.py").write_text("x = 1\n")
        _git(["add", "."], tree)
        _git(["commit", "-m", "unique work"], tree)
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "hermes-work")
        assert record.verdict == "keep"
        assert "unpushed" in record.reason

    def test_unique_commits_without_a_remote_keep_tree_and_branch(self, local_repo):
        """No remote: unique commits are judged against the local trunk, never "merged/pushed"."""
        tree, branch = _add_worktree(local_repo, "hermes-local-work")
        (tree / "new.py").write_text("x = 1\n")
        _git(["add", "."], tree)
        _git(["commit", "-m", "unique local work"], tree)

        records = worktree_gc.audit_worktrees(str(local_repo), with_sizes=False)
        record = _verdict(records, "hermes-local-work")
        assert record.verdict == "keep"
        assert "unpushed" in record.reason
        assert worktree_gc.reclaim_worktrees(str(local_repo), records=records) == []
        assert tree.exists()
        assert _git(["rev-parse", "--verify", branch], local_repo)

    def test_without_a_remote_local_trunk_is_the_baseline(self, local_repo):
        """Control for the no-remote guard: work merged into the local ``main`` still reclaims
        (tree and squash-merged branch), while a branch holding unique commits — with or without
        a worktree — is reported and kept by the branch audit instead of vanishing."""
        clean_tree, _ = _add_worktree(local_repo, "hermes-clean")
        squashed_tree, _ = _add_worktree(local_repo, "hermes-squashed")
        (squashed_tree / "k.py").write_text("k = 1\n")
        _git(["add", "."], squashed_tree)
        _git(["commit", "-m", "squash me"], squashed_tree)
        _git(["merge", "--squash", "hermes/hermes-squashed"], local_repo)
        _git(["commit", "-m", "squash merge"], local_repo)
        _git(["branch", "orphan-unique", "main"], local_repo)
        _git(["checkout", "-q", "orphan-unique"], local_repo)
        (local_repo / "o.py").write_text("o = 1\n")
        _git(["add", "."], local_repo)
        _git(["commit", "-m", "unique, no worktree"], local_repo)
        _git(["checkout", "-q", "main"], local_repo)

        records = worktree_gc.audit_worktrees(str(local_repo), with_sizes=False)
        assert _verdict(records, "hermes-clean").verdict == "reap"
        assert _verdict(records, "hermes-squashed").verdict == "reap"
        worktree_gc.reclaim_worktrees(str(local_repo), records=records)
        assert not clean_tree.exists() and not squashed_tree.exists()

        by_name = {b.name: b for b in worktree_gc.audit_branches(str(local_repo))}
        assert by_name["orphan-unique"].verdict == "keep"
        assert "unique" in by_name["orphan-unique"].reason
        assert by_name["main"].verdict == "keep"
        assert "hermes/hermes-squashed" not in by_name  # branch -D ran only on merged work

    def test_patch_equivalent_commits_reap(self, repo):
        """The squash/rebase-merge leak: local commit unreachable from any
        remote ref but patch-equivalent to an upstream commit → merged work."""
        tree, _ = _add_worktree(repo, "hermes-merged")
        (tree / "feat.py").write_text("y = 2\n")
        _git(["add", "."], tree)
        _git(["commit", "-m", "feat"], tree)
        sha = _git(["rev-parse", "HEAD"], tree)
        # "Merge" it to main with a DIFFERENT committer so the cherry-pick
        # produces a distinct sha (same-second identical-committer cherry
        # picks can produce the identical sha — pitfall from the skill).
        _git(["cherry-pick", sha], repo,
             env={"GIT_COMMITTER_NAME": "other", "GIT_COMMITTER_EMAIL": "o@o"})
        _git(["push", "origin", "main"], repo)
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        assert _verdict(records, "hermes-merged").verdict == "reap"

    def test_live_locked_tree_keeps(self, repo):
        tree, _ = _add_worktree(repo, "hermes-live")
        _git(["worktree", "lock", str(tree),
              "--reason", f"hermes pid={os.getpid()}"], repo)
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "hermes-live")
        assert record.verdict == "keep"
        assert "in use" in record.reason

    def test_kanban_tree_untouched(self, repo, board):
        _add_worktree(repo, "t_deadbeef", branch="kanban/t_deadbeef")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "t_deadbeef")
        assert record.verdict == "keep"
        assert "kanban" in record.reason
        # No row on this board: kept AND flagged instead of silently "preserved".
        assert record.attention == worktree_gc._ATTENTION_NO_BOARD_ROW


class TestReclaim:
    def test_reap_removes_tree_and_branch(self, repo):
        tree, branch = _add_worktree(repo, "hermes-clean")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        actions = worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert any("removed hermes-clean" in a for a in actions)
        assert not tree.exists()
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", branch],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert probe.returncode != 0, "branch should be gone with its tree"

    def test_untracked_files_archived_under_the_active_profile_home(self, repo, tmp_path, monkeypatch):
        """The archive follows the active Hermes home (a named profile here), never ~/.hermes."""
        native_home = tmp_path / "native"
        profile_home = tmp_path / "root" / "profiles" / "work"
        profile_home.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: native_home)
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        tree, _ = _add_worktree(repo, "hermes-scratch")
        (tree / "NOTES.md").write_text("important scribbles\n")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert not tree.exists()
        archived = list((profile_home / "archive" / "worktree-prune").rglob("NOTES.md"))
        assert archived, "untracked file must be archived under the profile home, not destroyed"
        assert archived[0].read_text() == "important scribbles\n"
        assert not (native_home / ".hermes").exists()

    def test_dry_run_changes_nothing(self, repo):
        tree, _ = _add_worktree(repo, "hermes-clean")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        actions = worktree_gc.reclaim_worktrees(
            str(repo), dry_run=True, records=records
        )
        assert any("would remove" in a for a in actions)
        assert tree.exists()

    def test_frozen_list_ignores_trees_created_after_audit(self, repo):
        """Concurrent-session trap: a tree created between audit and reclaim
        must be out of scope by construction."""
        _add_worktree(repo, "hermes-old")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        late_tree, _ = _add_worktree(repo, "hermes-late")
        worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert late_tree.exists(), "tree created after the audit must survive"

    def test_dead_locked_tree_is_unlocked_and_reaped(self, repo):
        tree, _ = _add_worktree(repo, "hermes-zombie")
        _git(["worktree", "lock", str(tree),
              "--reason", "hermes pid=999999999"], repo)
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        assert _verdict(records, "hermes-zombie").verdict == "reap"
        worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert not tree.exists()


class TestBranchGC:
    def test_merged_branch_deleted_any_name(self, repo):
        """Branch GC is content-gated, not name-gated: any fully-merged local
        branch is safe to delete regardless of prefix."""
        _git(["branch", "salv-12345", "main"], repo)
        _git(["branch", "feat/some-old-thing", "main"], repo)
        records = worktree_gc.audit_branches(str(repo))
        by_name = {record.name: record for record in records}
        assert by_name["salv-12345"].verdict == "delete"
        assert by_name["feat/some-old-thing"].verdict == "delete"
        worktree_gc.reclaim_branches(str(repo), records=records)
        out = _git(["branch", "--format=%(refname:short)"], repo)
        assert "salv-12345" not in out
        assert "feat/some-old-thing" not in out

    def test_unique_commit_branch_kept(self, repo):
        _git(["checkout", "-b", "feat/real-work"], repo)
        (repo / "wip.py").write_text("z = 3\n")
        _git(["add", "."], repo)
        _git(["commit", "-m", "wip"], repo)
        _git(["checkout", "main"], repo)
        records = worktree_gc.audit_branches(str(repo))
        by_name = {record.name: record for record in records}
        assert by_name["feat/real-work"].verdict == "keep"
        assert "unique" in by_name["feat/real-work"].reason

    def test_patch_equivalent_branch_deleted(self, repo):
        """Rebase-merged PR branch: SHAs differ from main but every commit is
        patch-equivalent — the dominant branch leak."""
        _git(["checkout", "-b", "fix/landed"], repo)
        (repo / "fix.py").write_text("a = 4\n")
        _git(["add", "."], repo)
        _git(["commit", "-m", "fix"], repo)
        sha = _git(["rev-parse", "HEAD"], repo)
        _git(["checkout", "main"], repo)
        _git(["cherry-pick", sha], repo,
             env={"GIT_COMMITTER_NAME": "other", "GIT_COMMITTER_EMAIL": "o@o"})
        _git(["push", "origin", "main"], repo)
        records = worktree_gc.audit_branches(str(repo))
        by_name = {record.name: record for record in records}
        assert by_name["fix/landed"].verdict == "delete"
        assert "patch-equivalent" in by_name["fix/landed"].reason

    def test_checked_out_and_protected_kept(self, repo):
        _tree, branch = _add_worktree(repo, "hermes-active")
        records = worktree_gc.audit_branches(str(repo))
        by_name = {record.name: record for record in records}
        assert by_name["main"].verdict == "keep"
        assert by_name[branch].verdict == "keep"


class TestOlderThanGate:
    def test_young_reapable_tree_kept_under_older_than(self, repo):
        _add_worktree(repo, "hermes-young")
        records = worktree_gc.audit_worktrees(
            str(repo), with_sizes=False, older_than_days=7,
        )
        record = _verdict(records, "hermes-young")
        assert record.verdict == "keep"
        assert "older-than" in record.reason

    def test_aged_reapable_tree_still_reaps(self, repo):
        import os as _os
        import time as _time

        tree, _ = _add_worktree(repo, "hermes-old")
        old = _time.time() - 10 * 86400
        _os.utime(tree, (old, old))
        records = worktree_gc.audit_worktrees(
            str(repo), with_sizes=False, older_than_days=7,
        )
        assert _verdict(records, "hermes-old").verdict == "reap"

    def test_older_than_never_widens_eligibility(self, repo):
        """A tree with real work stays keep at ANY age — the age gate only
        restricts, it can never doom unmerged/dirty work."""
        import os as _os
        import time as _time

        tree, _ = _add_worktree(repo, "hermes-old-work")
        (tree / "README.md").write_text("edited\n")
        old = _time.time() - 30 * 86400
        _os.utime(tree, (old, old))
        records = worktree_gc.audit_worktrees(
            str(repo), with_sizes=False, older_than_days=7,
        )
        record = _verdict(records, "hermes-old-work")
        assert record.verdict == "keep"
        assert "tracked" in record.reason


class TestExternalTrees:
    def test_external_tree_reported_never_reaped(self, repo, tmp_path):
        ext = tmp_path / "elsewhere-tree"
        _git(["worktree", "add", str(ext), "-b", "ext/branch"], repo)
        (ext / "WIP.txt").write_text("outside work\n")

        external = worktree_gc.audit_external_trees(str(repo))
        paths = [record.path for record in external]
        assert any("elsewhere-tree" in p for p in paths)
        record = next(r for r in external if "elsewhere-tree" in r.path)
        assert record.branch == "ext/branch"
        assert not record.missing

        # The managed audit + reclaim never see or touch it.
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        assert all("elsewhere-tree" not in r.name for r in records)
        worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert ext.exists() and (ext / "WIP.txt").exists()

    def test_managed_trees_not_reported_as_external(self, repo):
        _add_worktree(repo, "hermes-managed")
        external = worktree_gc.audit_external_trees(str(repo))
        assert all("hermes-managed" not in r.path for r in external)

    def test_missing_registration_flagged_and_pruned(self, repo, tmp_path):
        import shutil as _shutil

        ext = tmp_path / "vanished-tree"
        _git(["worktree", "add", str(ext), "-b", "ext/vanished"], repo)
        _shutil.rmtree(ext)

        external = worktree_gc.audit_external_trees(str(repo))
        record = next(r for r in external if "vanished-tree" in r.path)
        assert record.missing

        planned = worktree_gc.prune_missing_registrations(str(repo), dry_run=True)
        assert any("vanished-tree" in line for line in planned)
        # Dry-run changed nothing.
        assert any(
            r.missing for r in worktree_gc.audit_external_trees(str(repo))
        )

        done = worktree_gc.prune_missing_registrations(str(repo))
        assert any("pruned" in line for line in done)
        assert all(
            "vanished-tree" not in r.path
            for r in worktree_gc.audit_external_trees(str(repo))
        )


class TestTerminalCardReclaim:
    """A t_<hex> tree is judged by its card's STATUS, and a terminal card only
    reclaims work the repo already has: deletions over a head that is on the
    trunk. Everything else dirty stays put — a done card is not a licence to
    throw away unmerged work."""

    def _deleted(self, repo, name, branch):
        tree, branch = _add_worktree(repo, name, branch=branch)
        (tree / "README.md").unlink()
        return tree, branch

    def test_deletions_only_on_a_done_card_reaps_archive(self, repo, board):
        tree, branch = self._deleted(repo, "t_abcdef", "kanban/t_abcdef")
        board("t_abcdef", "done")

        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "t_abcdef")
        assert record.verdict == "reap-archive", record.reason
        assert "card done" in record.reason
        assert "deletions" in record.reason
        assert not record.attention

        actions = worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert any("removed t_abcdef" in a for a in actions), actions
        assert any("deletions" in a for a in actions), "the action line must name what it removed"
        assert not tree.exists()
        # reap-archive semantics: the branch is fully on the trunk, so it goes too.
        assert _git(["branch", "--list", branch], repo).strip() == ""

    def test_modification_on_a_done_card_stays(self, repo, board):
        tree, _ = _add_worktree(repo, "t_abc123", branch="kanban/t_abc123")
        (tree / "README.md").write_text("edited\n")
        board("t_abc123", "done")

        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "t_abc123")
        assert record.verdict == "keep"
        assert "tracked" in record.reason
        worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert tree.exists()

    def test_deletions_with_an_unpublished_head_stay(self, repo, board):
        """Deleting a file loses nothing only when HEAD is already on the trunk."""
        tree, _ = _add_worktree(repo, "t_abc456", branch="kanban/t_abc456")
        (tree / "work.py").write_text("w = 1\n")
        _git(["add", "."], tree)
        _git(["commit", "-m", "unpublished work"], tree)
        (tree / "README.md").unlink()
        board("t_abc456", "done")

        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "t_abc456")
        assert record.verdict == "keep", record.reason
        worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert tree.exists()

    def test_deletion_plus_untracked_dirt_stays(self, repo, board):
        """`??` is real work too: only a pure-D status qualifies."""
        tree, _ = _add_worktree(repo, "t_abc789", branch="kanban/t_abc789")
        (tree / "README.md").unlink()
        (tree / "NOTES.md").write_text("scribbles\n")
        board("t_abc789", "done")

        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        assert _verdict(records, "t_abc789").verdict == "keep"
        worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert tree.exists() and (tree / "NOTES.md").exists()

    def test_renamed_file_is_real_work(self, repo, board):
        tree, _ = _add_worktree(repo, "t_abc321", branch="kanban/t_abc321")
        _git(["mv", "README.md", "RENAMED.md"], tree)
        board("t_abc321", "done")

        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        assert _verdict(records, "t_abc321").verdict == "keep"

    def test_deletions_only_parses_paths_with_spaces(self, repo):
        """`-z` records are NUL-separated: a space in a path is not a field break."""
        (repo / "two words.py").write_text("x = 1\n")
        _git(["add", "."], repo)
        _git(["commit", "-m", "add a spaced path"], repo)
        tree, _ = _add_worktree(repo, "hermes-spaces")

        assert worktree_gc._deletions_only(str(tree)) is False, "a clean tree is not deletions"
        (tree / "two words.py").unlink()
        (tree / "README.md").unlink()
        assert worktree_gc._deletions_only(str(tree)) is True

    def test_deletions_on_a_live_card_stay(self, repo, board):
        tree, _ = self._deleted(repo, "t_11fe01", "kanban/t_11fe01")
        board("t_11fe01", "running")

        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "t_11fe01")
        assert record.verdict == "keep"
        assert "running" in record.reason
        assert tree.exists()


class TestNoBoardRowAttention:
    """D1: a card tree the board cannot resolve is REACHABLE — kept, and named
    in its own ATTENTION section instead of vanishing into "preserved"."""

    def test_no_board_row_tree_is_kept_and_flagged(self, repo, board):
        tree, _ = _add_worktree(repo, "t_0badc0de", branch="hermes/t_0badc0de")
        board("t_somethingelse", "done")  # a board exists; this card is not on it

        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "t_0badc0de")
        assert record.verdict == "keep"
        assert "no board row" in record.reason
        assert record.attention == worktree_gc._ATTENTION_NO_BOARD_ROW

        worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert tree.exists(), "an unresolvable card is never a licence to delete"

    def test_resolved_card_carries_no_attention(self, repo, board):
        _add_worktree(repo, "t_abc999", branch="kanban/t_abc999")
        board("t_abc999", "done")

        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        assert _verdict(records, "t_abc999").attention == ""


class TestCmdWorktreeJson:
    def _ns(self, repo, action, **kw):
        import argparse

        return argparse.Namespace(
            repo=str(repo), worktree_action=action,
            json=kw.get("json", True), older_than=kw.get("older_than"),
            dry_run=kw.get("dry_run", False),
            trees_only=kw.get("trees_only", False),
            branches_only=kw.get("branches_only", False),
        )

    def test_list_json_shape(self, repo, capsys):
        import json

        from hermes_cli.worktree_cmd import cmd_worktree

        _add_worktree(repo, "hermes-json")
        assert cmd_worktree(self._ns(repo, "list")) == 0
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {"repo", "trees", "external_trees", "branches"}
        names = [t["name"] for t in payload["trees"]]
        assert "hermes-json" in names
        tree = next(t for t in payload["trees"] if t["name"] == "hermes-json")
        assert {"verdict", "reason", "age_days", "branch"} <= set(tree)

    def test_prune_dry_run_json(self, repo, capsys):
        import json

        from hermes_cli.worktree_cmd import cmd_worktree

        _add_worktree(repo, "hermes-json-prune")
        assert cmd_worktree(self._ns(repo, "prune", dry_run=True)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True
        assert any("hermes-json-prune" in a for a in payload["actions"])
        # dry-run: tree still present
        assert (repo / ".worktrees" / "hermes-json-prune").exists()

    def test_negative_older_than_rejected(self, repo, capsys):
        from hermes_cli.worktree_cmd import cmd_worktree

        assert cmd_worktree(self._ns(repo, "prune", older_than=-1)) == 1

    def test_prune_prints_an_attention_section_for_unresolvable_cards(self, repo, board, capsys):
        """The 'no board row' class gets its own section — it is not 'preserved'
        and it is not silent."""
        from hermes_cli.worktree_cmd import cmd_worktree

        _add_worktree(repo, "t_0badc0de", branch="hermes/t_0badc0de")
        _add_worktree(repo, "hermes-plain")
        board("t_unrelated", "done")

        assert cmd_worktree(self._ns(repo, "prune", json=False, dry_run=True,
                                     trees_only=True)) == 0
        out = capsys.readouterr().out
        attention_block = out.split("ATTENTION", 1)
        assert len(attention_block) == 2, out
        assert "1 tree(s)" in attention_block[1].splitlines()[0]
        assert "t_0badc0de" in attention_block[1]
        # Not folded into the preserved list, and not reaped.
        preserved = out.split("Preserved", 1)[1].split("ATTENTION", 1)[0] if "Preserved" in out else ""
        assert "t_0badc0de" not in preserved
        assert (repo / ".worktrees" / "t_0badc0de").exists()

    def test_prune_json_carries_attention(self, repo, board, capsys):
        import json

        from hermes_cli.worktree_cmd import cmd_worktree

        _add_worktree(repo, "t_0badc0de", branch="hermes/t_0badc0de")
        board("t_unrelated", "done")

        assert cmd_worktree(self._ns(repo, "prune", dry_run=True)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert [a["name"] for a in payload["attention"]] == ["t_0badc0de"]
        assert payload["attention"][0]["attention"] == "no-board-row"
        assert all(p["name"] != "t_0badc0de" for p in payload["preserved"])
