"""On-demand worktree + branch reclaim (``hermes worktree`` / ``/worktree prune``).

The startup pruner (``cli._prune_stale_worktrees``) is conservative and silent — clean, fully
merged scratch past an age tier only. This module also reclaims trees whose only "dirt" is
untracked scratch (archived first) and branches whose content is on upstream.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Branches never considered for deletion, in any mode.
_PROTECTED_BRANCHES = {"main", "master", "develop", "dev", "trunk"}

# Trees owned by another lifecycle (kanban dispatcher gc) — never touched.
_KANBAN_RE = re.compile(r"^t_[0-9a-f]+$")

# Bounded cherry probe: a branch this far ahead of upstream is a stale-base
# lane, not merged scratch; checking it is expensive and it stays preserved.
_MAX_CHERRY_AHEAD = 50


@dataclass
class TreeRecord:
    name: str
    path: str
    branch: str
    age_days: float
    size_mb: Optional[int]
    verdict: str          # reap | reap-archive | reap-keep-branch | keep
    reason: str
    untracked: list[str] = field(default_factory=list)
    # A keep the sweep cannot justify on its own: the record needs a human
    # decision, so every printer must show it instead of folding it into
    # "preserved" (see ``_ATTENTION_NO_BOARD_ROW``).
    attention: str = ""


@dataclass
class BranchRecord:
    name: str
    verdict: str          # delete | keep
    reason: str


def _run(cmd: list, timeout: int, cwd: Optional[str] = None,
         env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=cwd, env=env, stdin=subprocess.DEVNULL)


@dataclass
class ExternalTreeRecord:
    """A linked worktree registered on the repo but living OUTSIDE
    ``.worktrees/`` — created by hand or by another tool. Reported for
    visibility only; the reclaim paths never touch these."""

    path: str
    branch: str           # branch name, or "detached @<sha>" when detached
    locked: bool
    missing: bool         # registered but the directory no longer exists


def _git(args: list, cwd: str, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run git, translating timeouts into returncode 124. Every verdict fails safe toward "keep"
    on nonzero, so a slow ``git cherry`` on a huge repo degrades to keep instead of aborting the
    audit mid-list. :func:`noninteractive_repo_git_env` because ``status`` executes the repo's
    ``core.fsmonitor`` and clean filters (GHSA-7x36-8jrh-v4pw)."""
    from hermes_cli._subprocess_compat import FILTER_DISCOVERY_FAILED, noninteractive_repo_git_env
    env = noninteractive_repo_git_env(cwd)
    if env is None:
        return subprocess.CompletedProcess(args=["git", *args], returncode=1, stdout="",
                                           stderr=FILTER_DISCOVERY_FAILED)
    try:
        return _run(["git", *args], timeout, cwd, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args=["git", *args], returncode=124, stdout="",
                                           stderr=f"timeout after {timeout}s")


def _tree_size_mb(path: Path, timeout: int = 30) -> Optional[int]:
    """Cheap directory size via ``du -sm`` — best-effort, None on failure."""
    try:
        result = _run(["du", "-sm", str(path)], timeout)
        return int(result.stdout.split()[0]) if result.returncode == 0 and result.stdout.strip() else None
    except Exception:
        return None


def _dirty_split(path: str) -> tuple[bool, list[str]]:
    """(has_tracked_modifications, untracked_paths) — tracked = real work, untracked = archivable.

    ``-z`` plus ``core.quotePath=false``: the human-readable form C-quotes every
    path holding a space or a non-ASCII byte, so ``qa notes.md`` arrives as
    ``"qa notes.md"``. Slicing that record yielded a rel-path that never joined
    back onto the tree, the archive came out empty, and the tree leaked forever
    with the log claiming its scratch had been preserved. Under ``-z`` each
    record is ``XY <path>`` NUL-terminated, and a rename/copy carries its
    ORIGINAL path as one extra NUL-separated field.
    """
    try:
        result = _git(["-c", "core.quotePath=false", "status", "--porcelain", "-z"],
                      cwd=path, timeout=10)
        if result.returncode != 0:
            return True, []  # fail safe: treat as real work
        fields = result.stdout.split("\0")
        untracked: list[str] = []
        tracked = 0
        index = 0
        while index < len(fields):
            record = fields[index]
            index += 1
            if not record:
                continue
            code, name = record[:2], record[3:]
            if "R" in code or "C" in code:
                index += 1  # the rename/copy source follows as its own field
            if code == "??":
                untracked.append(name)
            else:
                tracked += 1
        return tracked > 0, untracked
    except Exception:
        return True, []


