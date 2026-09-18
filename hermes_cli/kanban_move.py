"""Move a card — and its whole parent/child component — between boards.

``boards export``/``import`` copy a whole board; this is the per-card verb that
``export`` can't express. The card travels with its comments, events, runs and
file attachments; gateway subscriptions and machine-local runtime state
(claims, worker PIDs, workspace paths) are deliberately NOT carried.

**A move severs an edge only where the caller declares it.** The unit of
movement is the requested card's *link-closed set*: the card plus every card
reachable through ``task_links`` parent/child edges (undirected BFS). A card
that is linked to anything else is refused unless the caller opts into moving
the whole set with ``with_links=True`` (CLI ``--with-links`` / ``--link-closed``)
— a silent sever is how a child ends up with zero parents on the target and gets
auto-promoted, whereupon the dispatcher spawns a worker for a card whose
dependency just landed on another board. Where the set genuinely cannot travel
as a whole (its gating edge points at a card that has to stay put), the operator
declares the cut edge by edge (CLI ``--sever-edge PARENT:CHILD``, one per leaving
edge) together with a mandatory ``--sever-reason``: declared cuts are audited on
both boards as ``link_severed`` events, and no declared cut may strand a card
that sits in a dispatcher pool lane. This mirrors the safety semantics of
``~/.hermes/kanban/migrations/migrate_apr25.py``.

**Ids are preserved**, so tombstones and lineage stay coherent across boards. A
target row that already holds a moved id is refused unless it carries
``moved_in`` provenance for this source board *and* this id — that case is a
resumed move (the process died between the target commit and the source delete),
and re-running the same command simply finishes the source removal without
duplicating anything.

Two-phase, target-first: the copy (cards + relations + internal link rows) is
committed on the destination board before the source row is removed, so a failed
write can never strand the card. The source board's write lock is held across
the whole move, which both orders the phases and closes the claim race (a
dispatcher cannot claim a card mid-move).

Refuses while ANY card in the set is running (or holds a live claim/run row);
backs up both stores first; writes a ``moved_in`` audit event on the destination
and a ``moved_out`` tombstone event on the source for EVERY card in the set
(keyed to the now-deleted task id — the schema has no FK, so the tombstone event
is the durable "this id left for board X as id X" trail).
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_transfer import _DISPATCHABLE_STATUSES, _snapshot_db

# Columns that record machine-local runtime state and must not follow the card.
_SCRUB_NULL = (
    "claim_lock", "claim_expires", "worker_pid", "current_run_id",
    "last_heartbeat_at", "session_id", "workspace_path", "branch_name",
    "project_id", "last_failure_error",
)

# Columns whose cards the dispatcher can claim on its next tick. Cutting the last
# parent edge under one of these satisfies the card's dependencies, so the
# promotion sweep flips it to ``ready`` and spawns a worker for it — exactly what
# a severed edge must never cause. Same pool lanes ``migrate_apr25.py`` gate 2
# refuses to cut under.
_POOL_LANES = ("todo", "ready", "triage")


def parse_edge_spec(spec: str) -> tuple[str, str]:
    """Parse a CLI ``--sever-edge PARENT:CHILD`` value into an edge tuple.

    Task ids never contain ``:``, so the split is unambiguous.
    """
    text = (spec or "").strip()
    parent_id, sep, child_id = text.partition(":")
    parent_id, child_id = parent_id.strip(), child_id.strip()
    if not sep or not parent_id or not child_id:
        raise ValueError(
            "--sever-edge expects PARENT:CHILD (two task ids separated by ':'), "
            f"got {spec!r}"
        )
    return parent_id, child_id


def _normalize_declared(sever_edges: Optional[Iterable[Any]]) -> list[tuple[str, str]]:
    """Validate + de-duplicate declared severances, keeping the given direction."""
    out: list[tuple[str, str]] = []
    for spec in sever_edges or ():
        if isinstance(spec, str):
            pair = parse_edge_spec(spec)
        else:
            try:
                parent_id, child_id = spec
            except (TypeError, ValueError):
                raise ValueError(
                    "sever_edges entries must be (parent_id, child_id) pairs or "
                    f"'PARENT:CHILD' strings, got {spec!r}"
                ) from None
            pair = (str(parent_id).strip(), str(child_id).strip())
            if not pair[0] or not pair[1]:
                raise ValueError(f"sever_edges entry {spec!r} names an empty task id")
        if pair not in out:
            out.append(pair)
    return out


def _row_to_dict(row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _insert_mapped(conn, table: str, row: dict[str, Any], *, skip: tuple[str, ...] = ()) -> None:
    """Insert ``row`` into ``table``, omitting ``skip`` columns (the auto-increment
    ``id`` on child tables). Column names come from the row's own keys, so the
    INSERT tracks the schema without a hand-written column list."""
    cols = [c for c in row.keys() if c not in skip]
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
        [row[c] for c in cols],
    )


def _backup_board_db(slug: str, ts: int) -> Optional[Path]:
    """WAL-safe snapshot of ``slug``'s DB; None when the board has no DB yet."""
    db_path = kb.kanban_db_path(slug)
    if not db_path.exists():
        return None
    backup_dir = kb.kanban_home() / "kanban" / "backups" / f"move-{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{slug}-before.db"
    _snapshot_db(db_path, dest)
    return dest


