"""Task graph initialization and atomic decomposition persistence."""
from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # typing only: importing kanban_db here would close an import cycle
    from hermes_cli.kanban_db import DecomposeRefusal, TriageEscalationRefusal

def inherit_creator_origin(
    conn: sqlite3.Connection, task_id: str, creator_task_id: Optional[str], *,
    created_at: int,
) -> None:
    """Copy durable origin inside creation's transaction, never adding dependencies."""
    if not creator_task_id:
        return
    from hermes_cli.kanban_db import _inherit_notify_subs

    conn.execute(
        "UPDATE tasks SET session_id = COALESCE(session_id, "
        "(SELECT session_id FROM tasks WHERE id = ?)) WHERE id = ?",
        (creator_task_id, task_id),
    )
    _inherit_notify_subs(conn, task_id, (creator_task_id,), created_at=created_at)


def initial_task_state(
    conn: sqlite3.Connection, parents: tuple[str, ...], initial_status: str,
    triage: bool, tenant: Optional[str],
) -> tuple[str, Optional[str]]:
    """Resolve state and tenant under the creator's write transaction.

    Parent order breaks ties in this soft namespace; explicit tenant wins.
    Validate parents even for parked tasks so links never dangle.
    """
    rows = {}
    if parents:
        rows = {row["id"]: row for row in conn.execute(
            "SELECT id, status, tenant FROM tasks WHERE id IN "
            "(" + ",".join("?" * len(parents)) + ")", parents,
        )}
        missing = [pid for pid in parents if pid not in rows]
        if missing:
            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
        if tenant is None:
            tenant = next((rows[pid]["tenant"] for pid in parents if rows[pid]["tenant"]), None)
    if initial_status == "blocked":
        return "blocked", tenant
    if triage:
        return "triage", tenant
    if any(row["status"] != "done" for row in rows.values()):
        return "todo", tenant
    return "ready", tenant


def _validate_children_graph(children: list) -> None:
    """DB-free shape check + Kahn's cycle check on the sibling graph (a cycle
    would deadlock every involved child in ``todo`` forever)."""
    for idx, child in enumerate(children):
        if not isinstance(child, dict):
            raise ValueError(f"child[{idx}] is not a dict")
        title = child.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"child[{idx}].title is required")
        parents_idx = child.get("parents") or []
        if not isinstance(parents_idx, list):
            raise ValueError(f"child[{idx}].parents must be a list")
        for p in parents_idx:
            if not isinstance(p, int) or p < 0 or p >= len(children):
                raise ValueError(f"child[{idx}].parents[{p}] is not a valid index into children")
            if p == idx:
                raise ValueError(f"child[{idx}] cannot list itself as a parent")

    in_deg = [0] * len(children)
    adj: list[list[int]] = [[] for _ in children]
    for i, c in enumerate(children):
        for p in (c.get("parents") or []):
            adj[p].append(i)
            in_deg[i] += 1
    queue = [i for i in range(len(children)) if in_deg[i] == 0]
    seen = 0
    while queue:
        seen += 1
        for nb in adj[queue.pop()]:
            in_deg[nb] -= 1
            if in_deg[nb] == 0:
                queue.append(nb)
    if seen != len(children):
        raise ValueError("cyclic dependency detected in decomposed children list")


