"""The dependency RECEIPT: what a new parent edge can do on its own.

A link gates a child behind a parent, but a gate is not a promise — the child only runs once
the parent FINISHES, and a parent parked in ``review``/``triage``/``blocked``, or assigned to
a profile that no longer exists, will never move by itself. This sibling owns the write paths
(:func:`link_tasks_reported`, :func:`unlink_tasks`), the per-edge classification every parent
of the child gets, the warning the doors report, and the one payload (:func:`receipt_fields`)
each door renders — the link door and the create door report the same receipt in the same
words, because a card filed behind a standing parent is the same silent park as one linked
into it.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached late-bound via
``_kb`` (import-cycle breaking) so monkeypatching ``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Mapping, Optional

from hermes_cli.kanban_parent_state import ParentState, parent_promotion_state

#: A parent in one of these has finished: the kernel promotes a child past it
#: (``_parents_satisfied``/``recompute_ready``), so such an edge is no gate at all.
TERMINAL_PARENT_STATUSES = ("done", "archived")


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


def _receipt(edges: list, *, gated: bool, board: Optional[str] = None) -> LinkOutcome:
    """Build one receipt off already-classified edges — the ONLY place one is constructed."""
    dispatch = {} if board is None else {board: _kb.board_dispatch_enabled(board)}
    return LinkOutcome(
        gated=gated,
        parents=edges,
        standing=[edge.id for edge in edges if not edge.self_moves],
        warning=link_warning(edges, dispatch),
        board_dispatch=dispatch,
    )


def dependency_outcome(
    conn: sqlite3.Connection,
    child_id: str,
    *,
    gated: bool,
    profiles=None,
    board: Optional[str] = None,
) -> LinkOutcome:
    """The receipt for ``child_id``'s dependency set, as it stands.

    ``gated`` is the caller's own demotion fact - the link door's ready->todo flip, the
    create door's birth behind a parent that has not finished - and is passed in rather
    than inferred: the two doors mean different things by it, and each already reports it
    on its own surface. The edges, the standing set and the warning are built HERE, once,
    so no door can report a dependency set in wording another door would not.
    """
    return _receipt(parent_edges(conn, child_id, profiles), gated=gated, board=board)


def created_outcome(
    conn: sqlite3.Connection, child_id: str, *, profiles=None, board: Optional[str] = None,
) -> LinkOutcome:
    """The receipt for a card FILED with parents: what its new dependency set can do alone.

    A create door that answers only the new id leaves the filer to discover the park later,
    and the ``dependency_wait`` event it writes is not the answer: a ``running`` parent still
    finishes, while one parked in ``review`` or behind a dead assignee never moves. ``gated``
    is true iff the card is parked in ``todo`` behind a parent that has not finished -
    ``done``/``archived`` are terminal, the kernel's own promotion rule.
    """
    edges = parent_edges(conn, child_id, profiles)
    gated = _kb._task_status(conn, child_id) == "todo" and any(
        edge.status not in TERMINAL_PARENT_STATUSES for edge in edges)
    return _receipt(edges, gated=gated, board=board)


def receipt_fields(outcome: LinkOutcome) -> dict:
    """The door payload for one receipt: the same keys on every surface that reports one.

    A create that carried NO parents answers exactly what it always did — ``gated: False``
    and nothing else — because there is no edge to describe; the receipt appears only when
    there is a dependency set to report. ``gated_by`` names the parent that parked the card
    (the first edge, in id order, that has not finished), so an agent reads WHY its new card
    sits in ``todo`` from the call that created it instead of a second round trip.
    """
    if not outcome.parents:
        return {"gated": False}
    open_edges = [e for e in outcome.parents if e.status not in TERMINAL_PARENT_STATUSES]
    payload = {
        "gated": outcome.gated,
        "parents_state": [{"id": e.id, "status": e.status, "code": e.code,
                           "self_moves": e.self_moves} for e in outcome.parents],
        "standing": outcome.standing,
        "warning": outcome.warning,
        "board_dispatch": outcome.board_dispatch,
    }
    if outcome.gated and open_edges:
        payload["gated_by"] = open_edges[0].id
    return payload


def unlink_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """Remove one ``parent_id -> child_id`` edge; returns whether one was removed.

    The inverse door of :func:`link_tasks_reported`, and it re-gates the child at once, the
    way ``complete_task``/``unblock_task`` do: without it the child stays stuck in ``todo``
    until the next dispatcher tick or a manual ``hermes kanban recompute`` (issue #22459).
    """
    with _kb.write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?", (parent_id, child_id),
        )
        removed = cur.rowcount > 0
        if removed:
            _kb._append_event(conn, child_id, "unlinked",
                              {"parent": parent_id, "child": child_id})
    if removed:
        _kb.recompute_ready(conn)
    return removed


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
    return dependency_outcome(conn, child_id, gated=gated, profiles=profiles, board=board)


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