def _is_running(conn, task_id: str, task_row) -> bool:
    """True when the card is executing, or holds a live claim / open run row.

    Fail-closed on purpose: a stale ``claim_lock`` with no open run still blocks
    the move, and the operator clears it with ``kanban reclaim``.
    """
    if task_row["status"] == "running" or task_row["current_run_id"] is not None:
        return True
    if "claim_lock" in task_row.keys() and task_row["claim_lock"] is not None:
        return True
    open_run = conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id = ? AND status = 'running' "
        "AND ended_at IS NULL LIMIT 1",
        (task_id,),
    ).fetchone()
    return open_run is not None


def _link_closed_set(
    conn, task_id: str, *, board: str,
    exclude_edges: Iterable[tuple[str, str]] = (),
) -> list[str]:
    """The requested card's link-closed set, in deterministic BFS order.

    Undirected over ``task_links``: a parent and its child are in the same set.
    Raises ``ValueError`` when an edge points at a card that does not exist —
    the set can't be trusted, so the move is refused (fail closed).

    ``exclude_edges`` are directed ``(parent_id, child_id)`` pairs the caller has
    already declared as severed: they are not traversed, so the walk stops at the
    cut instead of swallowing the card on the far side. Only :func:`move_task`
    passes them, and only after checking each pair against ``task_links``.
    """
    skip = {(str(parent_id), str(child_id)) for parent_id, child_id in exclude_edges}
    order: list[str] = [task_id]
    seen = {task_id}
    queue: list[str] = [task_id]
    while queue:
        node = queue.pop(0)
        rows = conn.execute(
            "SELECT parent_id AS other FROM task_links WHERE child_id = ? "
            "UNION SELECT child_id AS other FROM task_links WHERE parent_id = ? "
            "ORDER BY other",
            (node, node),
        ).fetchall()
        for r in rows:
            other = r["other"]
            if (node, other) in skip or (other, node) in skip:
                continue
            if other in seen:
                continue
            if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (other,)).fetchone() is None:
                raise ValueError(
                    f"board {board!r} has a dangling link row referencing unknown task "
                    f"{other!r}; repair it (it cannot be moved) before moving {task_id!r}"
                )
            seen.add(other)
            order.append(other)
            queue.append(other)
    return order


def _leaving_edges(conn, members: Iterable[str]) -> list[tuple[str, str]]:
    """Link rows with exactly ONE endpoint inside ``members`` (deterministic).

    These are the edges a move would cut: the ones that would dangle off the
    moved set and off whatever stays behind.
    """
    inside = set(members)
    if not inside:
        return []
    marks = ", ".join("?" for _ in inside)
    rows = conn.execute(
        f"SELECT parent_id, child_id FROM task_links "
        f"WHERE parent_id IN ({marks}) OR child_id IN ({marks}) "
        f"ORDER BY parent_id, child_id",
        [*inside, *inside],
    ).fetchall()
    return [
        (r["parent_id"], r["child_id"])
        for r in rows
        if (r["parent_id"] in inside) != (r["child_id"] in inside)
    ]


