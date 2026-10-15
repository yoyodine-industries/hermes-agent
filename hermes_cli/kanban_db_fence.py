"""The Kanban write fence: the subject a verdict judges is the store the write LANDS ON.

Split out of ``hermes_cli/kanban_db.py`` (the facade) per the facade+siblings rule - behaviour
goes in a sibling, never a facade (AGENTS.md). ``kanban_db`` re-exports the two names its own
door and its other callers read, at its tail, exactly as it does for its other topical siblings.

The verdict and its SUBJECT live together here because they are ONE decision:

* :func:`_assert_not_delegated_child_mutation` - the verdict, asked about one path (every
  ``write_txn`` user and board-metadata mutator passes through it).
* :func:`_write_fence_target_store` - the subject for a connection: the board store the write
  lands on, which is the file ``write_txn`` already fences (``kanban_db_connect._main_db_file``).
* :func:`_assert_not_delegated_child_write` - the door's fast-fail: the verdict asked about the
  CONNECTION's store, with the board the caller NAMED only as a fallback.

Cross-module imports are inside the functions (AGENTS.md: siblings late-import the facade inside
functions, never a module-level cycle), so importing this module imports nothing else.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


def _assert_not_delegated_child_mutation(path: str | Path | None = None) -> None:
    """Reject Kanban mutations from ``delegate_task`` child contexts.

    The tool/CLI fast-fail guards are UX, not a trust boundary (a child can shell
    out or import this module); the invariant lives here so every ``write_txn``
    user and board-metadata mutator fails closed before touching durable state.
    *path* is the board DB / metadata root being mutated; ``None`` means the
    lineage's own board (``kanban_home()``).

    Three arms, all additive (ruling t_fcf7a321, Decision 1): the ContextVar and the
    ``HERMES_DELEGATED_CHILD_CONTEXT`` marker (both inside :func:`kanban_path_is_fenced`), plus
    the PROVENANCE arm — a process descended from a LIVE dispatched worker is refused even when
    the cooperatively-clearable marker has been stripped. Reads never call this.
    """
    from agent.delegation_context import (
        PROVENANCE_REFUSAL_MESSAGE,
        ancestor_owns_live_kanban_claim,
        kanban_path_is_fenced,
    )
    from hermes_cli.kanban_db import kanban_home

    target = kanban_home() if path is None else path
    if kanban_path_is_fenced(target):
        raise PermissionError("delegate_task child contexts cannot mutate Kanban tasks or boards")
    if ancestor_owns_live_kanban_claim(target):
        raise PermissionError(PROVENANCE_REFUSAL_MESSAGE)


def _write_fence_target_store(
    conn: sqlite3.Connection, *, board: Optional[str] = None,
) -> Optional[Path]:
    """The board store a mutation on *conn* LANDS ON - the only subject a fence verdict may judge.

    A caller may NAME a board (``board=``) for the row it writes, but the write lands on the
    store ``conn`` is open against. ``kanban_db_path(board=<name>)`` answers the ENV-PINNED store
    whenever a worker's pin is authoritative (``kanban_db._explicit_board_intent_pinned``), so a
    fence judged against it can speak about a board that is neither the write's board nor a board
    the invocation named: it over-refuses a write that lands OUTSIDE the fence and it MASKS the
    honest board error - the same masking class the CLI fast-fail was cured of (ruling of
    platform-stl on t_ecd51c9c; card t_57e33100).

    The subject is therefore read OFF THE CONNECTION - the very file ``write_txn`` fences in the
    same breath (``kanban_db_connect._main_db_file``), so the two agree instead of disagreeing.
    ``board`` is kept only as the FALLBACK for the one case the connection cannot answer (no file
    at all: an in-memory store), and a name that resolves to NO board is then no board for a
    fence verdict either. Never raises: a guard that cannot resolve a subject must not become the
    caller's failure.
    """
    from hermes_cli.kanban_db import (
        DEFAULT_BOARD,
        _normalize_board_slug,
        board_exists,
        kanban_db_path,
    )
    from hermes_cli.kanban_db_connect import _main_db_file

    path = _main_db_file(conn)
    if path:
        return Path(path)
    if not board:
        return None
    try:
        normed = _normalize_board_slug(board)
        if not normed or (normed != DEFAULT_BOARD and not board_exists(normed)):
            return None
        return kanban_db_path(board=normed)
    except (ValueError, TypeError, OSError):
        return None


def _assert_not_delegated_child_write(
    conn: sqlite3.Connection, *, board: Optional[str] = None,
) -> None:
    """Refuse a delegated-child / worker-descendant write to the store *conn* LANDS on.

    :func:`_assert_not_delegated_child_mutation` carries every arm of the fence - the
    cooperatively-clearable ContextVar and marker inside ``kanban_path_is_fenced``, plus the
    PROVENANCE arm a worker's shell cannot clear with ``env -u``. This is the same verdict with
    the SUBJECT read off the connection instead of the named/pinned board: the designation door
    is an ESTATE move (it re-ranks a card into the board's reserved tranche), so what it must be
    judged by is the store its own rows land in.

    ``None`` target (no store to be told, no board named) is NO VERDICT, never a blanket
    refusal: a fence that cannot see must not refuse.
    """
    target = _write_fence_target_store(conn, board=board)
    if target is None:
        return
    _assert_not_delegated_child_mutation(target)
