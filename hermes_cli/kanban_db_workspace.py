"""Task workspace lifecycle: scratch/dir/worktree resolution (incl. git worktree creation), post-completion cleanup with containment guards, worker tmux teardown and the first-use scratch-workspace tip.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.

Terminal-reap invariant: a card's linked worktree is reaped when it holds no
*unrecoverable* state — a clean tree whose HEAD either rides a branch that
survives the removal (every non-``wt/`` branch, i.e. a project-linked card's
``<project-slug>/<task-id>``, whose commits stay reachable) or has no commits
unreachable from a remote-tracking ref. Commits are protected by KEEPING THE
BRANCH, never by keeping the tree: a tree parked at ``<repo>/.worktrees/<id>``
must not linger merely because nothing was pushed. See
:func:`_worktree_reap_verdict` (the one predicate both the prompt reap and the
residue sweep use) and :func:`sweep_terminal_worktree_workspaces`.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any
from typing import Optional
from typing import TYPE_CHECKING
import contextlib

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task

_REMOVABLE_KINDS = ("scratch", "worktree")

# Statuses after which a child no longer needs its parent's workspace artifacts.
_ACTIVE_CHILDREN_SQL = (
    "SELECT 1 FROM task_links l "
    "JOIN tasks t ON t.id = l.child_id "
    "WHERE l.parent_id = ? AND t.status NOT IN ('done', 'archived', 'failed', 'cancelled') "
    "LIMIT 1"
)

_WORKSPACE_ROW_SQL = "SELECT workspace_kind, workspace_path, branch_name FROM tasks WHERE id = ?"


def _git(repo_root: Path, *args: str, timeout: int) -> subprocess.CompletedProcess:
    """``git -C repo_root args``; never raises on a non-zero exit."""
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True, encoding='utf-8', errors='replace',
        timeout=timeout,
        check=False,
    )


def _has_active_children(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute(_ACTIVE_CHILDREN_SQL, (task_id,)).fetchone() is not None


def _managed_scratch_path_info(p: Path) -> tuple[bool, Optional[str]]:
    """Return whether *p* is managed scratch storage and the matching board."""
    try:
        p_abs = p.resolve(strict=False)
    except OSError:
        return False, None
    roots: list[tuple[Path, Optional[str]]] = []
    override = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
    if override:
        with contextlib.suppress(OSError):
            roots.append((Path(override).expanduser().resolve(strict=False), None))
    try:
        home = _kb.kanban_home()
    except OSError:
        home = None
    if home is not None:
        with contextlib.suppress(OSError):
            roots.append(((home / "kanban" / "workspaces").resolve(strict=False), _kb.DEFAULT_BOARD))
        entries: list[Path] = []
        with contextlib.suppress(OSError):
            entries = list((home / "kanban" / "boards").resolve(strict=False).iterdir())
        for entry in entries:
            with contextlib.suppress(OSError):
                if entry.is_dir():
                    roots.append(((entry / "workspaces").resolve(strict=False), entry.name))
    for root, board in roots:
        if p_abs == root:
            continue
        try:
            if p_abs.is_relative_to(root):
                return True, board
        except ValueError:
            continue
    return False, None


def _scratch_workspace(conn: sqlite3.Connection, task_id: str) -> Optional[Path]:
    """Expanded ``workspace_path`` when the task uses a scratch workspace, else ``None``."""
    row = conn.execute(
        "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row or row["workspace_kind"] != "scratch" or not row["workspace_path"]:
        return None
    return Path(row["workspace_path"]).expanduser()


def _is_managed_scratch_path(p: Path) -> bool:
    """True iff *p* is a STRICT descendant of a kanban-managed ``workspaces/``
    root (``HERMES_KANBAN_WORKSPACES_ROOT``, ``<kanban_home>/kanban/workspaces``,
    or ``<kanban_home>/kanban/boards/<slug>/workspaces``). A path equal to a
    root is not managed (deleting it would wipe every task's scratch dir);
    ``<kanban_home>/kanban``, ``.../logs`` and ``.../boards/<slug>`` hold
    Hermes' own DB and metadata. :func:`_cleanup_workspace` refuses
    ``rmtree`` outside managed storage — a board ``default_workdir`` on a real
    source tree paired with ``workspace_kind='scratch'`` would otherwise make
    task completion delete user data.

    See #28818.
    """
    return _managed_scratch_path_info(p)[0]


def _cleanup_workspace(conn: sqlite3.Connection, task_id: str) -> None:
    """Remove a task's scratch workspace dir and kill its stale tmux session.
    Called from :func:`complete_task` after the transaction commits; best-effort
    so cleanup never blocks completion. ``scratch`` is removed; ``worktree``
    only when provably free of work (clean tree, every commit reachable from a
    remote-tracking ref); ``dir`` is intentionally preserved."""
    try:
        row = conn.execute(_WORKSPACE_ROW_SQL, (task_id,)).fetchone()
        if not row:
            return
        kind: Optional[str] = row["workspace_kind"]
        path: Optional[str] = row["workspace_path"]
        if kind not in _REMOVABLE_KINDS or not path:
            # Not removable itself, but completing may still unblock a deferred
            # parent scratch cleanup (e.g. a 'dir' child of a scratch parent).
            # See #33774.
            _try_cleanup_parent_workspaces(conn, task_id)
            return
        # Defer while any child is not yet terminal so it can still read
        # handoff artifacts from this workspace.
        if _has_active_children(conn, task_id):
            _kb._log.debug(
                "Deferring %s workspace cleanup for task %s: "
                "active children still need workspace at %s",
                kind, task_id, path,
            )
            return
        # Kill the (dead) tmux worker session BEFORE removing a worktree so a
        # lingering worker never has its cwd deleted from under it.
        if kind == "worktree":
            _cleanup_worker_tmux(conn, task_id)
            _cleanup_worktree_workspace(task_id, path, row["branch_name"])
            _try_cleanup_parent_workspaces(conn, task_id)
            return
        wp = Path(path)
        if wp.is_dir():
            # Containment guard: a board's ``default_workdir`` can pair
            # ``workspace_kind='scratch'`` with a user path pointing at a real
            # source tree; without this, completion would rmtree the user's data.
            # See #28818.
            if _is_managed_scratch_path(wp):
                shutil.rmtree(wp, ignore_errors=True)
                _kb._log.debug("Removed scratch workspace: %s", wp)
            else:
                _kb._log.warning(
                    "Refusing to remove out-of-scratch workspace for task %s: %s "
                    "(workspace_kind='scratch' but path is outside any "
                    "kanban-managed workspaces root)",
                    task_id, wp,
                )
        # Kill the owning worker's tmux session if it is now dead, then let any
        # parent whose children are all done run its deferred cleanup.
        _cleanup_worker_tmux(conn, task_id)
        # After cleaning up this task's workspace, check if any parent tasks now have all children done —
        # their deferred cleanup can proceed (#33774).
        _try_cleanup_parent_workspaces(conn, task_id)
    except Exception:
        pass  # best-effort — never block completion


# ---------------------------------------------------------------------------
# Card worktrees: the identity marker (Part 3) and the one reap predicate
# ---------------------------------------------------------------------------

# A card worktree parked at ``<repo>/.worktrees/<task-id>`` reads like the
# project checkout. Every tree we materialize carries this marker, and it is
# listed in the repo's COMMON ``.git/info/exclude`` so the marker itself never
# dirties the tree (a dirty tree would never be reaped — see
# :func:`_write_worktree_marker`).
_WORKTREE_MARKER_NAME = "KANBAN-WORKTREE.md"


def _worktree_head_is_detached(worktree_path: Path) -> bool:
    """``git rev-parse --abbrev-ref HEAD`` == ``HEAD`` means a detached HEAD.

    Fails SAFE toward True (an unreadable HEAD counts as detached), so a git
    error can only ever preserve a tree that holds unpushed commits.
    """
    out = _kb._git_out(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
    return out is None or out.strip() == "HEAD"


def _has_remote_tracking_baseline(path: Path) -> bool:
    """Whether ``_worktree_has_unpushed_commits`` has anything to compare to.

    That predicate's own contract is "no remote-tracking refs = no baseline ->
    False", so its verdict alone must never authorise deleting a branch: in a
    repo with no remote, every commit looks pushed. Deleting ``wt/<task-id>``
    needs a real baseline, else the reap would destroy the only ref that held
    those commits.
    """
    return bool(
        _kb._git_out(path, "for-each-ref", "--format=%(refname)", "refs/remotes")
    )


def _worktree_reap_verdict(worktree_path: Path) -> tuple[bool, str]:
    """THE terminal-reap predicate, shared by the prompt reap in
    :func:`_cleanup_worktree_workspace` and the residue sweep in
    :func:`sweep_terminal_worktree_workspaces` so the two cannot drift.

    Returns ``(reap, reason)``. ``git worktree remove`` never touches refs, so a
    tree is reapable when nothing in it is unrecoverable:
      1. dirty (uncommitted tracked changes or untracked files) -> preserve;
      2. detached HEAD holding commits no remote-tracking ref reaches ->
         preserve (they would be unreachable once the tree goes);
      3. otherwise -> reap: an attached HEAD rides a branch that survives the
         removal and keeps its commits, so the tree itself is worth nothing.
    """
    from hermes_cli.worktree_ops import (  # late-bound: CLI safety predicates
        _worktree_has_unpushed_commits,
        _worktree_is_dirty,
    )

    if _worktree_is_dirty(str(worktree_path)):
        return False, "uncommitted changes in the tree"
    if _worktree_head_is_detached(worktree_path) and _worktree_has_unpushed_commits(
        str(worktree_path)
    ):
        return False, "detached HEAD holding commits no ref points at"
    return True, "clean tree; HEAD's branch keeps any commits"


def _worktree_marker_path(tree: Path) -> Path:
    return tree / _WORKTREE_MARKER_NAME


def _worktree_marker_text(
    *,
    task_id: str,
    branch_name: Optional[str],
    repo_root: Path,
    state: str = "created",
    reason: Optional[str] = None,
) -> str:
    branch = (branch_name or "").strip() or f"wt/{task_id}"
    stamped = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lines = [
        "# Kanban task worktree — NOT the project checkout",
        "",
        "This directory is a **kanban task worktree** (`git worktree`) that is",
        f"parked at its own HEAD under `<repo>/.worktrees/{task_id}`. Files here",
        "are a task snapshot, not repo truth: the canonical checkout is",
        "",
        f"- canonical repo root: `{repo_root}`",
        f"- kanban task id: `{task_id}`",
        f"- branch: `{branch}`",
        f"- marker stamped: `{stamped}` ({state})",
    ]
    if reason:
        lines += [
            "",
            "This tree outlived its kanban card because the terminal reap",
            f"preserved it: {reason}.",
        ]
    lines += [
        "",
        f"`{_WORKTREE_MARKER_NAME}` is listed in the repository's common",
        "`.git/info/exclude`, so the marker never makes the tree dirty.",
        "",
    ]
    return "\n".join(lines)


def _worktree_common_exclude(tree: Path) -> Optional[Path]:
    """``<common-git-dir>/info/exclude`` for the tree, or ``None``.

    ``git rev-parse --git-path info/exclude`` run inside a linked worktree
    resolves to the COMMON git dir's exclude file, which is the same directory
    ``--git-common-dir`` names — so we can address the file without gambling on
    a ``--path-format``/``--git-path`` combination's git-version behavior.
    """
    common = _git_common_dir(tree)
    return None if common is None else common / "info" / "exclude"


def _ensure_marker_excluded(tree: Path) -> bool:
    """Idempotently list the marker in the repo's common ``info/exclude``.

    Returns whether the entry is in place. The marker is only ever written when
    it is: an unexcluded marker would make the tree look dirty, and a dirty tree
    is preserved forever — resurrecting the parked-tree bug in a new shape.
    """
    exclude = _worktree_common_exclude(tree)
    if exclude is None:
        return False
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if _WORKTREE_MARKER_NAME in existing.split():
            return True
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}# kanban task worktrees are not project dirt\n")
            handle.write(f"{_WORKTREE_MARKER_NAME}\n")
        return True
    except OSError as exc:
        _kb._log.debug("Could not add %s to %s: %s", _WORKTREE_MARKER_NAME, exclude, exc)
        return False


def _write_worktree_marker(
    tree: Path,
    *,
    task_id: str,
    branch_name: Optional[str],
    repo_root: Path,
    state: str = "created",
    reason: Optional[str] = None,
) -> None:
    """Stamp ``<tree>/KANBAN-WORKTREE.md`` so a parked tree identifies itself.

    Best-effort: never raises into the caller (worktree creation or housekeeping),
    and never clobbers tracked content — if the branch already tracks a file of
    that name, it is left exactly as it is.
    """
    try:
        if not tree.is_dir():
            return
        if _kb._git_out(tree, "ls-files", "--error-unmatch", _WORKTREE_MARKER_NAME):
            _kb._log.debug("Leaving tracked %s in %s alone", _WORKTREE_MARKER_NAME, tree)
            return
        if not _ensure_marker_excluded(tree):
            _kb._log.debug(
                "Not writing %s in %s: %s is not writable, and an unexcluded "
                "marker would dirty the tree forever",
                _WORKTREE_MARKER_NAME, tree, _worktree_common_exclude(tree),
            )
            return
        _worktree_marker_path(tree).write_text(
            _worktree_marker_text(
                task_id=task_id, branch_name=branch_name, repo_root=repo_root,
                state=state, reason=reason,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        _kb._log.debug("Could not write %s in %s: %s", _WORKTREE_MARKER_NAME, tree, exc)


def _cleanup_worktree_workspace(
    task_id: str, path: str, branch_name: Optional[str] = None
) -> tuple[bool, str]:
    """Remove a finished task's linked git worktree when it holds no *unrecoverable* state.

    Invariant (see the module docstring): ``git worktree remove`` never touches
    refs, so the tree is reaped when :func:`_worktree_reap_verdict` finds nothing
    unrecoverable — a clean tree whose HEAD either rides a branch that survives
    the removal or has no commits unreachable from a remote-tracking ref.
    Commits are protected by KEEPING THE BRANCH, not by keeping the tree: a
    project-linked card's ``<project-slug>/<task-id>`` branch is never deleted,
    so an unpushed project branch no longer pins the tree on disk forever.

    Removal is plain ``git worktree remove`` — never ``--force``, so git's own
    dirty guard re-verifies at removal time (the TOCTOU property is kept). Any
    doubt (dirty, a detached HEAD with unpushed commits, an unresolvable repo,
    failing git) preserves the tree and refreshes its marker with the reason
    (Part 3). The auto-generated ``wt/<task-id>`` branch is deleted only when a
    remote-tracking baseline proves it holds no unique commits; custom branches
    are always kept. Best-effort; returns ``(removed, reason)``.
    """
    try:
        from hermes_cli.worktree_ops import _worktree_has_unpushed_commits
    except Exception:
        return False, "git safety predicates unavailable"  # preserve
    try:
        wp = Path(path).expanduser()
        if not wp.is_dir():
            return False, "tree is not on disk"
        common = _git_common_dir(wp)
        if common is None or common.name != ".git":
            return False, "not a linked worktree of a normal repo"
        repo_root = common.parent
        if wp.resolve(strict=False) == repo_root.resolve(strict=False):
            return False, "path is the main checkout"
        reap, reason = _worktree_reap_verdict(wp)
        if not reap:
            _kb._log.info("Preserving worktree for task %s at %s: %s", task_id, wp, reason)
            # A surviving tree must say why it is still here (Part 3).
            _write_worktree_marker(
                wp, task_id=task_id, branch_name=branch_name, repo_root=repo_root,
                state="preserved", reason=reason,
            )
            return False, reason
        # Both branch verdicts below need the worktree's own HEAD, so read them
        # BEFORE the removal takes it away.
        branch = (branch_name or "").strip() or f"wt/{task_id}"
        branch_holds_unique_commits = _worktree_has_unpushed_commits(str(wp))
        remote_baseline = _has_remote_tracking_baseline(repo_root)
        # No --force: git's own dirty guard re-verifies at removal time, so if
        # the tree became dirty since our check (TOCTOU) removal fails safe.
        result = _git(repo_root, "worktree", "remove", str(wp), timeout=60)
        if result.returncode != 0:
            _kb._log.warning(
                "git worktree remove failed for task %s at %s: %s",
                task_id, wp, (result.stderr or result.stdout or "").strip(),
            )
            return False, "git worktree remove refused"
        _kb._log.debug("Removed worktree workspace: %s", wp)
        # Only an auto-generated branch is ours to delete, and only when a
        # remote-tracking ref demonstrably holds its commits.
        if branch.startswith("wt/") and remote_baseline and not branch_holds_unique_commits:
            _git(repo_root, "branch", "-D", branch, timeout=30)
        return True, "reaped"
    except Exception as exc:
        _kb._log.debug("Worktree cleanup for task %s failed: %s", task_id, exc)
        return False, "cleanup failed"


# Statuses after which a card's worktree is residue nobody will revisit.
_TERMINAL_TASK_STATUSES = ("done", "archived", "failed", "cancelled")


def sweep_terminal_worktree_workspaces(
    conn: sqlite3.Connection,
    *,
    min_age_hours: float = 6.0,
    limit: int = 20,
    dry_run: bool = False,
) -> dict:
    """Reap card worktrees whose terminal reap never ran (Part 2: the residue).

    ``_cleanup_workspace`` only runs from ``complete_task``/``archive_task``, so
    a card that ended any other way (failed/cancelled, or a status written by a
    recovery path) leaks its tree with no other reaper — ``worktree prune``
    deliberately skips ``t_*`` trees. This is that reaper.

    Callers: the **dispatcher tick** is the periodic one (hourly gate in
    ``kanban_db_dispatch._maybe_sweep_terminal_worktrees``); ``hermes kanban gc``
    is the manual one and is the caller that passes ``dry_run``.

    Only a row whose ``<repo>/.worktrees/<task-id>`` path exists on disk is
    considered, and each candidate goes through the same
    :func:`_worktree_reap_verdict` predicate as the prompt reap. Never raises on
    a missing tree, a non-repo path or a git error — logs at DEBUG and moves on
    (this runs inside the gateway process). Returns ``{"scanned": n,
    "removed": [...], "preserved": {path: why}, "skipped": n}``.
    """
    summary: dict = {"scanned": 0, "removed": [], "preserved": {}, "skipped": 0}
    try:
        cutoff = time.time() - max(0.0, float(min_age_hours)) * 3600.0
        rows = list(
            conn.execute(
                "SELECT t.id AS id, t.workspace_path AS workspace_path, "
                "       t.branch_name AS branch_name, t.completed_at AS completed_at, "
                "       (SELECT MAX(r.ended_at) FROM task_runs r WHERE r.task_id = t.id) "
                "         AS last_run_end "
                "  FROM tasks t "
                " WHERE t.workspace_kind = 'worktree' "
                "   AND t.workspace_path IS NOT NULL AND t.workspace_path <> '' "
                "   AND t.status IN ({})".format(
                    ", ".join("?" for _ in _TERMINAL_TASK_STATUSES)
                ),
                _TERMINAL_TASK_STATUSES,
            )
        )
    except sqlite3.Error as exc:
        _kb._log.debug("Terminal worktree sweep skipped: %s", exc)
        return summary
    # No timestamp = no age verdict; never guess (skip those rows entirely).
    candidates: list[tuple[int, Any]] = []
    for row in rows:
        stamp = row["completed_at"] or row["last_run_end"]
        if stamp is None or int(stamp) > cutoff:
            continue
        candidates.append((int(stamp), row))
    candidates.sort(key=lambda item: item[0])  # oldest residue first
    budget = max(1, int(limit))
    for _stamp, row in candidates:
        if len(summary["removed"]) >= budget:
            break
        summary["scanned"] += 1
        task_id = row["id"]
        tree = Path((row["workspace_path"] or "").strip()).expanduser()
        # Containment: only ever the card's own ``<repo>/.worktrees/<task-id>``.
        if tree.parent.name != ".worktrees" or tree.name != task_id:
            summary["skipped"] += 1
            _kb._log.debug("Skipping non-canonical kanban worktree path %s (task %s)", tree, task_id)
            continue
        if not tree.is_dir():
            summary["skipped"] += 1
            continue
        try:
            if dry_run:
                # Report-only: same predicate, no mutation.
                reap, reason = _worktree_reap_verdict(tree)
                if reap:
                    summary["removed"].append(str(tree))
                else:
                    summary["preserved"][str(tree)] = reason
                continue
            removed, reason = _cleanup_worktree_workspace(task_id, str(tree), row["branch_name"])
        except Exception as exc:  # never raise into a gateway tick / CLI
            summary["skipped"] += 1
            _kb._log.debug("Sweep failed for task %s at %s: %s", task_id, tree, exc)
            continue
        if removed:
            summary["removed"].append(str(tree))
        else:
            summary["preserved"][str(tree)] = reason
    return summary


def _try_cleanup_parent_workspaces(conn: sqlite3.Connection, task_id: str) -> None:
    """Run the deferred cleanup of any parent scratch/worktree workspace whose
    children are now all done/archived/failed/cancelled (called after each
    child completes).

    See #33774.
    """
    try:
        parents = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?",
            (task_id,),
        ).fetchall()
        for (parent_id,) in parents:
            row = conn.execute(_WORKSPACE_ROW_SQL, (parent_id,)).fetchone()
            if (
                not row
                or row["workspace_kind"] not in _REMOVABLE_KINDS
                or not row["workspace_path"]
                or _has_active_children(conn, parent_id)
            ):
                continue
            if row["workspace_kind"] == "worktree":
                _cleanup_worktree_workspace(parent_id, row["workspace_path"], row["branch_name"])
                continue
            wp = Path(row["workspace_path"])
            if wp.is_dir() and _is_managed_scratch_path(wp):
                shutil.rmtree(wp, ignore_errors=True)
                _kb._log.debug("Deferred cleanup: removed parent %s scratch workspace: %s", parent_id, wp)
    except Exception:
        pass  # best-effort


def _cleanup_worker_tmux(conn: sqlite3.Connection, task_id: str) -> None:
    """Kill the tmux session associated with a task's assignee, if dead."""
    try:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row or not row["assignee"]:
            return
        # Workers named swarm1-12 use tmux sessions named swarm-swarm1 etc.
        session = f"swarm-{row['assignee']}"
        out = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_dead}"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
        )
        if out.stdout.strip() == "1":
            subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True, timeout=5)
            _kb._log.debug("Killed stale tmux session: %s", session)
    except Exception:
        pass  # best-effort — never block completion