def _moved_in_provenance(conn, task_id: str, *, from_board: str) -> bool:
    """True when the ``task_id`` row on *this* board arrived mid-move from
    ``from_board`` — the target half of an interrupted move whose source delete
    never ran."""
    for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'moved_in' ORDER BY id",
        (task_id,),
    ):
        raw = r["payload"]
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if (
            isinstance(data, dict)
            and data.get("from_board") == from_board
            and data.get("from_task_id") == task_id
        ):
            return True
    return False


def _refuse_partial_set(task_id: str, src_slug: str, members: list[str], edge_count: int) -> str:
    others = len(members) - 1
    return (
        f"task {task_id!r} on board {src_slug!r} is linked to {others} other card(s) "
        f"({edge_count} parent/child edge(s)), so moving it alone would sever those edges "
        f"and can auto-promote an orphaned child. Rerun with --with-links to move the "
        f"whole link-closed set ({len(members)} cards), or unlink the card first. If a "
        f"card in the set has to stay behind, declare every edge the move would cut with "
        f"--sever-edge PARENT:CHILD --sever-reason TEXT."
    )


def _verify_declared_edges(
    conn, declared: list[tuple[str, str]], *, board: str, task_id: str,
) -> list[tuple[str, str]]:
    """Each declared pair must be a real ``task_links`` row between two real cards.

    A typo (or a reversed direction) must not degrade into "the cut happened
    somewhere else": the operator declared a specific edge, so either that edge
    exists or the move is refused.
    """
    not_a_link = [
        (parent_id, child_id)
        for parent_id, child_id in declared
        if conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (parent_id, child_id),
        ).fetchone() is None
    ]
    if not_a_link:
        rendered = ", ".join(f"{p} -> {c}" for p, c in not_a_link)
        raise ValueError(
            f"--sever-edge names edge(s) that are not a parent/child link on board "
            f"{board!r} (typo?): {rendered}. Check the direction and declare the exact "
            f"PARENT:CHILD row, or drop the declaration."
        )
    unknown = sorted({
        tid
        for parent_id, child_id in declared
        for tid in (parent_id, child_id)
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone() is None
    })
    if unknown:
        raise ValueError(
            f"--sever-edge names card(s) that do not exist on board {board!r}: "
            f"{', '.join(repr(t) for t in unknown)}. Repair the dangling link row (or "
            f"drop the declaration) before moving {task_id!r}."
        )
    return list(declared)


def _refuse_undeclared_leaving_edges(
    task_id: str, src_slug: str, undeclared: list[tuple[str, str]], *, with_links: bool,
) -> str:
    """Name every edge this move would still cut, plus the exact flag that declares it."""
    rendered = "\n".join(f"  {parent_id} -> {child_id}" for parent_id, child_id in undeclared)
    flags = " ".join(f"--sever-edge {p}:{c}" for p, c in undeclared)
    tail = (
        "Rerun with --with-links as well to move the whole link-closed set instead, or "
        "unlink the card first."
        if with_links
        else "Or rerun with --with-links to move the whole link-closed set, or unlink the "
        "card first."
    )
    return (
        f"task {task_id!r} on board {src_slug!r} would still sever "
        f"{len(undeclared)} undeclared parent/child edge(s):\n{rendered}\n"
        f"Every edge a move leaves behind must be declared — nothing is severed silently. "
        f"Declare the rest with --sever-reason TEXT and the exact edge flags: {flags}. "
        f"{tail}"
    )


def _refuse_travelling_edges(
    travelling: list[tuple[str, str]], *, src_slug: str, dst_slug: str,
) -> str:
    """A declared edge whose BOTH ends move is not a cut — it is simply carried."""
    rendered = ", ".join(f"{parent_id} -> {child_id}" for parent_id, child_id in travelling)
    return (
        f"--sever-edge declares edge(s) that travel WITH the move set, so nothing would be "
        f"severed (both endpoints of each are moving to board {dst_slug!r}): {rendered}. "
        f"Those rows are carried to the target; drop the declaration, or declare a "
        f"different edge — one that leaves a card behind on board {src_slug!r}."
    )