def _deletions_only(path: str) -> bool:
    """True when EVERY local change in *path* is a tracked DELETION (porcelain ``D`` only).

    A deletion removes content that still exists on the trunk, so it is the one
    kind of tracked dirt a tree can be reclaimed over without losing work. Any
    modification, addition, rename, copy or untracked path is real work. Parsed
    exactly like :func:`_dirty_split` (``-z``, ``core.quotePath=false``) so a path
    holding a space cannot be misread, and it fails SAFE toward False: an
    unreadable status is never a licence to delete a tree.
    """
    try:
        result = _git(["-c", "core.quotePath=false", "status", "--porcelain", "-z"],
                      cwd=path, timeout=10)
        if result.returncode != 0:
            return False
        fields = result.stdout.split("\0")
        seen = False
        index = 0
        while index < len(fields):
            record = fields[index]
            index += 1
            if not record:
                continue
            code = record[:2]
            if "R" in code or "C" in code:
                index += 1  # the rename/copy source follows as its own field
            if "D" not in code or not set(code) <= {"D", " "}:
                return False  # M / A / R / C / ?? — real work
            seen = True
        return seen
    except Exception:
        return False


def _head_on_trunk(_ops, path: str, timeout: int = 10) -> Optional[str]:
    """The ref whose tip already CONTAINS HEAD, or None when HEAD is unpublished.

    The other half of the deletions-only gate: a deletion loses nothing only if
    the tree's commits are already on the trunk. Judged against the same ref
    merged work is judged against (``_worktree_merge_base_ref``) and by ancestry,
    so a branch carrying even one commit the trunk lacks is not eligible. No
    comparable ref -> None (fail safe: nothing is reaped).
    """
    try:
        trunk = _ops._worktree_merge_base_ref(path, timeout=5)
    except Exception:
        return None
    if not trunk:
        return None
    try:
        result = _git(["merge-base", "--is-ancestor", "HEAD", trunk], cwd=path, timeout=timeout)
    except Exception:
        return None
    return trunk if result.returncode == 0 else None