def decompose_triage_task(
    conn: sqlite3.Connection, task_id: str, *, root_assignee: Optional[str], children: list[dict],
    author: Optional[str] = None, auto_promote: bool = True,
    allow_off_board_ask: bool = False,
) -> Optional[list[str] | TriageEscalationRefusal | DecomposeRefusal]:
    """Fan a triage task out into children and move the root to ``todo``; the root
    waits on every child and wakes (``ready``) when all are done.

    ``children``: dicts of ``title`` (required), ``body``, ``assignee``,
    ``parents`` (indices into this list), optional workspace overrides.
    Returns child ids in input order, or None when the root is missing / not
    in triage, or has already decomposed. Atomic: malformed entries abort fan-out.

    A root the block-loop breaker parked returns a ``TriageEscalationRefusal`` (falsy)
    instead: it is an escalation for a human, and fanning it out would hand the same
    unchanged card back to the board — where it blocks again and manufactures another
    graph of children that inherit the failing context.
    """
    from hermes_cli import kanban_db as kb_triage
    from hermes_cli.kanban_db import (
        _canonical_assignee, _link, _append_event, _insert_comment,
        write_txn, recompute_ready,
    )

    if not children:
        return None
    if root_assignee is not None:
        root_assignee = _canonical_assignee(root_assignee)
    _validate_children_graph(children)

    # ONE txn so the fan-out is atomic; helpers that open their own write_txn
    # (create_task, link_tasks, add_comment) must not be called in here.
    now = int(time.time())
    with write_txn(conn):
        root_row = conn.execute(
            "SELECT id, status, tenant, workspace_kind, workspace_path "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if root_row is None or root_row["status"] != "triage":
            return None
        # Guard before the fan-out: both refusals are decided inside this txn, so neither a
        # concurrent escalation nor a record that now says the work is already decided
        # (approved / superseded / in review / live branch / live run) can slip a graph
        # past it. The record refusal is also WRITTEN here, so it survives the attempt.
        refusal = kb_triage.decompose_refusal_guard(conn, task_id, author=author)
        if refusal is not None:
            return refusal
        # Dependency links alone do not imply lineage. The completion event is
        # committed with the graph, and survives re-triage or unlinking.
        if conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'decomposed' LIMIT 1",
            (task_id,),
        ).fetchone():
            return None
        child_ids = [
            _insert_decomposed_child(
                conn, task_id, root_row, child, author, now,
                allow_off_board_ask=allow_off_board_ask,
            )
            for child in children
        ]
        # Sibling edges within the decomposed graph.
        for idx, child in enumerate(children):
            for p_idx in child.get("parents") or []:
                parent_id, child_id = child_ids[p_idx], child_ids[idx]
                _link(conn, parent_id, child_id, cause="decompose")
                _append_event(conn, child_id, "linked", {"parent": parent_id, "child": child_id})
        # Root waits for the whole graph: link it under EVERY child (simpler
        # than computing leaves; cycle-free since the root is only ever a child).
        for cid in child_ids:
            _link(conn, cid, task_id, cause="decompose")
        # Flip the root triage -> todo, assignee -> orchestrator.
        sets = ["status = 'todo'"]
        params: list[Any] = []
        if root_assignee is not None:
            sets.append("assignee = ?")
            params.append(root_assignee)
        params.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", tuple(params))
        if author and author.strip():
            _insert_comment(
                conn, task_id, author.strip(),
                "Decomposed into " + ", ".join(child_ids)
                + ". Root will wake when all children complete.",
                now,
            )
        _append_event(
            conn, task_id, "decomposed", {"child_ids": child_ids, "root_assignee": root_assignee},
        )
    # Outside the txn (own IMMEDIATE txn). ``auto_promote=False`` leaves the
    # children in ``todo`` for manual-review-first workflows.
    if auto_promote:
        recompute_ready(conn)
    return child_ids


