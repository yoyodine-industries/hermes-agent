"""Read-only census of the cards a DEFCON hold is pinning — the ``hermes status`` /
``hermes doctor`` half of the lane-scoped stop's visibility (v3 §6.9).

The WRITE side of that record is ``kanban_db_dispatch._record_lockdown_events``: one
``skipped_lockdown`` task event per held card per ENGAGEMENT. This module reads exactly the rows
it writes, so the two must agree on the dedupe key — a card counts only while the NEWEST
``skipped_lockdown`` event on it carries the engagement identity that is on the sentinel NOW
(``agent.estop.engagement_key()``, the ``mtime_ns:size`` of the sentinel body). A lifted and
re-armed stop therefore reports the cards it holds today, not the union of every stop this host
ever armed.

Read-only by construction: each store is opened ``mode=ro`` through
:mod:`hermes_cli.sqlite_safe_read` (POSIX-lock safe; no schema pass; never creates a missing
store; no write to the database — the store's bytes are measured sha256-identical across a census
of the four live boards) and every read failure is REPORTED as ``unreadable`` rather than raised —
a status line must survive a board it cannot read, and it must not quietly report a board it
could not read as holding nothing.

The census's one on-disk effect is SQLite's, and it is NOT "no sidecars": the first read of a WAL
store whose ``-wal``/``-shm`` were reaped recreates both files, exactly as any other reader of
that store does (measured on the quiet ``defcon`` and ``ops`` boards, which gained both while
their stores stayed byte-identical). They outlive this connection — a read-only close does not
unlink them; a later read-write opener's last close reclaims them — so the census adds those two
sidecar files without writing the database.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from hermes_cli import sqlite_safe_read

#: Rendered in place of a count for a board whose store could not be read.
UNREADABLE = "unreadable"

#: How long a read may wait on another process's write lock before the board is called
#: unreadable. A diagnostic that blocks on a busy dispatcher is worse than one that says so.
_LOCK_TIMEOUT_SECONDS = 2.0

# The NEWEST ``skipped_lockdown`` event per card, resolved in ONE pass over the board (the same
# "newest row per card" read ``_record_lockdown_events`` does, as a ``MAX(id)`` join): a
# per-card query would be one query per held card per surface render.
_NEWEST_HELD_SQL = (
    "SELECT ev.task_id, ev.payload "
    "  FROM task_events AS ev "
    "  JOIN (SELECT task_id, MAX(id) AS newest"
    "          FROM task_events"
    "         WHERE kind = 'skipped_lockdown'"
    "      GROUP BY task_id) AS newest"
    "    ON newest.task_id = ev.task_id AND newest.newest = ev.id"
)


@dataclass(frozen=True)
class HeldBoard:
    """One board's held cards; ``cards is None`` means the store could not be read."""

    board: str
    cards: Optional[int] = 0
    lanes: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class HeldCards:
    """The census across the boards this host dispatches, in board order."""

    boards: tuple[HeldBoard, ...] = ()

    @property
    def total(self) -> int:
        """Cards counted so far — a LOWER BOUND when :attr:`unreadable` is non-empty."""
        return sum(board.cards or 0 for board in self.boards)

    @property
    def unreadable(self) -> tuple[str, ...]:
        return tuple(board.board for board in self.boards if board.cards is None)

    def lanes(self) -> dict[str, int]:
        """Held cards per LANE, merged across boards (the dispatch gate is not board-keyed)."""
        merged: dict[str, int] = {}
        for board in self.boards:
            for lane, count in board.lanes.items():
                merged[lane] = merged.get(lane, 0) + count
        return merged


