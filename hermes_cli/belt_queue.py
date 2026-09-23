"""The belt queue: the realtime half of the maintenance framework.

A card BLOCK is the trigger. A first-party lifecycle observer
(``hermes_cli.observability``) calls :func:`enqueue_block`, which appends ONE row
to a dedicated SQLite store and returns. Enqueue-only by contract: no board
write, no model call, no routing, no window gate — routing is cheap I/O and runs
around the clock.

Two stores, two writers. The queue lives in ``<hermes home>/kanban/belt.db``,
NOT in the board DB, so an enqueue can never take a lock a board writer holds:
the hook fires post-commit and a board write in flight (another process, or a
caller inside its own txn) must not be able to deadlock it. The board is read
read-only and only to snapshot state; the board is never written here.

Coalescing is keyed on STATE, not on the fact of a block: a unique index on
``(task_id, state_fingerprint)`` plus ``INSERT OR IGNORE`` is what makes a card
that blocks six times a day with unchanged state produce ONE row. The
fingerprint recipe (:func:`state_fingerprint`) is a shared contract — the belt
DAG's gate node has to recompute the identical value, so the field order, the
None encoding and the separator are part of the interface, not an internal
detail.

The board -> domain map is a static registry, not hook logic (see the design's
section 1.3): a JSON object at ``<hermes home>/kanban/belt_domains.json`` with
the design's first cut as the built-in default. The registry's ROWS are the
catalogue owner's; this module only implements the lookup, and the same lookup
runs on the dispatch side, so both sides resolve a board to the same domain.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Columns of ``belt_queue``, in the design's order (section 1.2).
BELT_QUEUE_COLUMNS = (
    "task_id", "board", "assignee", "project_id", "domain", "block_kind",
    "reason", "last_failure_error", "source_status", "state_fingerprint",
    "enqueued_at", "status", "run_id", "attempts",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS belt_queue (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            TEXT NOT NULL,
    board              TEXT NOT NULL,
    assignee           TEXT,
    project_id         TEXT,
    domain             TEXT NOT NULL,
    block_kind         TEXT NOT NULL,
    reason             TEXT,
    last_failure_error TEXT,
    source_status      TEXT,
    state_fingerprint  TEXT NOT NULL,
    enqueued_at        TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'queued',
    run_id             TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0
)
"""

#: The coalescing key. A card in an unchanged state must not queue twice.
_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS belt_queue_state_uidx "
    "ON belt_queue(task_id, state_fingerprint)"
)

#: Design section 1.3 first cut; a registry file overrides or extends it and the
#: catalogue owner owns those rows. An unknown board defaults to ``platform``.
DEFAULT_BOARD_DOMAINS = {
    "ops": "platform",
    "research": "research",
    "financially": "financially",
}
DEFAULT_DOMAIN = "platform"

#: Domains name a DAG (``maintenance-belt-<domain>.yaml``), so a value that is
#: not a path-safe slug is refused rather than resolved into a bogus file.
_DOMAIN_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")

#: Unit separator: cannot occur in any field's own text, so the join is
#: unambiguous without escaping.
_SEP = "\x1f"


def belt_db_path() -> Path:
    """The belt queue store: ``<hermes home>/kanban/belt.db``."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "kanban" / "belt.db"


def domain_registry_path() -> Path:
    """The board -> domain registry the enqueue and the dispatcher share."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "kanban" / "belt_domains.json"