_SCRATCH_TIP_SENTINEL_NAME = ".scratch_tip_shown"


_SCRATCH_TIP_MESSAGE = (
    "scratch workspaces are ephemeral — they're deleted when the task "
    "completes. Use --workspace worktree: (git worktree) or "
    "--workspace dir:/abs/path (existing dir) to preserve worker output."
)


def _scratch_tip_sentinel_path() -> Path:
    """Path to the per-install scratch-workspace-tip sentinel file."""
    return _kb.kanban_home() / _SCRATCH_TIP_SENTINEL_NAME


def _scratch_tip_shown() -> bool:
    """True iff the scratch-workspace tip was already emitted on this install.
    Best-effort — any error re-emits, the safer failure mode for a help message."""
    try:
        return _scratch_tip_sentinel_path().exists()
    except OSError:
        return False


def _mark_scratch_tip_shown() -> None:
    """Touch the sentinel so future scratch workspaces stay silent. Best-effort:
    a failure means the tip may appear once more, preferable to crashing dispatch."""
    try:
        path = _scratch_tip_sentinel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        pass


def _maybe_emit_scratch_tip(
    conn: sqlite3.Connection,
    task_id: str,
    workspace_kind: Optional[str],
) -> None:
    """Emit the first-use scratch-workspace tip once per install, right after a
    scratch workspace is materialized. No-op for ``worktree``/``dir`` (preserved
    by design) and once the sentinel exists."""
    if (workspace_kind or "scratch") != "scratch" or _scratch_tip_shown():
        return
    try:
        _kb._log.warning("kanban: %s (task %s)", _SCRATCH_TIP_MESSAGE, task_id)
        with _kb.write_txn(conn):
            _kb._append_event(
                conn, task_id, "tip_scratch_workspace",
                {"message": _SCRATCH_TIP_MESSAGE},
            )
    except Exception:
        # Best-effort — never block the spawn loop over a help message.
        pass
    finally:
        _mark_scratch_tip_shown()


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def _git_toplevel(path: Path) -> Optional[Path]:
    """Return the git toplevel containing ``path``, or ``None`` if not in a repo."""
    out = _kb._git_out(path, "rev-parse", "--show-toplevel")
    if out is None:
        return None
    try:
        return Path(out).expanduser().resolve()
    except Exception:
        return Path(out).expanduser()