def _insert_decomposed_child(
    conn: sqlite3.Connection, root_id: str, root_row: sqlite3.Row, child: dict,
    author: Optional[str], now: int, *, allow_off_board_ask: bool = False,
) -> str:
    """Insert one decomposed child as ``todo`` (linked under the root later so
    the dispatcher only ever sees a coherent graph); returns its id.

    Workspace: per-child override wins, else inherit the root's kind. Path
    inherits only when kinds match (a 'dir' child must not point at the
    root's worktree) and NEVER for worktrees — siblings dispatch concurrently
    and one shared checkout would put them all on the first sibling's branch
    with no lock; leaving it unset makes dispatch materialize a fresh
    ``<repo>/.worktrees/<child-id>`` per child from the board anchor.
    """
    from hermes_cli.kanban_db import (
        _new_task_id, _canonical_assignee, _append_event, _apply_board_priority_policy,
        board_for_connection, _resolve_operator_ask, operator_ask_event,
    )
    from hermes_cli.kanban_register import (
        OFF_BOARD_ASK_EVENT, apply_stamp, guard_new_ask_home, normalise_body,
    )

    root_ws_kind = root_row["workspace_kind"] or "scratch"
    child_ws_kind = child.get("workspace_kind") or root_ws_kind
    if child.get("workspace_path"):
        child_ws_path = child.get("workspace_path")
    elif child_ws_kind == "worktree":
        child_ws_path = None
    elif child_ws_kind == root_ws_kind:
        child_ws_path = root_row["workspace_path"]
    else:
        child_ws_path = None
    new_id = _new_task_id()
    body = child.get("body")
    child_assignee = _canonical_assignee(child.get("assignee"))
    # THE SECOND WRITER (card t_0396c536; restored by t_ecbfb34b). A decomposed child is born
    # here rather than through ``create_task``, so the board's bound has to be reached from
    # THIS insert too: wired into one writer only, a fan-out put every child outside its
    # band while the root's own card looked correctly banded. The board is resolved from
    # THIS connection - a caller holding a ``--board`` override is not on the current board,
    # and reading the ambient one would bound the children by somebody else's table.
    priority, policy_provenance = _apply_board_priority_policy(
        0, assignee=child_assignee, board=board_for_connection(conn),
        title=child["title"].strip(), body=body if isinstance(body, str) else None,
    )
    # The ask stamp rides this insert for the SAME reason as the policy above: a
    # decomposition is a card born outside ``create_task``, and the children of an ask are
    # exactly the "deep chains" the register tree lost. The root's own body is the
    # reference - a fan-out of a card in service of an ask serves that ask.
    _board = board_for_connection(conn)
    ask_ref = _resolve_operator_ask(
        conn, board=_board, parents=[root_id], body=body if isinstance(body, str) else None,
        serves=None,
    )
    # THE ASK-HOME DOOR (card t_abefe660): the SAME guard the create seam applies, at the
    # second writer. A decomposed child that IS a new ask (the root is the register, or a
    # card whose ask half is empty) must land on the register's own board; a child that
    # inherits an existing ask id is not gated. Refused before the INSERT.
    ask_off_board = guard_new_ask_home(
        ask_ref, board=_board, title=child["title"].strip(),
        allow_off_board=allow_off_board_ask, caller=author,
    )
    ask_pair = ask_ref.for_card(new_id) if ask_ref is not None else None
    # No ask pair: normalise the body rather than dropping it (card t_4576e74f). The old
    # ``body if isinstance(body, str) else None`` silently NULLed a bytes/non-str child
    # body; ``normalise_body`` stores it as text, matching the create seam exactly.
    child_body = apply_stamp(body, *ask_pair) if ask_pair else normalise_body(body)
    conn.execute(
        "INSERT INTO tasks "
        "(id, title, body, assignee, status, priority, workspace_kind, "
        " workspace_path, tenant, created_at, created_by) "
        "VALUES (?, ?, ?, ?, 'todo', ?, ?, ?, ?, ?, ?)",
        (
            new_id, child["title"].strip(), child_body,
            child_assignee, priority, child_ws_kind, child_ws_path,
            root_row["tenant"], now, (author or "decomposer"),
        ),
    )
    _append_event(
        conn, new_id, "created",
        {
            "by": author or "decomposer",
            "from_decompose_of": root_id,
            **(operator_ask_event(ask_ref, ask_pair)),
            **((
                {"priority_policy": policy_provenance}
            ) if policy_provenance else {}),
        },
    )
    inherit_creator_origin(conn, new_id, root_id, created_at=now)
    # The hatch's own record, so an off-board NEW ask let through on purpose is visible.
    if ask_off_board is not None:
        _append_event(conn, new_id, OFF_BOARD_ASK_EVENT, ask_off_board)
    return new_id