def state_fingerprint(
    *,
    task_id: str,
    status: Any,
    block_kind: Any,
    block_recurrences: Any,
    last_failure_error: Any,
) -> str:
    """Hash of the state a disposition would act on.

    Recipe (shared with the belt DAG's gate node): sha256 over the UTF-8 encoding
    of ``task_id, status, block_kind, block_recurrences, last_failure_error``
    joined by U+001F, each rendered with :func:`_field`. ``None`` and ``0`` are
    distinct; a changed error string is a changed state, which is what lets a
    re-blocked card with a NEW cause disposition again.
    """
    body = _SEP.join(
        _field(value)
        for value in (task_id, status, block_kind, block_recurrences, last_failure_error)
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _field(value: Any) -> str:
    """One fingerprint field: ``None`` -> empty string, ints canonicalised."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return str(value).strip() if isinstance(value, str) else str(value)


def domain_for_board(board: Any) -> str:
    """Resolve a board to its maintenance domain (design section 1.3)."""
    name = (board or "").strip().lower()
    domain = _registry().get(name) or DEFAULT_BOARD_DOMAINS.get(name) or DEFAULT_DOMAIN
    if not _DOMAIN_RE.match(str(domain)):
        logger.warning("belt: refusing non-slug domain %r for board %r", domain, name)
        return DEFAULT_DOMAIN
    return str(domain)


def _registry() -> Dict[str, str]:
    """The registry file's rows, or ``{}`` when it is absent or unreadable."""
    path = domain_registry_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("belt: unreadable domain registry %s", path, exc_info=True)
        return {}
    if not isinstance(raw, dict):
        logger.warning("belt: domain registry %s is not a JSON object", path)
        return {}
    return {str(k).strip().lower(): v for k, v in raw.items()}


def enqueue_block(
    *,
    task_id: str,
    board: Any = None,
    assignee: Any = None,
    run_id: Any = None,
    reason: Any = None,
    block_kind: Any = None,
    source_status: Any = None,
) -> Optional[int]:
    """Append this block to the belt queue; return the row id, or None.

    Re-reads the live Task row read-only (the hook payload is a hint, the row is
    the state) and never raises: the caller is a lifecycle observer, and a
    transition must not fail because the queue write did.
    """
    try:
        state = _live_task_state(board, task_id)
        if state is None:
            logger.warning("belt: no task row for %s on board %r", task_id, board)
            return None
        resolved_kind = state["block_kind"] if state["block_kind"] is not None else block_kind
        fingerprint = state_fingerprint(
            task_id=task_id,
            status=state["status"],
            block_kind=resolved_kind,
            block_recurrences=state["block_recurrences"],
            last_failure_error=state["last_failure_error"],
        )
        row = {
            "task_id": task_id,
            "board": str(board or "default"),
            "assignee": state["assignee"] if state["assignee"] is not None else assignee,
            "project_id": state["project_id"],
            "domain": domain_for_board(board),
            "block_kind": "" if resolved_kind is None else str(resolved_kind),
            "reason": reason,
            "last_failure_error": state["last_failure_error"],
            "source_status": source_status,
            "state_fingerprint": fingerprint,
            "enqueued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        return _insert(row)
    except Exception:
        logger.warning("belt: enqueue failed for %s", task_id, exc_info=True)
        return None


def _live_task_state(board: Any, task_id: str) -> Optional[Dict[str, Any]]:
    """The durable Task columns the queue row is derived from (read-only)."""
    from hermes_cli.kanban_db import kanban_db_path

    path = kanban_db_path(board=str(board) if board else None)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, block_kind, block_recurrences, last_failure_error,"
            " project_id, assignee FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "status": row["status"],
        "block_kind": row["block_kind"],
        "block_recurrences": int(row["block_recurrences"] or 0),
        "last_failure_error": row["last_failure_error"],
        "project_id": row["project_id"],
        "assignee": row["assignee"],
    }


def _insert(row: Dict[str, Any]) -> Optional[int]:
    """INSERT OR IGNORE one queue row; None when the state is already queued."""
    path = belt_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **row,
        # Written explicitly rather than left to the DDL defaults: the insert
        # names every column, and a NULL here would be a silent NOT NULL drop
        # under OR IGNORE.
        "status": row.get("status") or "queued",
        "run_id": row.get("run_id"),
        "attempts": int(row.get("attempts") or 0),
    }
    conn = sqlite3.connect(str(path), timeout=2.0)
    try:
        conn.execute("PRAGMA busy_timeout = 2000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            # WAL is unavailable on some network filesystems; the queue still
            # works in the default journal mode.
            logger.debug("belt: WAL unavailable on %s", path, exc_info=True)
        conn.execute(_SCHEMA_SQL)
        conn.execute(_INDEX_SQL)
        columns = ", ".join(BELT_QUEUE_COLUMNS)
        placeholders = ", ".join("?" for _ in BELT_QUEUE_COLUMNS)
        cur = conn.execute(
            f"INSERT OR IGNORE INTO belt_queue ({columns}) VALUES ({placeholders})",
            tuple(payload.get(column) for column in BELT_QUEUE_COLUMNS),
        )
        if not cur.rowcount:
            # OR IGNORE also swallows a constraint violation. An ignored insert
            # with no matching row is a dropped enqueue, not a coalesce.
            queued = conn.execute(
                "SELECT 1 FROM belt_queue WHERE task_id = ? AND state_fingerprint = ? LIMIT 1",
                (payload["task_id"], payload["state_fingerprint"]),
            ).fetchone()
            if queued is None:
                logger.error(
                    "belt: enqueue for %s was ignored with no matching queued row",
                    payload["task_id"],
                )
                return None
        conn.commit()
    finally:
        conn.close()
    return cur.lastrowid if cur.rowcount else None