def _git_branch_exists(repo_root: Path, branch_name: str) -> bool:
    try:
        result = _git(repo_root, "show-ref", "--verify", f"refs/heads/{branch_name}", timeout=30)
    except Exception:
        return False
    return result.returncode == 0


def _git_abs_path(path: Path, flag: str) -> Optional[Path]:
    out = _kb._git_out(path, "rev-parse", "--path-format=absolute", flag)
    return Path(out).expanduser().resolve(strict=False) if out else None


def _git_common_dir(path: Path) -> Optional[Path]:
    return _git_abs_path(path, "--git-common-dir")


def _git_dir(path: Path) -> Optional[Path]:
    return _git_abs_path(path, "--git-dir")


def _git_current_branch(path: Path) -> Optional[str]:
    return _kb._git_out(path, "branch", "--show-current")


def _is_linked_worktree_checkout(path: Path) -> bool:
    git_dir = _git_dir(path)
    common_dir = _git_common_dir(path)
    return git_dir is not None and common_dir is not None and git_dir != common_dir


def _nearest_existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _repo_root_for_worktree_target(path: Path) -> Optional[Path]:
    current = _nearest_existing_path(path).resolve(strict=False)
    while True:
        repo_root = _git_toplevel(current)
        if repo_root is not None:
            return repo_root
        if current == current.parent:
            return None
        current = current.parent


