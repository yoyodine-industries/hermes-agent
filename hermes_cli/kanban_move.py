"""Move a single card (task) between boards on the same host.

``boards export``/``import`` copy a whole board; this is the per-card verb that
``export`` can't express. The card travels with its comments, events, runs and
file attachments; parent/child links, gateway subscriptions and machine-local
runtime state (claims, worker PIDs, workspace paths) are deliberately NOT
carried — they belong to the source board's graph and this machine.

Two-phase, target-first: the copy is committed on the destination board before
the source row is removed, so a failed write can never strand the card. The
source board's write lock is held across the whole move, which both orders the
phases and closes the claim race (a dispatcher cannot claim the card mid-move).

Refuses while the card is running; backs up both stores first; writes a
``moved_in`` audit event on the destination and a ``moved_out`` audit event on
the source (keyed to the now-deleted task id — the schema has no FK, so the
tombstone event is the durable "this id left for board X as id Y" trail).
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_transfer import _DISPATCHABLE_STATUSES, _snapshot_db

# Columns that record machine-local runtime state and must not follow the card.
_SCRUB_NULL = (
    "claim_lock", "claim_expires", "worker_pid", "current_run_id",
    "last_heartbeat_at", "session_id", "workspace_path", "branch_name",
    "project_id", "last_failure_error",
)


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
    if task_row["status"] == "running" or task_row["current_run_id"] is not None:
        return True
    open_run = conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id = ? AND status = 'running' "
        "AND ended_at IS NULL LIMIT 1",
        (task_id,),
    ).fetchone()
    return open_run is not None


def _copy_to_target(
    src_conn, dst_conn, task_row, *, src_slug: str, dst_slug: str, new_id: str,
) -> tuple[dict[str, int], list[str], bool]:
    """Copy the card + relations into ``dst_conn`` (caller's txn). Returns
    ``(counts, warnings, parked_triage)``."""
    task_id = task_row["id"]
    counts: dict[str, int] = {"comments": 0, "events": 0, "runs": 0, "attachments": 0}
    warnings: list[str] = []
    parked_triage = False

    copied = _row_to_dict(task_row)
    copied["id"] = new_id
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

    # Runs first so their old->new id map can re-anchor event run_id pointers.
    run_map: dict[int, int] = {}
    for r in src_conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,)
    ):
        old_run_id = r["id"]
        d = _row_to_dict(r)
        d["task_id"] = new_id
        _insert_mapped(dst_conn, "task_runs", d, skip=("id",))
        new_run_id = int(dst_conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        run_map[old_run_id] = new_run_id
        counts["runs"] += 1

    for r in src_conn.execute(
        "SELECT * FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,)
    ):
        d = _row_to_dict(r)
        d["task_id"] = new_id
        _insert_mapped(dst_conn, "task_comments", d, skip=("id",))
        counts["comments"] += 1

    for r in src_conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
    ):
        d = _row_to_dict(r)
        d["task_id"] = new_id
        rid = d["run_id"]
        d["run_id"] = run_map.get(rid) if rid is not None else None
        _insert_mapped(dst_conn, "task_events", d, skip=("id",))
        counts["events"] += 1

    target_dir = kb.task_attachments_dir(new_id, board=dst_slug)
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
        d["task_id"] = new_id
        d["stored_path"] = str(dest)
        _insert_mapped(dst_conn, "task_attachments", d, skip=("id",))
        counts["attachments"] += 1

    kb._append_event(
        dst_conn, new_id, "moved_in",
        {"from_board": src_slug, "from_task_id": task_id},
    )
    return counts, warnings, parked_triage


def _remove_from_source(conn, task_id: str, *, dst_slug: str, new_id: str) -> int:
    """Delete the card + all relations from the source board (caller's txn),
    then record a tombstone ``moved_out`` event keyed to the now-gone id.
    Returns the number of parent/child links severed."""
    severed = int(
        conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE parent_id = ? OR child_id = ?",
            (task_id, task_id),
        ).fetchone()[0]
    )
    kb._delete_task_relations(conn, task_id)
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    kb._append_event(
        conn, task_id, "moved_out",
        {"to_board": dst_slug, "to_task_id": new_id},
    )
    return severed


def move_task(task_id: str, target_slug: str, *, source_slug: Optional[str] = None) -> dict[str, Any]:
    """Move ``task_id`` to ``target_slug``; returns a summary dict. Raises
    ``ValueError`` on a bad slug, a missing task, a same-board move, or a
    running card; ``FileNotFoundError``/``OSError`` on a missing store."""
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
            if _is_running(src_conn, task_id, task_row):
                raise ValueError(
                    f"task {task_id!r} is running on board {src_slug!r}; "
                    f"reclaim or archive it first, then move"
                )

            new_id = task_id
            if dst_conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
                new_id = kb._new_task_id()

            with kbc.write_txn(dst_conn):
                counts, warnings, parked_triage = _copy_to_target(
                    src_conn, dst_conn, task_row,
                    src_slug=src_slug, dst_slug=dst_slug, new_id=new_id,
                )
            severed = _remove_from_source(src_conn, task_id, dst_slug=dst_slug, new_id=new_id)

        # Re-gate children whose parent just left the board (outside the txn —
        # recompute_ready opens its own).
        kb.recompute_ready(src_conn)

    return {
        "from_board": src_slug,
        "to_board": dst_slug,
        "from_task_id": task_id,
        "to_task_id": new_id,
        "counts": counts,
        "severed_links": severed,
        "parked_triage": parked_triage,
        "warnings": warnings,
        "backups": {
            "source": str(backups["source"]) if backups["source"] else None,
            "target": str(backups["target"]) if backups["target"] else None,
        },
    }
