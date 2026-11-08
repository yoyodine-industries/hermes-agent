#!/usr/bin/env python3
"""Manual acceptance harness: a card cannot outlive its attempt ceiling.

Runs the REAL dispatcher tick (``kanban_db_dispatch.dispatch_once``) against a
throwaway board whose worker is a REAL child process that exits rc=0 WITHOUT a
terminal kanban call — the exact shape measured on the live fleet (189 of 241
crashed runs). The card starts with ``max_retries=2``: two attempts is its
ceiling.

Nothing here touches a live board: HERMES_HOME / HERMES_KANBAN_HOME are pinned
to a fresh temp dir before ``hermes_cli`` is imported, and the board is
``demo``. The evidence is what the dispatcher itself wrote.

Usage: scripts/kanban_attempt_ceiling_demo.py <label> [out.json]
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time

LABEL = sys.argv[1] if len(sys.argv) > 1 else "run"
OUT = sys.argv[2] if len(sys.argv) > 2 else None
TICKS = 7

_temp = tempfile.mkdtemp(prefix="kanban-ceiling-demo-")
# This worker's own environment pins the LIVE board (the dispatcher injects
# HERMES_KANBAN_DB / _WORKSPACES_ROOT into every worker). Pin every kanban path
# to the throwaway dir before importing hermes_cli, so a synthetic card cannot
# read or write a live board.
os.environ["HERMES_HOME"] = _temp
os.environ["HERMES_KANBAN_HOME"] = _temp
os.environ["HERMES_KANBAN_BOARD"] = "demo"
os.environ["HERMES_KANBAN_DB"] = os.path.join(_temp, "demo", "kanban.db")
os.environ["HERMES_KANBAN_WORKSPACES_ROOT"] = os.path.join(_temp, "demo", "workspaces")
os.environ["HERMES_KANBAN_ATTACHMENTS_ROOT"] = os.path.join(_temp, "demo", "attachments")
os.environ["HERMES_KANBAN_CRASH_GRACE_SECONDS"] = "0"
os.environ.pop("HERMES_KANBAN_TASK", None)
os.environ.pop("HERMES_KANBAN_WORKSPACE", None)
# This session is a delegated child: that flag makes kanban connects read-only
# ("ask its owner to initialize it"), which would make the demo's own board
# un-creatable. Drop it — the demo is the board's owner.
os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
os.environ.pop("HERMES_AGENT", None)
os.environ.pop("HERMES_SESSION_SOURCE", None)
os.makedirs(os.environ["HERMES_KANBAN_WORKSPACES_ROOT"], exist_ok=True)
os.makedirs(os.environ["HERMES_KANBAN_ATTACHMENTS_ROOT"], exist_ok=True)
os.makedirs(os.path.dirname(os.environ["HERMES_KANBAN_DB"]), exist_ok=True)

from hermes_cli import kanban_db as kb          # noqa: E402
from hermes_cli import kanban_db_connect as kbc  # noqa: E402
from hermes_cli import kanban_db_dispatch as kbd  # noqa: E402

BOARD = "demo"
CEILING = 2
spawned_pids: list[int] = []


def spawn(task, workspace, board=None):
    """A worker that exits rc=0 without ever calling kanban_complete/block."""
    proc = subprocess.Popen(["/usr/bin/true"])
    spawned_pids.append(proc.pid)
    return proc.pid


_ledger_count = getattr(kbd, "_attempts_without_disposition", None)


def card_state(conn):
    row = conn.execute(
        "SELECT status, consecutive_failures, block_kind, block_recurrences, "
        "       max_retries, last_failure_error FROM tasks WHERE id = ?",
        (TASK,),
    ).fetchone()
    return {
        "status": row["status"],
        "consecutive_failures": row["consecutive_failures"],
        "block_kind": row["block_kind"],
        "block_recurrences": row["block_recurrences"],
        "max_retries": row["max_retries"],
        # Absent on the base tree, where the counter does not exist yet.
        "ledger_attempts": _ledger_count(conn, TASK) if _ledger_count else None,
        "runs": conn.execute(
            "SELECT COUNT(*) c FROM task_runs WHERE task_id = ?", (TASK,)
        ).fetchone()["c"],
    }


print(f"=== synthetic card, real dispatcher ticks ({LABEL}) ===")
print(f"throwaway home: {_temp}")
print(f"board: {BOARD}   ceiling (max_retries): {CEILING}")

kb.init_db()
conn = kbc.connect(board=BOARD)
print(f"db: {kb.kanban_db_path(board=BOARD)}")
assert _temp in str(kb.kanban_db_path(board=BOARD)), "refusing to run against a live board"

TASK = kb.create_task(conn, title="rc0 without a terminal call", assignee="default")
conn.execute(
    "UPDATE tasks SET max_retries = ?, max_runtime_seconds = ? WHERE id = ?",
    (CEILING, 900, TASK),
)
conn.commit()
print(f"card: {TASK}")

timeline = []
for tick in range(1, TICKS + 1):
    before = card_state(conn)
    result = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, board=BOARD)
    after = card_state(conn)
    fields = {k: v for k, v in dataclasses.asdict(result).items() if v and k != "skipped_locked"}
    timeline.append({"tick": tick, "before": before, "after": after, "result": fields})
    print(
        f"\ntick {tick}: before={before['status']}/cf={before['consecutive_failures']}"
        f"/runs={before['runs']}  ->  after={after['status']}/cf={after['consecutive_failures']}"
        f"/runs={after['runs']}/kind={after['block_kind']}"
    )
    print(f"   dispatch result: {fields or '(no writes)'}")
    time.sleep(0.4)

final = card_state(conn)
print(f"\n=== final card state ({LABEL}) ===")
for key, value in final.items():
    print(f"   {key}: {value}")

print(f"\n=== run ledger ({LABEL}) ===")
runs = []
for row in conn.execute(
    "SELECT id, outcome, substr(coalesce(error,''),1,70) error, metadata, started_at, ended_at "
    "FROM task_runs WHERE task_id = ? ORDER BY id",
    (TASK,),
).fetchall():
    meta = json.loads(row["metadata"] or "{}")
    entry = {
        "run": row["id"],
        "outcome": row["outcome"],
        "duration_s": (row["ended_at"] or 0) - (row["started_at"] or 0),
        "metadata": meta,
    }
    runs.append(entry)
    print(f"   run {row['id']}: {row['outcome']}  {row['error']}")
    print(f"      metadata: {json.dumps(meta, sort_keys=True)}")

print(f"\n=== breaker events ({LABEL}) ===")
events = []
for row in conn.execute(
    "SELECT id, kind, payload, run_id FROM task_events WHERE task_id = ? ORDER BY id",
    (TASK,),
).fetchall():
    payload = json.loads(row["payload"] or "{}")
    events.append({"event": row["id"], "kind": row["kind"], "payload": payload})
    print(f"   event {row['id']}: {row['kind']}  {json.dumps(payload, sort_keys=True)}")

summary = {
    "label": LABEL,
    "board": BOARD,
    "throwaway_home": _temp,
    "ceiling": CEILING,
    "attempts_spawned": len(spawned_pids),
    "runs": len(runs),
    "escalated": final["status"] == "blocked",
    "final": final,
    "timeline": timeline,
    "run_ledger": runs,
    "events": events,
}
print(f"\n=== summary ({LABEL}) ===")
print(json.dumps({k: summary[k] for k in
                  ("label", "attempts_spawned", "runs", "escalated", "final")}, sort_keys=True))
if OUT:
    with open(OUT, "w") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
    print(f"evidence: {OUT}")