def _archive_untracked(tree: Path, untracked: list[str]) -> Optional[Path]:
    """Copy untracked files out of a doomed tree; None on any failure (caller must then keep).

    Symlinks are archived AS LINKS. Skipping them (the old behaviour) let a tree
    whose only untracked content was a symlink be deleted with the link never
    captured, while the caller logged that the scratch had been preserved.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    from hermes_constants import get_hermes_home
    dest = get_hermes_home() / "archive" / "worktree-prune" / f"{tree.name}-{stamp}"
    try:
        for rel in untracked:
            src = tree / rel
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                if target.is_symlink() or target.exists():
                    target.unlink()
                os.symlink(os.readlink(src), target)
            elif src.is_dir():
                shutil.copytree(src, target, symlinks=True, dirs_exist_ok=True)
            elif src.exists():
                shutil.copy2(src, target)
            # else: the path vanished between `status` and now — nothing to preserve
        return dest
    except Exception as exc:
        logger.warning("Could not archive untracked files from %s: %s", tree, exc)
        return None


_TERMINAL_CARD_STATUSES = frozenset({"done", "archived", "cancelled", "failed"})

# A t_<hex> tree the board cannot resolve: kept (the card may live on a board
# this sweep cannot see — deleting on a missing row destroys real work), but
# flagged so no printer can fold it into "preserved" and lose it again.
_ATTENTION_NO_BOARD_ROW = "no-board-row"
_NO_BOARD_ROW_REASON = "kanban task tree; no board row for this card"


def _card_status_for_tree(task_id: str) -> Optional[str]:
    """Board status of *task_id*, or None when no board store knows the card.

    Resolved through :func:`hermes_cli.kanban_db.kanban_home` — the board root,
    which honours ``HERMES_KANBAN_HOME`` and ``get_default_hermes_root()`` —
    NEVER the active profile's ``HERMES_HOME``. Boards are shared across
    profiles by design, so resolving through the profile home made the pruner
    blind to every card whenever it ran under ``HERMES_HOME=<profile>``:
    measured 0/173 cards resolved that way, versus 122 reap / 57 keep (with no
    live-card tree reaped) against the board root.

    Reads every board store read-only: the root ``kanban.db``, the legacy
    ``kanban/kanban.db`` and every ``kanban/boards/<slug>/kanban.db``. A store
    with no ``tasks`` table is skipped — it cannot hold the card, and treating
    such a stub as unreadable is what made the completion path defer every
    cleanup. Any board reporting a NON-terminal status wins: conservative.
    """
    try:
        from hermes_cli import kanban_db as _kb  # late: import cycle

        home = _kb.kanban_home()
        boards = _kb.boards_root()
    except Exception:
        return None
    candidates = [home / "kanban.db", home / "kanban" / "kanban.db"]
    if boards.is_dir():
        with contextlib.suppress(OSError):
            candidates.extend(sorted(p / "kanban.db" for p in boards.iterdir() if p.is_dir()))
    statuses: list[str] = []
    seen: set[str] = set()
    for db in candidates:
        if not db.is_file():
            continue
        try:
            key = str(db.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=1.0)
            try:
                row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            finally:
                conn.close()
        except Exception:
            continue
        if row and row[0]:
            statuses.append(str(row[0]))
    if not statuses:
        return None
    for status in statuses:
        if status not in _TERMINAL_CARD_STATUSES:
            return status
    return statuses[0]


def _classify_tree(_ops, repo_root: str, entry: Path, merge_cache, remote_heads) -> tuple[str, str, list[str]]:
    """Return (verdict, reason, untracked) for one tree under ``.worktrees/``.

    A ``t_<hex>`` tree belongs to a card, and the board's STATUS for that card
    — not the existence of a row — decides whether it may be reclaimed. A
    terminal card's tree is judged by the ordinary dirty/pushed rules below,
    with two refinements: a clean tree keeps its branch (the card's PR or its
    landing door may still need it), while a tree whose only local change is a
    DELETION over a head already on the trunk is ``reap-archive``. A tree whose
    card resolves to NO row is kept and flagged for attention: the row may
    simply be on a board this sweep cannot see, so a human decides.
    """
    kanban_status: Optional[str] = None
    if _KANBAN_RE.match(entry.name):
        kanban_status = _card_status_for_tree(entry.name)
        if kanban_status is None:
            return "keep", _NO_BOARD_ROW_REASON, []
        if kanban_status not in _TERMINAL_CARD_STATUSES:
            return "keep", f"kanban task tree; card is {kanban_status}", []
    verdict, reason, untracked = _classify_reapable(
        _ops, repo_root, entry, merge_cache, remote_heads, card_status=kanban_status)
    if kanban_status is not None:
        reason = f"card {kanban_status}; {reason}"
        if verdict == "reap":
            return "reap-keep-branch", f"{reason} (branch kept for the card)", []
    return verdict, reason, untracked


def _classify_reapable(_ops, repo_root: str, entry: Path, merge_cache, remote_heads,
                       *, card_status: Optional[str] = None) -> tuple[str, str, list[str]]:
    """Reapability of one tree, ignoring card ownership.

    ``card_status`` (a TERMINAL card's status) unlocks one extra verdict: a tree
    whose only local change is a tracked DELETION and whose head is already on
    the trunk. Such a tree carries no work the repo does not already have — the
    deleted content is still on the trunk, and nothing is unpublished — so it is
    ``reap-archive``: untracked files archived first (there are none, by the
    rule's own shape), then the tree and its fully-merged branch go.
    Anything else dirty stays ``keep`` at any card status.
    """
    path = str(entry)
    if _ops._worktree_lock_is_live(repo_root, path, timeout=5) == "live":
        return "keep", "in use by a running hermes session", []
    tracked_dirty, untracked = _dirty_split(path)
    if tracked_dirty:
        if card_status is not None and not untracked and _deletions_only(path):
            trunk = _head_on_trunk(_ops, path)
            if trunk:
                return ("reap-archive",
                        f"only local change is deletions and the head is already on {trunk}",
                        [])
        return "keep", "uncommitted tracked changes (real work)", []
    archive_note = f"{len(untracked)} untracked file(s) will be archived"
    if _ops._worktree_has_unpushed_commits(path, timeout=5) and not _ops._worktree_commits_all_merged_upstream(
        path, timeout=30, cache=merge_cache, max_ahead=_MAX_CHERRY_AHEAD):
        # Pushed-branch tier: single-branch fetch refspecs (managed-install default) leave pushed
        # PR branches with no refs/remotes/* entry, so `git log HEAD --not --remotes` reads them
        # as unpushed forever. A head EXACTLY matching the remote branch has nothing origin lacks.
        if not _ops._worktree_branch_pushed_exact(path, remote_heads, timeout=10):
            return "keep", "unpushed commits not found upstream", []
        if untracked:
            return "reap-keep-branch", f"pushed to origin (open-PR lane); branch kept; {archive_note}", untracked
        return "reap-keep-branch", "pushed to origin (open-PR lane); branch kept", []
    if untracked:
        return "reap-archive", f"merged/pushed; {archive_note}", untracked
    return "reap", "clean and fully merged/pushed", []


def audit_external_trees(repo_root: str) -> list[ExternalTreeRecord]:
    """List linked worktrees registered OUTSIDE ``.worktrees/``.

    ``hermes -w`` scratch trees all live under ``<repo>/.worktrees/``, but
    ``git worktree list --porcelain`` also knows about trees the user (or
    another tool) registered elsewhere. Those are someone else's state, so
    the reclaim paths never touch them — but hiding them entirely makes the
    audit lie about what the repo is carrying. Report them read-only, and
    flag registrations whose directory has vanished (safe to
    ``git worktree prune``).
    """
    result = _git(["worktree", "list", "--porcelain"], cwd=repo_root, timeout=10)
    if result.returncode != 0:
        return []

    managed_root = os.path.realpath(str(Path(repo_root) / ".worktrees"))
    main_root = os.path.realpath(repo_root)

    records: list[ExternalTreeRecord] = []
    current: dict = {}

    def _flush():
        path = current.get("path")
        if not path:
            return
        real = os.path.realpath(path)
        if real == main_root:
            return  # the main checkout itself
        if real == managed_root or real.startswith(managed_root + os.sep):
            return  # hermes-managed scratch tree — covered by audit_worktrees
        branch = current.get("branch", "")
        if not branch and current.get("head"):
            branch = f"detached @{current['head'][:10]}"
        records.append(ExternalTreeRecord(
            path=path,
            branch=branch,
            locked=bool(current.get("locked")),
            missing=not os.path.exists(path),
        ))

    for line in result.stdout.splitlines():
        line = line.rstrip()
        if not line:
            _flush()
            current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree "):]
        elif line.startswith("branch refs/heads/"):
            current["branch"] = line[len("branch refs/heads/"):]
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line == "locked" or line.startswith("locked "):
            current["locked"] = True
    _flush()
    return records


def prune_missing_registrations(repo_root: str, *, dry_run: bool = False) -> list[str]:
    """Drop registrations whose directory no longer exists (any location).

    The equivalent of a targeted ``git worktree prune``: purely
    metadata-level, never removes files, so it is safe even for external
    trees — a missing directory means there is nothing left to protect.
    """
    stale = [r for r in audit_external_trees(repo_root) if r.missing and not r.locked]
    if not stale:
        return []
    if dry_run:
        return [f"would prune stale registration {r.path}" for r in stale]
    result = _git(["worktree", "prune"], cwd=repo_root, timeout=15)
    if result.returncode != 0:
        return [f"failed to prune stale registrations: {result.stderr.strip()}"]
    return [f"pruned stale registration {r.path}" for r in stale]


def audit_worktrees(repo_root: str, *, with_sizes: bool = True,
                    older_than_days: Optional[float] = None) -> list[TreeRecord]:
    """Classify every tree under ``.worktrees/`` without mutating anything.

    ``older_than_days`` only ever RESTRICTS: a reapable tree younger than the threshold is kept
    ("too recent"). It never widens eligibility — age alone can't doom a tree carrying unmerged work.
    """
    from hermes_cli import worktree_ops as _ops
    worktrees_dir = Path(repo_root) / ".worktrees"
    if not worktrees_dir.exists():
        return []

    if _ops._repo_is_shallow(repo_root):
        _ops._deepen_shallow_repo(repo_root)

    merge_cache = _ops._load_worktree_merge_cache()
    cache_size_before = len(merge_cache)
    # One ls-remote for the whole sweep; None (offline) degrades pushed-tier verdicts to keep.
    remote_heads = _ops._fetch_remote_branch_heads(repo_root)

    now = time.time()
    records: list[TreeRecord] = []
    for entry in sorted(worktrees_dir.iterdir()):
        if not entry.is_dir():
            continue
        try:
            age_days = (now - entry.stat().st_mtime) / 86400.0
        except Exception:
            continue
        try:
            branch = _git(["branch", "--show-current"], cwd=str(entry), timeout=5).stdout.strip()
        except Exception:
            branch = ""
        verdict, reason, untracked = _classify_tree(_ops, repo_root, entry, merge_cache, remote_heads)
        if older_than_days is not None and verdict in _REAP_VERDICTS and age_days < older_than_days:
            verdict, reason, untracked = "keep", (
                f"reapable but only {age_days:.1f}d old (--older-than {older_than_days:g})"), []
        records.append(TreeRecord(
            name=entry.name, path=str(entry), branch=branch,
            age_days=age_days, size_mb=_tree_size_mb(entry) if with_sizes else None,
            verdict=verdict, reason=reason, untracked=untracked,
            attention=_ATTENTION_NO_BOARD_ROW if reason == _NO_BOARD_ROW_REASON else ""))

    if len(merge_cache) != cache_size_before:
        _ops._save_worktree_merge_cache(merge_cache)
    return records


_REAP_VERDICTS = {"reap", "reap-archive", "reap-keep-branch"}


def reclaim_worktrees(
    repo_root: str, *, dry_run: bool = False, records: Optional[list[TreeRecord]] = None
) -> list[str]:
    """Remove every reap-verdict tree from a frozen audit list — never re-globs inside the
    destructive loop, so trees created by concurrent sessions after the audit are out of scope."""
    if records is None:
        records = audit_worktrees(repo_root, with_sizes=False)
    actions: list[str] = []
    for record in records:
        if record.verdict not in _REAP_VERDICTS:
            continue
        if dry_run:
            actions.append(f"would remove {record.name} ({record.reason})")
            continue

        entry = Path(record.path)
        if record.untracked:
            archive = _archive_untracked(entry, record.untracked)
            if archive is None:
                actions.append(f"kept {record.name} (archive of untracked files failed)")
                continue
            actions.append(f"archived {len(record.untracked)} untracked file(s) → {archive}")

        # Dead-pid locks must be unlocked or `remove --force` refuses.
        with contextlib.suppress(Exception):
            _git(["worktree", "unlock", record.path], cwd=repo_root, timeout=10)
        try:
            remove_result = _git(["worktree", "remove", record.path, "--force"], cwd=repo_root, timeout=30)
            if remove_result.returncode != 0:
                actions.append(f"failed to remove {record.name}: {remove_result.stderr.strip()}")
                continue
            if record.verdict == "reap-keep-branch":
                actions.append(f"removed {record.name} (branch {record.branch} kept — {record.reason})")
                continue
            if record.branch and record.branch not in _PROTECTED_BRANCHES:
                _git(["branch", "-D", record.branch], cwd=repo_root, timeout=10)
            actions.append(f"removed {record.name} ({record.reason})")
        except Exception as exc:
            actions.append(f"failed to remove {record.name}: {exc}")

    if not dry_run:
        with contextlib.suppress(Exception):
            _git(["worktree", "prune"], cwd=repo_root, timeout=15)
    return actions


def audit_branches(repo_root: str) -> list[BranchRecord]:
    """Classify EVERY local branch: deletable when fully merged OR every commit is patch-equivalent
    upstream (``git cherry``) and not checked out. The gate is content reachability, not name."""
    from hermes_cli import worktree_ops as _ops
    if _ops._repo_is_shallow(repo_root):
        _ops._deepen_shallow_repo(repo_root)

    def _lines(result) -> list[str]:
        return [b.strip() for b in result.stdout.splitlines() if b.strip()]

    # No remote at all -> the local trunk; no trunk either -> nothing can be judged, report nothing.
    try:
        upstream = _ops._worktree_merge_base_ref(repo_root)
    except Exception:
        upstream = None
    if upstream is None:
        return []

    result = _git(["branch", "--format=%(refname:short)"], cwd=repo_root, timeout=10)
    if result.returncode != 0:
        return []
    branches = _lines(result)

    wt = _git(["worktree", "list", "--porcelain"], cwd=repo_root, timeout=10)
    active = {
        line.removeprefix("branch refs/heads/").strip()
        for line in wt.stdout.splitlines() if line.startswith("branch refs/heads/")}
    merged = set(_lines(_git(["branch", "--merged", upstream, "--format=%(refname:short)"], cwd=repo_root, timeout=15)))

    def _classify_branch(branch: str) -> BranchRecord:
        if branch in _PROTECTED_BRANCHES or branch in active:
            return BranchRecord(branch, "keep", "protected or checked out")
        if branch in merged:
            return BranchRecord(branch, "delete", "fully merged into " + upstream)
        # Rebase merges rewrite SHAs, so --merged misses them; cherry patch-equivalence catches
        # the dominant leak. Bounded: a branch far ahead is a stale-base lane, keep it.
        ahead = _git(["rev-list", "--count", f"{upstream}..{branch}"], cwd=repo_root, timeout=10)
        try:
            ahead_count = int(ahead.stdout.strip() or "0")
        except ValueError:
            ahead_count = _MAX_CHERRY_AHEAD + 1
        if ahead_count == 0:
            return BranchRecord(branch, "delete", "no commits beyond " + upstream)
        if ahead_count > _MAX_CHERRY_AHEAD:
            return BranchRecord(branch, "keep", f"{ahead_count} commits ahead (stale-base lane)")
        cherry = _git(["cherry", upstream, branch], cwd=repo_root, timeout=30)
        if cherry.returncode != 0:
            return BranchRecord(branch, "keep", "could not verify (git cherry failed)")
        lines = _lines(cherry)
        if lines and all(ln.startswith("-") for ln in lines):
            return BranchRecord(branch, "delete", "all commits patch-equivalent upstream")
        unique = sum(1 for ln in lines if ln.startswith("+"))
        return BranchRecord(branch, "keep", f"{unique} unique commit(s) not upstream")

    # Read-only, so parallel: hundreds of local branches × ~0.2-1s cherry probes would be minutes.
    import concurrent.futures
    workers = max(1, min(8, (os.cpu_count() or 4), len(branches)))
    if workers > 1:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hermes-branch-gc") as pool:
                return list(pool.map(_classify_branch, branches))
        except Exception:
            pass
    return [_classify_branch(b) for b in branches]


def reclaim_branches(
    repo_root: str, *, dry_run: bool = False, records: Optional[list[BranchRecord]] = None
) -> list[str]:
    """Delete every delete-verdict branch from a frozen audit list."""
    if records is None:
        records = audit_branches(repo_root)
    actions: list[str] = []
    for record in records:
        if record.verdict != "delete":
            continue
        if dry_run:
            actions.append(f"would delete branch {record.name} ({record.reason})")
            continue
        result = _git(["branch", "-D", record.name], cwd=repo_root, timeout=10)
        actions.append(
            f"deleted branch {record.name}" if result.returncode == 0
            else f"failed to delete {record.name}: {result.stderr.strip()}")
    return actions


def worktrees_summary(repo_root: str) -> tuple[int, Optional[int]]:
    """(tree_count, total_size_mb) for the escalation notice; size is best-effort with a timeout."""
    worktrees_dir = Path(repo_root) / ".worktrees"
    if not worktrees_dir.exists():
        return 0, None
    try:
        count = sum(1 for e in worktrees_dir.iterdir() if e.is_dir())
    except Exception:
        return 0, None
    return count, _tree_size_mb(worktrees_dir, timeout=20)