def _ensure_git_worktree(repo_root: Path, target: Path, branch_name: str) -> None:
    """Materialize ``target`` as a linked git worktree under ``repo_root``.

    Also stamps the tree with ``KANBAN-WORKTREE.md`` (Part 3): a tree parked
    under ``<repo>/.worktrees/<task-id>`` otherwise looks exactly like the
    project checkout to anyone who finds it. The marker is written on the
    already-materialized early return too, so trees that predate it get one.
    """
    target = target.expanduser()
    repo_common = _git_common_dir(repo_root)
    if target.exists() and repo_common is not None and _git_common_dir(target) == repo_common:
        _write_worktree_marker(
            target, task_id=target.name, branch_name=branch_name, repo_root=repo_root
        )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if _git_branch_exists(repo_root, branch_name):
        args = ["worktree", "add", str(target), branch_name]
    else:
        args = ["worktree", "add", "-b", branch_name, str(target), "HEAD"]
    result = _git(repo_root, *args, timeout=60)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"git worktree add failed for {target} on branch {branch_name}: {stderr}"
        )
    _write_worktree_marker(
        target, task_id=target.name, branch_name=branch_name, repo_root=repo_root
    )


def _anchored_worktree(repo_root: Path, task_id: str, branch_name: str) -> tuple[Path, str]:
    """Materialize the canonical ``<repo>/.worktrees/<task-id>`` worktree."""
    target = repo_root / ".worktrees" / task_id
    _ensure_git_worktree(repo_root, target, branch_name)
    return target, branch_name