def _promotion_hazard(
    *,
    src_slug: str,
    dst_slug: str,
    declared: list[tuple[str, str]],
    inside: set[str],
    status_of,
) -> Optional[str]:
    """Refuse a declared cut that can auto-promote a card sitting in a pool lane.

    Mirrors ``migrate_apr25.py`` gate 2. Cutting the last parent edge under a card
    in ``todo``/``ready``/``triage`` satisfies its dependencies, so the promotion
    sweep flips it to ``ready`` and a worker is spawned for it. Whether that card
    is the one left behind (its parent moved away) or the one that lands on the
    target (its parent stayed behind), the orphan is the same defect — so both
    directions are refused, and no flag overrides it.
    """
    for parent_id, child_id in declared:
        status = status_of(child_id)
        if parent_id in inside:
            # The child stays behind while its parent's edge is cut away.
            if status in _POOL_LANES:
                return (
                    f"severing {parent_id} -> {child_id} removes a parent edge from "
                    f"{child_id!r} (status={status!r} on board {src_slug!r}), which sits "
                    f"in a dispatcher pool lane — it could be auto-promoted without its "
                    f"dependency. No flag overrides this: finish or park {child_id!r} "
                    f"first, then rerun the move."
                )
        elif status in _POOL_LANES:
            # The child travels, but without the parent row it depends on.
            return (
                f"severing {parent_id} -> {child_id} drops {child_id!r} "
                f"(status={status!r}) onto board {dst_slug!r} with no parent row of its "
                f"own, so it could be auto-promoted there. No flag overrides this: finish "
                f"or park {child_id!r} first, then rerun the move."
            )
    return None


def _copy_to_target(
    src_conn, dst_conn, task_row, *, src_slug: str, dst_slug: str,
    declared: Iterable[tuple[str, str]] = (), sever_reason: Optional[str] = None,
) -> tuple[dict[str, int], list[str], bool]:
    """Copy the card + relations into ``dst_conn`` (caller's txn), keeping its id.

    ``declared`` are the severances this move cuts (see :func:`move_task`): for
    every one touching this card, a ``link_severed`` event records the cut here,
    on the side that receives the card, carrying the id of the card left behind.

    Returns ``(counts, warnings, parked_triage)``.
    """
    task_id = task_row["id"]
    counts: dict[str, int] = {"tasks": 0, "comments": 0, "events": 0, "runs": 0, "attachments": 0}
    warnings: list[str] = []
    parked_triage = False

    copied = _row_to_dict(task_row)
    for col in _SCRUB_NULL:
        copied[col] = None
    copied["consecutive_failures"] = 0
    # A dir/worktree card pointed at the SOURCE board's workspace; the dispatcher
    # here can't rebuild it, so park it for a human (mirrors import's behaviour).
    if copied["workspace_kind"] in ("dir", "worktree") and copied["status"] in _DISPATCHABLE_STATUSES:
        copied["status"] = "triage"
        parked_triage = True
        warnings.append(
            "parked in triage — its workspace was a directory/worktree on the source board"
        )
    _insert_mapped(dst_conn, "tasks", copied)
    counts["tasks"] += 1

    # Runs first so their old->new id map can re-anchor event run_id pointers.
    run_map: dict[int, int] = {}
    for r in src_conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,)
    ):
        old_run_id = r["id"]
        d = _row_to_dict(r)
        d["task_id"] = task_id
        _insert_mapped(dst_conn, "task_runs", d, skip=("id",))
        new_run_id = int(dst_conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        run_map[old_run_id] = new_run_id
        counts["runs"] += 1

    for r in src_conn.execute(
        "SELECT * FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,)
    ):
        d = _row_to_dict(r)
        d["task_id"] = task_id
        _insert_mapped(dst_conn, "task_comments", d, skip=("id",))
        counts["comments"] += 1

    for r in src_conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
    ):
        d = _row_to_dict(r)
        d["task_id"] = task_id
        rid = d["run_id"]
        d["run_id"] = run_map.get(rid) if rid is not None else None
        _insert_mapped(dst_conn, "task_events", d, skip=("id",))
        counts["events"] += 1

    target_dir = kb.task_attachments_dir(task_id, board=dst_slug)
    for r in src_conn.execute(
        "SELECT * FROM task_attachments WHERE task_id = ? ORDER BY id", (task_id,)
    ):
        d = _row_to_dict(r)
        src_path = Path(d["stored_path"])
        if not src_path.is_file():
            warnings.append(f"attachment {d['filename']!r} missing on disk — dropped")
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = kb._collision_free_path(target_dir, Path(d["stored_path"]).name)
        shutil.copy2(src_path, dest)
        d["task_id"] = task_id
        d["stored_path"] = str(dest)
        _insert_mapped(dst_conn, "task_attachments", d, skip=("id",))
        counts["attachments"] += 1

    kb._append_event(
        dst_conn, task_id, "moved_in",
        {"from_board": src_slug, "from_task_id": task_id},
    )
    # Declared severance: this edge was cut, not carried. The counterpart id is
    # the only handle left once the source row is gone, so record it here too.
    # (``role`` names the MOVED card's side of the edge; the source-side event of
    # the same cut names the card that stayed — see `_remove_from_source`.)
    for parent_id, child_id in declared:
        if task_id not in (parent_id, child_id):
            continue
        outside_id = child_id if task_id == parent_id else parent_id
        kb._append_event(
            dst_conn, task_id, "link_severed",
            {
                "role": "parent" if task_id == parent_id else "child",
                "outside_task_id": outside_id,
                "from_board": src_slug,
                "reason": sever_reason,
                "note": "declared severance — the other end stayed on the source board",
            },
        )
    return counts, warnings, parked_triage