def board_db_paths(boards: Optional[Iterable[str]] = None) -> list[tuple[str, Path]]:
    """``(slug, kanban.db)`` for every board this host dispatches, ``default`` first.

    ``boards`` is the slug list the caller already has (``hermes kanban boards`` enumerates
    it); omitted, it comes from :func:`kanban_db.list_boards` with archived boards dropped —
    an archived board holds nothing this host will dispatch.

    Each path is the board's OWN store even when ``HERMES_KANBAN_DB`` pins this process to one
    board (a dispatched worker running ``hermes status``): the question the census answers is
    "how many cards is this stop holding on the host", and ``kanban_db.kanban_db_path`` would
    collapse every entry onto the pinned store.
    """
    from hermes_cli import kanban_db as kb

    slugs = [meta["slug"] for meta in kb.list_boards(include_archived=False)] if boards is None else list(boards)
    paths: list[tuple[str, Path]] = []
    for slug in slugs:
        store = kb.kanban_home() / "kanban.db" if slug == kb.DEFAULT_BOARD else kb.board_dir(slug) / "kanban.db"
        paths.append((slug, store))
    return paths


def census(
    *,
    engagement: str,
    boards: Optional[Iterable[str]] = None,
    paths: Optional[Iterable[tuple[str, Path]]] = None,
) -> HeldCards:
    """Count the cards held under ``engagement`` (``agent.estop.engagement_key()``), per board.

    ``boards`` narrows (and orders) the census; ``paths`` takes pre-resolved stores instead —
    the seam the renderers and their tests share. A board with no store on disk holds nothing
    (nothing has dispatched there yet); one that cannot be read is reported as ``UNREADABLE``.
    """
    entries = list(paths) if paths is not None else board_db_paths(boards)
    return HeldCards(tuple(_board_census(board, path, engagement) for board, path in entries))


def clause(held: HeldCards, *, total_hold: bool = False) -> str:
    """``held cards: 3 (defcon: 2, ops: 1)`` — the operator line's held-card clause.

    ``held cards: none`` when nothing is held and every store was readable. A board that could
    not be read is NAMED (``ops: unreadable``) rather than dropped: a count that silently skips
    a board it failed to open is the fail-open reading this line exists to prevent.

    ``total_hold`` picks the lane half. Under a TOTAL halt every lane is held, so a per-lane
    list would present a total halt as if a lane had been singled out — the exact misreading
    (§6.9) the clause is here to prevent; the lane list is therefore rendered only for a
    lane-scoped hold, where it is the point.
    """
    listed = [board for board in held.boards if board.cards is None or board.cards]
    if not listed:
        return "held cards: none"
    named = ", ".join(
        f"{board.board}: {UNREADABLE if board.cards is None else board.cards}" for board in listed
    )
    text = f"held cards: {held.total} ({named})"
    if not held.total:
        return text
    if total_hold:
        return f"{text} — total halt, every lane held"
    return f"{text} — held lanes: " + ", ".join(f"{lane} x{count}" for lane, count in sorted(held.lanes().items()))


def _board_census(board: str, path: Path, engagement: str) -> HeldBoard:
    """One board's held cards. Never raises: a store that cannot be read is named as such."""
    if not path.exists():
        return HeldBoard(board=board, cards=0)
    try:
        # ``mode=ro`` + the tracking registry: no schema pass, no write to the store, and no
        # raw-file read that could cancel a live connection's POSIX locks elsewhere in-process.
        # A WAL store whose ``-wal``/``-shm`` were reaped gets both back on this first read —
        # SQLite's own sidecar churn, not a store write (see the module docstring).
        conn = sqlite_safe_read.connect_tracked(
            f"file:{path}?mode=ro", tracking_path=path, uri=True, timeout=_LOCK_TIMEOUT_SECONDS,
        )
    except Exception:
        return HeldBoard(board=board, cards=None)
    try:
        lanes: dict[str, int] = {}
        for row in conn.execute(_NEWEST_HELD_SQL):
            payload = _payload(row[1])
            if not isinstance(payload, dict) or payload.get("engagement") != engagement:
                continue
            lane = str(payload.get("profile") or "").strip() or "(unassigned)"
            lanes[lane] = lanes.get(lane, 0) + 1
    except sqlite3.Error:
        return HeldBoard(board=board, cards=None)
    finally:
        conn.close()
    return HeldBoard(board=board, cards=sum(lanes.values()), lanes=lanes)


def _payload(raw) -> Optional[object]:
    """Tolerant payload read: a hand-mangled event must not take the status line down."""
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