def _resolve_worktree_workspace(task: Task, *, board: Optional[str] = None) -> tuple[Path, str]:
    """Resolve + materialize a linked git worktree for ``task``. With no
    ``task.workspace_path`` the anchor is the board's ``default_workdir`` so
    every worktree lands under a board-owned repo (``<repo>/.worktrees/<id>``)
    instead of the dispatcher's incidental CWD (whatever dir the gateway was
    launched from); with no anchor configured we fail loudly rather than guess."""
    branch_name = (task.branch_name or "").strip() or f"wt/{task.id}"
    if not task.workspace_path:
        board_slug = board if board else _kb.get_current_board()
        board_default = (_kb.read_board_metadata(board_slug).get("default_workdir") or "").strip()
        if not board_default:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but no workspace_path, "
                f"and board {board_slug!r} has no default_workdir set. Set a board "
                "default workdir (a git repo) or create the task with "
                "--workspace worktree:<absolute-repo-path>."
            )
        anchor = Path(board_default).expanduser()
        if not anchor.is_absolute():
            raise ValueError(
                f"board {board_slug!r} default_workdir {board_default!r} is not "
                "absolute; use an absolute path to a git repo"
            )
        repo_root = _git_toplevel(anchor)
        if repo_root is None:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but board "
                f"{board_slug!r} default_workdir {board_default!r} is not inside a git repo"
            )
        return _anchored_worktree(repo_root, task.id, branch_name)

    requested = Path(task.workspace_path).expanduser()
    if not requested.is_absolute():
        raise ValueError(
            f"task {task.id} has non-absolute worktree path "
            f"{task.workspace_path!r}; use an absolute path"
        )
    requested_resolved = requested.resolve(strict=False)

    if requested.exists() and _is_linked_worktree_checkout(requested):
        actual_branch = _git_current_branch(requested)
        if actual_branch == branch_name:
            return requested_resolved, actual_branch
        # The requested path is an existing checkout of a DIFFERENT task's
        # branch (decompose children inherit the root's workspace_path
        # verbatim, so siblings all point here). Reusing it would run this task
        # on the other task's branch — silent cross-task provenance corruption,
        # unsafe under concurrency — so fall back to our own worktree.
        fallback_root = _repo_root_for_worktree_target(requested.parent)
        if fallback_root is not None:
            fallback = fallback_root / ".worktrees" / task.id
            if fallback.resolve(strict=False) != requested_resolved:
                _ensure_git_worktree(fallback_root, fallback, branch_name)
                return fallback.resolve(strict=False), branch_name
        # No repo to anchor a fallback on (or the occupied path IS this task's
        # own canonical worktree): keep the legacy reuse rather than fail dispatch.
        return requested_resolved, actual_branch or branch_name

    repo_root = _git_toplevel(requested)
    if repo_root is not None and requested_resolved == repo_root:
        return _anchored_worktree(repo_root, task.id, branch_name)

    repo_root = _repo_root_for_worktree_target(requested.parent)
    if repo_root is None:
        raise ValueError(
            f"task {task.id} worktree path {task.workspace_path!r} is not inside a git repo "
            "and does not point at a git repo root"
        )
    _ensure_git_worktree(repo_root, requested, branch_name)
    return requested, branch_name