def _internal_edges(conn, members: Iterable[str]) -> list[tuple[str, str]]:
    """Every link row whose BOTH endpoints are in ``members`` (deterministic)."""
    ids = list(members)
    if not ids:
        return []
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT parent_id, child_id FROM task_links "
        f"WHERE parent_id IN ({marks}) AND child_id IN ({marks}) "
        f"ORDER BY parent_id, child_id",
        [*ids, *ids],
    ).fetchall()
    return [(r["parent_id"], r["child_id"]) for r in rows]


def _copy_links(dst_conn, edges: Iterable[tuple[str, str]]) -> int:
    """Carry the set's internal edges to the target (idempotent: a resumed move
    re-inserts rows the interrupted attempt already wrote)."""
    edges = list(edges)
    for parent_id, child_id in edges:
        dst_conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (parent_id, child_id),
        )
    return len(edges)


def _remove_from_source(
    conn, task_ids: Iterable[str], *, dst_slug: str, src_slug: str,
    declared: Iterable[tuple[str, str]] = (), sever_reason: Optional[str] = None,
) -> None:
    """Delete the moved cards + all their relations from the source board
    (caller's txn), then write a tombstone ``moved_out`` event per id.

    ``_delete_task_relations`` drops any link row touching a moved card; with
    ``declared`` empty the set is link-closed, so no edge survives on the source
    and none dangles. Each declared severance is written as a ``link_severed``
    event on the card that STAYS BEHIND, before the link row that recorded the
    edge is dropped — an audited cut, not a silent one.
    """
    ids = list(task_ids)
    inside = set(ids)
    for parent_id, child_id in declared:
        # Exactly one endpoint moved; name it, and the card that stays behind.
        moved_id = parent_id if parent_id in inside else child_id
        outside_id = child_id if parent_id in inside else parent_id
        kb._append_event(
            conn, outside_id, "link_severed",
            {
                "role": "child" if outside_id == child_id else "parent",
                "moved_task_id": moved_id,
                "moved_to_board": dst_slug,
                "reason": sever_reason,
                "note": "declared severance — the other end moved to another board",
            },
        )
    for tid in ids:
        kb._delete_task_relations(conn, tid)
        conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    for tid in ids:
        kb._append_event(
            conn, tid, "moved_out",
            {"to_board": dst_slug, "to_task_id": tid, "from_board": src_slug},
        )


