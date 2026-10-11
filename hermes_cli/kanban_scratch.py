"""Scratch kanban board stores: board-by-name resolution that cannot reach a LIVE store.

Why this module exists (measured incident, 2026-09-27 09:23Z). An evidence harness wanted a
"scratch copy of the live board": it asked :func:`hermes_cli.kanban_db.kanban_db_path` for the
CURRENT board's store and copied a board into that path. ``kanban_home()`` is shared across
profiles BY DESIGN and follows the host's ``<root>/kanban/current`` pointer, so the
"scratch" destination resolved to the operator's LIVE store — ``boards/ops/kanban.db`` — and
a ``sqlite3`` backup replaced 42 MB of ops history with 180 KB of defcon rows. The harness's
own scratch HOME did not protect it: ``HERMES_HOME`` is not what :func:`kanban_home` reads.

So the rule is structural, not advisory — **a harness must never ask a resolver that is
allowed to answer with a live path**:

1. a board resolves by NAME to an EXPLICIT absolute path under a root the caller names
   (:func:`board_store`) — never through the host's current-board pointer;
2. a scratch destination is CONSTRUCTED under the run's own directory
   (:func:`scratch_board_store`);
3. :func:`assert_private_store` runs BEFORE the first write and FAILS CLOSED: a destination
   that is, or may be, a live board store — or that is not provably under the run's own
   directory — raises :class:`UnsafeKanbanStore` with the paths named. Ambiguity aborts; it
   is never downgraded to a warning.

This is the same class as the guard-shared-trees pattern: a deterministic refusal.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from hermes_cli.kanban_db import (
    DEFAULT_BOARD,
    _normalize_board_slug,
    kanban_home,
)

__all__ = [
    "UnsafeKanbanStore",
    "live_kanban_root",
    "board_store",
    "live_board_stores",
    "assert_private_store",
    "scratch_board_store",
    "snapshot_board_into_scratch",
]


class UnsafeKanbanStore(RuntimeError):
    """A write was about to land on a path that is not provably a private scratch store."""


def live_kanban_root() -> Path:
    """The operator's LIVE kanban root — ``~/.hermes``, never a scratch override.

    ``kanban_home()`` honours ``HERMES_KANBAN_HOME`` (which is how a harness redirects itself);
    the guard must NOT be redirectable by the same variable, or it would certify the very
    override it exists to check. ``get_default_hermes_root()`` resolves the native/platform
    default and ignores ``HERMES_KANBAN_HOME`` entirely, so it answers "where is the real
    install" even mid-harness. ``HERMES_HOME`` is honoured the same way it always is (a
    profile dir resolves to its root), so a profile-scoped live store is still recognised.
    """
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root()).expanduser().resolve()


def board_store(root: Path, board: str) -> Path:
    """Explicit absolute path of ``board``'s store under an explicit ``root``.

    Mirrors :func:`kanban_db.kanban_db_path`'s layout (``default`` keeps its back-compat
    ``<root>/kanban.db``; every other board is ``<root>/kanban/boards/<slug>/kanban.db``) but
    takes the root as an ARGUMENT and never consults the host's current-board pointer.
    """
    root = Path(root).expanduser().resolve()
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    if slug == DEFAULT_BOARD:
        return root / "kanban.db"
    return root / "kanban" / "boards" / slug / "kanban.db"


def live_board_stores(live_root: Optional[Path] = None) -> set[Path]:
    """Every store the LIVE install can dispatch from — the deny-list.

    Enumerated, not inferred: the default board's back-compat ``<root>/kanban.db``, every
    named board under ``<root>/kanban/boards/*/``, and whatever the root's current-board
    POINTER names right now (a stale pointer is still a live store once something writes it).

    Read straight off ``live_root`` on purpose: resolving through
    ``kanban_db.kanban_db_path()`` would answer with whatever ``HERMES_KANBAN_HOME`` /
    ``HERMES_KANBAN_DB`` currently say, so a harness that had redirected itself would get its
    own scratch path back as "the live store" — and, worse, a real live store would drop off
    the deny-list.
    """
    root = Path(live_root).expanduser().resolve() if live_root else live_kanban_root()
    stores = {root / "kanban.db"}
    boards_dir = root / "kanban" / "boards"
    try:
        for entry in sorted(boards_dir.iterdir()):
            if entry.is_dir():
                stores.add(entry / "kanban.db")
    except OSError:
        pass
    try:
        # utf-8-sig read: tolerate BOM-persisted current-board files (same fix as kanban_db).
        pointer = (root / "kanban" / "current").read_text(encoding="utf-8-sig").strip()
        slug = _normalize_board_slug(pointer)
        if slug and slug != DEFAULT_BOARD:
            stores.add(boards_dir / slug / "kanban.db")
    except (OSError, ValueError):
        pass
    return stores


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def assert_private_store(
    path: Path, *, run_root: Path, purpose: str = "scratch board store"
) -> Path:
    """Fail closed unless ``path`` is provably a private store under ``run_root``.

    Refuses (raising :class:`UnsafeKanbanStore`) when the destination:
      * is not under the run's own directory — resolution is ambiguous, or pinned to a
        path this run does not own (``HERMES_KANBAN_DB`` escaping the scratch tree);
      * IS a live board store, however it was reached (board name, host current-board
        pointer, a hand-written path);
      * sits under a run directory that owns the live root — that run is the live install.

    A run directory merely *inside* the live tree is allowed and is the host's own
    convention (a card's workspace evidence dir); what is refused is a live STORE SLOT,
    which no resolver enumerates below ``boards/<slug>/``.

    Returns the resolved path so callers use the checked value, never their own.
    """
    resolved = Path(path).expanduser().resolve()
    run = Path(run_root).expanduser().resolve()
    live = live_kanban_root()

    if resolved in live_board_stores(live) or resolved == live / "kanban.db":
        raise UnsafeKanbanStore(
            f"refusing to write {purpose} {resolved}: that is a LIVE board store of "
            f"{live}. A scratch harness must never resolve a live board store — failing "
            f"closed (incident 2026-09-27: boards/ops/kanban.db was overwritten this way)."
        )
    if not _is_within(resolved, run):
        raise UnsafeKanbanStore(
            f"refusing to write {purpose} {resolved}: it is not under this run's own "
            f"scratch directory {run}. Resolution is ambiguous or was steered by an "
            f"override this run does not control — failing closed."
        )
    if run == live or _is_within(live, run):
        raise UnsafeKanbanStore(
            f"refusing to write {purpose} {resolved}: the run's scratch directory {run} "
            f"owns the LIVE root ({live}). A scratch run must own a directory that does "
            f"not contain the live install — failing closed."
        )
    return resolved


def scratch_board_store(run_root: Path, board: str) -> Path:
    """A private store path for ``board``, constructed under ``run_root`` and checked.

    The path is built, never resolved: a construction cannot land on a live store, and the
    check refuses even if one is handed in by mistake.
    """
    run = Path(run_root).expanduser().resolve()
    return assert_private_store(
        board_store(run, board), run_root=run, purpose=f"{board!r} scratch board store"
    )


def snapshot_board_into_scratch(
    board: str, *, source_root: Path, run_root: Path
) -> tuple[Path, int]:
    """Copy ``board`` from ``source_root`` into a private store under ``run_root``.

    ``source_root`` names the origin explicitly (use :func:`live_kanban_root` for the real
    install) and is opened READ-ONLY; ``run_root`` receives the copy only after
    :func:`assert_private_store` has certified the destination. Returns the destination and
    the number of ``tasks`` rows copied, so the caller can quote both as evidence.

    This is the exact step whose earlier form destroyed the ops board: it resolved the
    SOURCE through the host's current-board pointer and the DESTINATION through the same
    resolver, so both sides moved together onto a live path.
    """
    source = board_store(source_root, board)
    if not source.exists():
        raise UnsafeKanbanStore(f"source board store does not exist: {source}")
    dest = scratch_board_store(run_root, board)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
            copied = out.execute("select count(*) from tasks").fetchone()[0]
        finally:
            out.close()
    finally:
        src.close()
    return dest, copied


def scratch_env(run_root: Path) -> dict[str, str]:
    """Env that pins every kanban resolver to this run's private tree.

    ``HERMES_KANBAN_HOME`` is the ONLY knob that moves the kanban root (it is what
    :func:`kanban_db.kanban_home` reads); ``HERMES_KANBAN_DB``/``HERMES_KANBAN_BOARD`` are
    cleared so nothing pins a single live file behind the caller's back.
    """
    run = Path(run_root).expanduser().resolve()
    return {
        "HERMES_KANBAN_HOME": str(run),
        "HERMES_KANBAN_DB": "",
        "HERMES_KANBAN_BOARD": "",
    }


def apply_scratch_env(run_root: Path) -> Path:
    """:func:`scratch_env` applied to this process; returns the certified kanban root."""
    run = Path(run_root).expanduser().resolve()
    live = live_kanban_root()
    if run == live or _is_within(live, run):
        raise UnsafeKanbanStore(
            f"refusing to point the kanban root at {run}: that directory owns the LIVE "
            f"root ({live}). A scratch run owns a directory that does not contain the "
            f"live install — failing closed."
        )
    for key, value in scratch_env(run).items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return run


def kanban_root_in_use() -> Path:
    """The root :func:`kanban_db.kanban_home` will use right now (diagnostics/evidence)."""
    return Path(kanban_home()).expanduser().resolve()