def resolve_workspace(task: Task, *, board: Optional[str] = None) -> Path:
    """Resolve (and create if needed) the workspace for a task.

    ``scratch``: ``<board-root>/workspaces/<id>/`` — path-stable across the
    dispatcher and every profile worker. ``dir``: ``workspace_path``, created
    if missing; MUST be absolute (relative paths would resolve against the
    dispatcher's CWD — confused-deputy traversal). ``worktree``: a linked git
    worktree; a repo-root ``workspace_path`` anchors ``<repo>/.worktrees/<id>``,
    a concrete path is created/reused, none -> the board's ``default_workdir``
    (raises if unset rather than guessing). Persist via ``set_workspace_path``.
    """
    kind = task.workspace_kind or "scratch"
    if kind == "worktree":
        return _resolve_worktree_workspace(task, board=board)[0]
    if kind == "scratch" and not task.workspace_path:
        p = _kb.workspaces_root(board=board) / task.id
    elif kind == "scratch":
        # Legacy explicit-path scratch tasks get the same absolute-path guard
        # as dir: — same threat model.
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; workspace paths must be absolute"
            )
    elif kind == "dir":
        if not task.workspace_path:
            raise ValueError(f"task {task.id} has workspace_kind=dir but no workspace_path")
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; use an absolute path "
                f"(relative paths are ambiguous against the dispatcher's CWD)"
            )
    else:
        raise ValueError(f"unknown workspace_kind: {kind}")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _set_task_column(conn: sqlite3.Connection, task_id: str, column: str, value: str) -> None:
    with _kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, task_id))


def set_workspace_path(conn: sqlite3.Connection, task_id: str, path: Path | str) -> None:
    _set_task_column(conn, task_id, "workspace_path", str(path))


def set_branch_name(conn: sqlite3.Connection, task_id: str, branch_name: str) -> None:
    _set_task_column(conn, task_id, "branch_name", str(branch_name))


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb  # noqa: E402