def move_task(
    task_id: str,
    target_slug: str,
    *,
    source_slug: Optional[str] = None,
    with_links: bool = False,
    link_closed: Optional[bool] = None,
    sever_edges: Optional[Iterable[Any]] = None,
    sever_reason: Optional[str] = None,
) -> dict[str, Any]:
    """Move ``task_id`` (with its whole link-closed set when ``with_links``) to
    ``target_slug``; returns a summary dict.

    Raises ``ValueError`` on a bad slug, a missing task, a same-board move, a
    card linked to others without ``with_links``, a running card anywhere in the
    set, or an id that already exists on the target without ``moved_in``
    provenance; ``FileNotFoundError``/``OSError`` on a missing store.

    ``link_closed`` is a keyword alias for ``with_links`` (mirroring the CLI's
    ``--link-closed``).

    ``sever_edges`` is the ONLY way a move may cut a parent/child edge: an
    iterable of ``(parent_id, child_id)`` pairs (or ``"PARENT:CHILD"`` strings)
    the caller declares as cut, with ``sever_reason`` mandatory — a cut is
    permanent and audited, so no reason means no cut. Every edge the move leaves
    behind must be declared (an undeclared one refuses and names itself), every
    declared pair must really be a leaving edge (a typo, or an edge whose both
    ends move, refuses), and no declared cut may strand a card that sits in a
    dispatcher pool lane (``todo``/``ready``/``triage``) on either side — that is
    exactly how an orphan gets auto-promoted and a worker spawned for it. The
    result carries ``severed_links`` (``[[parent_id, child_id], ...]``) only when
    a cut happened, so callers that never declare severance see the old shape.
    """
    if link_closed is not None:
        with_links = bool(link_closed)

    declared = _normalize_declared(sever_edges)
    if declared and not (sever_reason or "").strip():
        raise ValueError(
            f"declaring --sever-edge without --sever-reason is refused: a severed "
            f"dependency edge is permanent and audited on both boards, so record why "
            f"({len(declared)} edge(s) declared). Rerun with --sever-reason TEXT, or "
            f"move the whole link-closed set with --with-links instead."
        )

    dst_slug = kb._require_slug(target_slug)
    src_slug = kb._normalize_board_slug(source_slug) or kb.get_current_board()
    if src_slug == dst_slug:
        raise ValueError(f"cannot move a card from board {src_slug!r} to itself")
    if not kb.board_exists(src_slug):
        raise ValueError(f"source board {src_slug!r} does not exist")
    if not kb.board_exists(dst_slug):
        raise ValueError(f"target board {dst_slug!r} does not exist")

    ts = int(time.time())
    backups = {
        "source": _backup_board_db(src_slug, ts),
        "target": _backup_board_db(dst_slug, ts),
    }

    with kbc.connect_closing(board=src_slug) as src_conn, \
            kbc.connect_closing(board=dst_slug) as dst_conn:
        with kbc.write_txn(src_conn):
            task_row = src_conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_row is None:
                raise ValueError(f"task {task_id!r} not found on board {src_slug!r}")

            # The unit of movement: the whole link-closed set — minus the edges the
            # operator declared as severed, which stop the walk — or nothing.
            if declared:
                declared = _verify_declared_edges(
                    src_conn, declared, board=src_slug, task_id=task_id,
                )
            if declared:
                members = (
                    _link_closed_set(
                        src_conn, task_id, board=src_slug, exclude_edges=declared,
                    )
                    if with_links
                    else [task_id]
                )
            else:
                members = _link_closed_set(src_conn, task_id, board=src_slug)
            if len(members) > 1 and not with_links:
                raise ValueError(
                    _refuse_partial_set(
                        task_id, src_slug, members,
                        len(_internal_edges(src_conn, members)),
                    )
                )

            if declared:
                # Only an edge with exactly ONE endpoint in the move set is a cut.
                # Everything else either goes undeclared (refused — nothing is
                # severed silently) or travels with the set (nothing to sever).
                leaving = _leaving_edges(src_conn, members)
                leaving_set = set(leaving)
                undeclared = [edge for edge in leaving if edge not in set(declared)]
                if undeclared:
                    raise ValueError(
                        _refuse_undeclared_leaving_edges(
                            task_id, src_slug, undeclared, with_links=with_links,
                        )
                    )
                travelling = [edge for edge in declared if edge not in leaving_set]
                if travelling:
                    raise ValueError(
                        _refuse_travelling_edges(
                            travelling, src_slug=src_slug, dst_slug=dst_slug,
                        )
                    )

                def _status_of(tid: str, _conn=src_conn) -> Optional[str]:
                    row = _conn.execute(
                        "SELECT status FROM tasks WHERE id = ?", (tid,)
                    ).fetchone()
                    return row["status"] if row is not None else None

                hazard = _promotion_hazard(
                    src_slug=src_slug, dst_slug=dst_slug, declared=declared,
                    inside=set(members), status_of=_status_of,
                )
                if hazard:
                    raise ValueError(hazard)

            rows = {task_id: task_row}
            for tid in members:
                if tid == task_id:
                    continue
                rows[tid] = src_conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (tid,)
                ).fetchone()
                if _is_running(src_conn, tid, rows[tid]):
                    raise ValueError(
                        f"task {tid!r} is running on board {src_slug!r} (a live claim or run); "
                        f"it is in the link-closed set for {task_id!r} — reclaim or archive it "
                        f"first, then move the set"
                    )
            if _is_running(src_conn, task_id, task_row):
                raise ValueError(
                    f"task {task_id!r} is running on board {src_slug!r}; "
                    f"reclaim or archive it first, then move"
                )

            # Ids are preserved; a collision must be an interrupted move.
            resumed: set[str] = set()
            for tid in members:
                occupied = dst_conn.execute(
                    "SELECT 1 FROM tasks WHERE id = ?", (tid,)
                ).fetchone()
                if occupied is None:
                    continue
                if _moved_in_provenance(dst_conn, tid, from_board=src_slug):
                    resumed.add(tid)
                    continue
                raise ValueError(
                    f"task id {tid!r} already exists on board {dst_slug!r} and was not moved "
                    f"there from board {src_slug!r}; refusing to overwrite or remap it — "
                    f"archive or rename the target row first"
                )

            edges = _internal_edges(src_conn, members)

            # Phase 1 — target write txn commits BEFORE any source delete. A crash
            # after this point leaves a resumed-able state (see `resumed` above).
            counts: dict[str, int] = {
                "tasks": 0, "comments": 0, "events": 0, "runs": 0, "attachments": 0,
            }
            warnings: list[str] = []
            parked_triage = False
            with kbc.write_txn(dst_conn):
                for tid in members:
                    if tid in resumed:
                        # Copy phase already committed by the interrupted attempt.
                        continue
                    card_counts, card_warnings, card_parked = _copy_to_target(
                        src_conn, dst_conn, rows[tid],
                        src_slug=src_slug, dst_slug=dst_slug,
                        declared=declared, sever_reason=sever_reason,
                    )
                    for key, value in card_counts.items():
                        counts[key] += value
                    warnings.extend(card_warnings)
                    parked_triage = parked_triage or card_parked
                link_count = _copy_links(dst_conn, edges)

            # Phase 2 — source removal (same txn as the lock), with the declared
            # cuts audited on the cards that stay behind.
            _remove_from_source(
                src_conn, members, dst_slug=dst_slug, src_slug=src_slug,
                declared=declared, sever_reason=sever_reason,
            )

        # No re-gating pass is needed: outside a declared severance the set is
        # link-closed, so no card left behind lost a parent to this move — and a
        # declared cut under a pool-lane card was refused before anything moved.

    result = {
        "from_board": src_slug,
        "to_board": dst_slug,
        "from_task_id": task_id,
        "to_task_id": task_id,
        "moved_task_ids": list(members),
        "link_count": link_count,
        "counts": counts,
        "parked_triage": parked_triage,
        "warnings": warnings,
        "backups": {
            "source": str(backups["source"]) if backups["source"] else None,
            "target": str(backups["target"]) if backups["target"] else None,
        },
    }
    if declared:
        # Present only when a cut actually happened, so every caller that never
        # declares severance keeps the old result shape.
        result["severed_links"] = [[parent_id, child_id] for parent_id, child_id in declared]
    return result
