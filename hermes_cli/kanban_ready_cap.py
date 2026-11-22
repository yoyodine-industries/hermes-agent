"""Rule 1 of the dispatch-surface contract: READY must carry a PROMISE.

The defect this module closes is not "too many cards". It is that a card in ``ready``
LOOKS schedulable and is not: on the platform board 4,502 cards sat ``ready`` against
roughly 26 starts per 6 h, so one lane's honest wait was ~43 days. An assignment by
assignee therefore promised nothing, and bounded work assigned to a lane was done late,
by an STL, as a named exception.

What restores the promise is a CAP, not an archive. Each lane has a ready DEPTH — the
number of ``ready`` cards assigned to it — and a declared cap (default 50). The cap is a
queue length the dispatcher can actually reach: at 50 deep a lane's ready work is startable
inside the window, so ``ready`` means "this will be started before the cap is refilled".

Three moments, one rule:

* **birth** — :func:`hold_at_cap` is called by ``kanban_db.create_task`` once the card's
  initial state is resolved. A card that would be born ``ready`` on a lane already AT its
  cap is born ``todo`` instead, with a comment naming the cap so the author is never left
  guessing why their card is not dispatchable.
* **promotion** — ``kanban_db.recompute_ready`` (the ONE place a ``todo`` card becomes
  ``ready``) promotes cap-aware and in (priority desc, created_at asc) order, so freeing a
  slot promotes exactly the highest-ranked waiting card.
* **reconciliation** — :func:`reconcile_ready_depth` runs once per dispatcher tick and
  brings an already-over-cap lane back to its cap by DEMOTING the surplus to ``todo``.
  Nothing is lost: the row, its body, its comments, its links and its history are
  untouched; only the status word moves, and the dispatcher lifts it back as slots free.

WHY DEMOTE RATHER THAN ARCHIVE. Archiving 4,876 cards older than 7 days is a bulk state
change to other lanes' work that loses information to buy a number. Converting surplus
``ready`` into ``todo`` loses nothing, restores dispatch, and makes the retention question
optional instead of urgent.

CONFIGURATION IS DATA, NOT CODE. :func:`cap_for` reads, in order: the board's
``board.json`` key ``ready_cap``; the host config's ``kanban.ready_cap``; the constant
:data:`DEFAULT_READY_CAP`. A non-positive value DISABLES the cap for that board — the
pre-existing behaviour, kept reachable so a rollout can be halted without a code change.

THE DEPTH REPORT IS PART OF THE FEATURE. A cap nobody can read is a silent throttle,
which is the same false-green family as a launcher logging ``ok`` over a failed run, so
:func:`depth_report` renders ``ready/cap`` per lane and rides the dispatcher's own output
(text and ``--json``) on every tick.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any
from typing import Iterable
from typing import Optional

_log = logging.getLogger(__name__)

#: Ready depth a lane is allowed to hold when nothing declares otherwise. Chosen so a
#: lane's ready work is startable inside the scheduler's own window rather than being a
#: backlog in disguise; it is a POLICY number, read as data by :func:`cap_for`, never a
#: limit baked into a query.
DEFAULT_READY_CAP = 50

#: Board metadata key carrying the per-board cap (int; non-positive disables).
BOARD_CAP_KEY = "ready_cap"

#: Host config path carrying the default cap when the board declares none.
CONFIG_CAP_PATH = ("kanban", "ready_cap")

#: Set to a truthy value to disable the cap process-wide (rollout hatch; the same
#: behaviour a non-positive ``ready_cap`` gives one board, for a whole host).
DISABLE_ENV = "HERMES_KANBAN_READY_CAP_DISABLED"

#: The statuses a demoted card may be lifted out of. ``todo`` is the kernel's own
#: waiting state for a card with no unmet dependency; ``recompute_ready`` promotes it.
_HELD_STATUS = "todo"

_CAP_HOLD_EVENT = "ready_cap_held"
_CAP_PROMOTED_EVENT = "ready_cap_promoted"
_CAP_DEMOTED_EVENT = "ready_cap_demoted"


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

def _board_meta(board: Optional[str]) -> dict:
    from hermes_cli.kanban_db_boards import read_board_metadata

    try:
        return read_board_metadata(board) or {}
    except Exception:  # a broken board.json must not fail a filing or a tick
        return {}


def _config_cap() -> Optional[int]:
    from hermes_cli.config import load_config_readonly

    try:
        cfg = load_config_readonly() or {}
    except Exception:
        return None
    node: Any = cfg
    for key in CONFIG_CAP_PATH:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    try:
        return int(node)
    except (TypeError, ValueError):
        return None


def _coerce_cap(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cap_for(board: Optional[str] = None) -> int:
    """The ready cap in force for ``board`` — ``0`` means DISABLED.

    Resolution order: the process hatch (:data:`DISABLE_ENV`), then the board's own
    ``ready_cap``, then the host config's ``kanban.ready_cap``, then
    :data:`DEFAULT_READY_CAP`. Read at every call rather than cached: the cap is meant to
    be tunable while the service runs, so raising it must take effect on the next filing
    or the next tick, without a restart.
    """
    if (os.environ.get(DISABLE_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}:
        return 0
    declared = _coerce_cap(_board_meta(board).get(BOARD_CAP_KEY))
    if declared is None:
        declared = _config_cap()
    if declared is None:
        declared = DEFAULT_READY_CAP
    return declared if declared > 0 else 0  # non-positive == disabled


# --------------------------------------------------------------------------------------
# Depth
# --------------------------------------------------------------------------------------

def lane_depths(conn: sqlite3.Connection, *, board: Optional[str] = None) -> dict[str, int]:
    """``{assignee: ready depth}`` for this board, unassigned work excluded.

    READY only, and only rows that hold no claim: a claim moves the row to ``running``,
    so ``claim_lock IS NULL`` is belt-and-braces against a half-written claim and keeps
    the number aligned with what :func:`lane_ranked` counts.
    """
    rows = conn.execute(
        "SELECT COALESCE(assignee, '') AS lane, COUNT(*) AS n FROM tasks "
        "WHERE status = 'ready' AND claim_lock IS NULL AND COALESCE(assignee, '') != '' "
        "GROUP BY lane"
    ).fetchall()
    return {row[0]: int(row[1]) for row in rows}


def depth_report(conn: sqlite3.Connection, *, board: Optional[str] = None) -> dict:
    """The observable half of the cap: ``{lane, ready, cap, over}`` per lane.

    Returned (not printed) so the dispatcher can carry it on ``DispatchResult`` and every
    surface — text, ``--json``, the dashboard, a subscriber hook — renders the same
    numbers. Lanes are included when they hold ready work OR when they are near their cap;
    a lane at 0 ready with a cap of 50 is not news, a lane at 51 is.
    """
    cap = cap_for(board)
    depths = lane_depths(conn, board=board)
    lanes = [
        {
            "lane": lane,
            "ready": depth,
            "cap": cap,
            "over": max(0, depth - cap) if cap else 0,
            "at_cap": bool(cap) and depth >= cap,
        }
        for lane, depth in sorted(depths.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {
        "cap": cap,
        "enabled": cap > 0,
        "total_ready": sum(depths.values()),
        "lanes": lanes,
    }


def format_depth_report(report: dict) -> str:
    """One operator line, e.g. ``ready depth (cap 50): platform-coder 50/50 ...``.

    Empty string when the cap is disabled: a disabled cap has no promise to report and
    printing ``0/0`` per lane would read as a throttle that is not there.
    """
    if not report.get("enabled"):
        return ""
    cap = report.get("cap")
    lanes = report.get("lanes") or []
    if not lanes:
        return f"ready depth (cap {cap}/lane): no assigned ready work"
    parts = []
    for row in lanes:
        label = f"{row['lane']} {row['ready']}/{cap}"
        if row.get("over"):
            label += f" (+{row['over']} over)"
        parts.append(label)
    return f"ready depth (cap {cap}/lane): " + ", ".join(parts)


# --------------------------------------------------------------------------------------
# Rule 1 at the three moments
# --------------------------------------------------------------------------------------

def hold_at_cap(
    conn: sqlite3.Connection, *, assignee: Optional[str], board: Optional[str] = None,
) -> tuple[bool, str]:
    """``(hold, comment)`` for a card about to be BORN ready on ``assignee``'s lane.

    ``hold`` is True when the lane is already at (or over) its cap. The caller writes the
    card as ``todo`` and, when ``comment`` is non-empty, records it on the card — the
    author must be able to read WHY their filing is not dispatchable, or the cap is a
    silent throttle wearing a queue's clothes.
    """
    if not assignee:
        return False, ""
    cap = cap_for(board)
    if cap <= 0:
        return False, ""
    depth = lane_depths(conn, board=board).get(assignee, 0)
    if depth < cap:
        return False, ""
    return True, (
        f"ready cap: lane `{assignee}` is at its cap ({depth}/{cap} ready). The card is "
        f"filed in `todo` and is promoted to `ready` in (priority desc, created_at asc) "
        f"order as slots free — it will be started before the cap refills. Raising the cap "
        f"is `ready_cap` on this board's board.json (or `kanban.ready_cap` in config.yaml)."
    )


def lane_ranked(conn: sqlite3.Connection, *, status: str = "ready",
                lanes: Optional[Iterable[str]] = None) -> dict[str, list[sqlite3.Row]]:
    """Ready/todo rows grouped by lane, each lane in dispatch rank order.

    Dispatch rank is the order Rule 1 promises in: ``priority DESC, created_at ASC`` —
    the same order the spawner reads, so "the highest-priority todo promotes" is true by
    construction rather than by a second, drifting sort.
    """
    sql = (
        "SELECT id, assignee, priority, created_at, status FROM tasks "
        "WHERE status = ? AND claim_lock IS NULL AND COALESCE(assignee, '') != ''"
    )
    params: list[Any] = [status]
    if lanes is not None:
        names = [lane for lane in lanes]
        if not names:
            return {}
        sql += " AND assignee IN (" + ",".join("?" * len(names)) + ")"
        params.extend(names)
    sql += " ORDER BY assignee ASC, priority DESC, created_at ASC, id ASC"
    out: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(sql, params).fetchall():
        out.setdefault(row["assignee"], []).append(row)
    return out


def reconcile_ready_depth(conn: sqlite3.Connection, *, board: Optional[str] = None,
                          now: Optional[int] = None) -> dict:
    """Bring every lane back to its cap: DEMOTE the surplus ``ready`` rows to ``todo``.

    Runs on each dispatcher tick, before promotion, so the tick's spawn decision is taken
    against a queue that honours the promise. Ordering is the same rank Rule 1 states, and
    the rows demoted are the TAIL of it — the lowest priority, then the newest — so the
    work that survives as ``ready`` is the work the dispatcher would have started first.

    Returns ``{"cap", "demoted": [ids], "lanes": {lane: (ready, cap)}}``. Idempotent: a
    board already inside its caps costs one SELECT and writes nothing. The caller owns the
    transaction.
    """
    cap = cap_for(board)
    report: dict = {"cap": cap, "demoted": [], "lanes": {}}
    if cap <= 0:
        return report
    import time as _time
    from hermes_cli import kanban_db as _kb

    stamp = int(now if now is not None else _time.time())
    for lane, rows in lane_ranked(conn, status="ready").items():
        report["lanes"][lane] = (len(rows), cap)
        surplus = len(rows) - cap
        if surplus <= 0:
            continue
        for row in rows[cap:]:
            cur = conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ? AND status = 'ready' "
                "AND claim_lock IS NULL",
                (_HELD_STATUS, row["id"]),
            )
            if cur.rowcount != 1:
                continue  # lost a race with a claim; the lane depth is measured again next tick
            report["demoted"].append(row["id"])
            _kb._append_event(
                conn, row["id"], _CAP_DEMOTED_EVENT,
                {"from": "ready", "to": _HELD_STATUS, "lane": lane,
                 "ready": len(rows), "cap": cap, "at": stamp},
            )
    if report["demoted"]:
        _log.info(
            "kanban: ready cap %s — demoted %d surplus ready card(s) to todo",
            cap, len(report["demoted"]),
        )
    return report
