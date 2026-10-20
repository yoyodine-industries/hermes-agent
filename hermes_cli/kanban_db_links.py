"""The link door's RECEIPT: what a new parent edge can do on its own.

A link gates a child behind a parent, but a gate is not a promise — the child only runs once
the parent FINISHES, and a parent parked in ``review``/``triage``/``blocked``, or assigned to
a profile that no longer exists, will never move by itself. This sibling owns the write path
(:func:`link_tasks_reported`), the per-edge classification it attaches to every parent of the
child, and the warning the doors report.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached late-bound via
``_kb`` (import-cycle breaking) so monkeypatching ``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Mapping, Optional

from hermes_cli.kanban_parent_state import ParentState, parent_promotion_state


@dataclass(frozen=True)
class ParentEdgeState(ParentState):
    """A :class:`ParentState` bound to the edge it describes: the parent's ``id`` and the
    ``status`` the classifier read, so a receipt names WHICH parent will not move."""

    id: str = ""
    status: str = ""


@dataclass
class LinkOutcome:
    """The link door's receipt: did the link gate, and will anything move the parents?

    ``parents`` is one entry per parent edge of the child AFTER the link (so a card with
    several parents reports all of them, not just the one just added); ``standing`` is the
    ids of the parents that will NOT move on their own; ``warning`` is the human line(s)
    naming them, or None when the whole dependency set can finish by itself.
    ``board_dispatch`` carries the board's dispatch admission when the caller named one.
    """

    gated: bool
    parents: list = field(default_factory=list)
    standing: list = field(default_factory=list)
    warning: Optional[str] = None
    board_dispatch: dict = field(default_factory=dict)


def _would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """True iff ``parent_id`` is already a descendant of ``child_id``."""
    seen = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


def parent_edges(conn: sqlite3.Connection, child_id: str, profiles=None) -> list:
    """Every parent edge of ``child_id``, classified from its live row."""
    ids = _kb.parent_ids(conn, child_id)
    if not ids:
        return []
    rows = {
        row["id"]: row for row in conn.execute(
            "SELECT id, status, assignee, block_kind, current_run_id, due_at FROM tasks "
            f"WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
    }
    edges = []
    for pid in ids:
        row = rows.get(pid) or {"status": "", "assignee": "", "block_kind": None,
                                "current_run_id": None, "due_at": None}
        state = parent_promotion_state(row, profiles=profiles)
        edges.append(ParentEdgeState(code=state.code, text=state.text,
                                     self_moves=state.self_moves,
                                     id=pid, status=str(row["status"] or "")))
    return edges


def link_warning(edges: list, board_dispatch: Mapping[str, bool]) -> Optional[str]:
    """The link warning: one line per parent that will not move, plus a non-dispatching board.

    Only an ACTIONABLE receipt: a link whose parents all finish by themselves earns no
    warning, and the caller's ``gated`` flag stays the demotion fact.
    """
    lines = [f"NOT MOVING ON ITS OWN: `{e.id}` {e.text}" for e in edges if not e.self_moves]
    lines.extend(
        f"board `{slug}` does not dispatch (board.json `dispatch` is off): nothing on it "
        "is picked up."
        for slug, enabled in board_dispatch.items() if not enabled
    )
    return "\n".join(lines) if lines else None


def link_tasks_reported(
    conn: sqlite3.Connection,
    parent_id: str,
    child_id: str,
    *,
    expected_child_run_id: Optional[int] = None,
    profiles=None,
    board: Optional[str] = None,
) -> LinkOutcome:
    """Link ``parent_id -> child_id`` and report what the new dependency set can do on its own.

    The ONE write path behind both doors: ``kanban_db.link_tasks`` is this function's
    ``gated`` flag, and ``kanban_link`` / ``hermes kanban link`` report its ``parents`` and
    ``warning`` instead of a bare "Linked". A gated child is a card that will NOT run until
    the parents finish, so the receipt has to name the parents that will never finish on
    their own — that is the difference between a wait and a silent park.

    ``profiles`` is the caller's live-profile list (None means "not injected": the
    dead-assignee probe is skipped, not guessed). ``board`` names the board whose dispatch
    admission is reported; it is never inferred from ``conn``.
    """
    if parent_id == child_id:
        raise ValueError("a task cannot depend on itself")
    gated = False
    with _kb.write_txn(conn):
        missing = _kb._missing_task_ids(conn, [parent_id, child_id])
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        child = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (child_id,),
        ).fetchone()
        if child["status"] == "running" and (
            expected_child_run_id is None
            or child["current_run_id"] != expected_child_run_id
        ):
            raise ValueError(f"cannot link {parent_id} -> {child_id}: child is already running")
        if _would_cycle(conn, parent_id, child_id):
            raise ValueError(f"linking {parent_id} -> {child_id} would create a cycle")
        _kb._link(conn, parent_id, child_id)
        # If child was ready but parent is not yet terminal, demote child to todo
        # (archived counts as terminal, matching _parents_satisfied/recompute_ready).
        if _kb._task_status(conn, parent_id) not in ("done", "archived"):
            cur = conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                (child_id,),
            )
            gated = cur.rowcount == 1
            if gated:
                _kb._append_event(
                    conn,
                    child_id,
                    "dependency_wait",
                    {"reason": "parent_not_done", "demoted": True, "parent": parent_id},
                )
        _kb._append_event(
            conn,
            child_id,
            "linked",
            {"parent": parent_id, "child": child_id},
        )
        _kb._inherit_notify_subs(conn, child_id, (parent_id,))
    edges = parent_edges(conn, child_id, profiles)
    dispatch = {} if board is None else {board: _kb.board_dispatch_enabled(board)}
    return LinkOutcome(
        gated=gated,
        parents=edges,
        standing=[edge.id for edge in edges if not edge.self_moves],
        warning=link_warning(edges, dispatch),
        board_dispatch=dispatch,
    )


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
