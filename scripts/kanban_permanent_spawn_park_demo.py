#!/usr/bin/env python3
"""Manual acceptance harness: a card whose WORKSPACE cannot be resolved parks once.

Runs the REAL dispatcher tick (``kanban_db_dispatch.dispatch_once``) against a
throwaway board. The card's ``workspace_path`` points at a directory that is not a
git repo (and is not inside one), so ``_resolve_worktree_workspace`` fails with
"is not inside a git repo and does not point at a git repo root" — the exact shape
measured on the ops board 2026-09-16..19, where such a card was claimed ->
``spawn_failed`` -> re-queued by the disposition sweep's drain-leaks leg six times
and burned a worker slot per cycle.

Nothing here touches a live board: every kanban path (HERMES_KANBAN_DB included —
a dispatched worker's environment PINS the live board) is repointed at a fresh temp
dir before ``hermes_cli`` is imported, and the board is ``demo``. The spawn function
is a canary: the workspace must fail BEFORE a worker starts, so a clean run is one
that spawned nothing at all.

Usage: scripts/kanban_permanent_spawn_park_demo.py [out.json]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

OUT = sys.argv[1] if len(sys.argv) > 1 else None
_tmp = tempfile.mkdtemp(prefix="kanban-perm-park-demo-")
os.environ["HERMES_HOME"] = _tmp
os.environ["HERMES_KANBAN_HOME"] = _tmp
os.environ["HERMES_KANBAN_BOARD"] = "demo"
os.environ["HERMES_KANBAN_DB"] = os.path.join(_tmp, "demo", "kanban.db")
os.environ["HERMES_KANBAN_WORKSPACES_ROOT"] = os.path.join(_tmp, "demo", "workspaces")
os.environ["HERMES_KANBAN_ATTACHMENTS_ROOT"] = os.path.join(_tmp, "demo", "attachments")
os.environ["HERMES_KANBAN_CRASH_GRACE_SECONDS"] = "0"
for _drop in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE", "HERMES_DELEGATED_CHILD_CONTEXT",
              "HERMES_AGENT", "HERMES_SESSION_SOURCE"):
    os.environ.pop(_drop, None)
for _path in (os.environ["HERMES_KANBAN_WORKSPACES_ROOT"], os.environ["HERMES_KANBAN_ATTACHMENTS_ROOT"],
              os.path.dirname(os.environ["HERMES_KANBAN_DB"])):
    os.makedirs(_path, exist_ok=True)

from hermes_cli import kanban_db as kb            # noqa: E402
from hermes_cli import kanban_db_connect as kbc   # noqa: E402
from hermes_cli import kanban_db_dispatch as kbd  # noqa: E402

kb.init_db()
assert str(kb.kanban_db_path()).startswith(_tmp), "refusing to run against a non-throwaway board"

spawn_calls: list[str] = []


def canary(task_id, *_args, **_kwargs):
    spawn_calls.append(task_id)
    raise AssertionError("the workspace must fail before any spawn")


def _status(conn, tid):
    task = kb.get_task(conn, tid)
    assert task is not None
    return task.status


def card_state(conn, tid):
    row = conn.execute(
        "SELECT status, consecutive_failures, block_kind, last_failure_error, max_retries "
        "FROM tasks WHERE id = ?", (tid,),
    ).fetchone()
    event = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('spawn_failed', 'gave_up') ORDER BY id DESC LIMIT 1", (tid,),
    ).fetchone()
    return {
        "status": row["status"],
        "consecutive_failures": row["consecutive_failures"],
        "block_kind": row["block_kind"],
        "last_failure_error": row["last_failure_error"],
        "newest_failure": event["kind"] if event else None,
        "newest_failure_payload": json.loads(event["payload"]) if event and event["payload"] else {},
        "ledger_attempts": kbd._attempts_without_disposition(conn, tid),
        "runs": conn.execute(
            "SELECT COUNT(*) c FROM task_runs WHERE task_id = ?", (tid,)
        ).fetchone()["c"],
    }


report: dict = {"home": _tmp}
with kbc.connect() as conn:
    unspawnable = Path(_tmp) / "not-a-repo"
    unspawnable.mkdir()
    perm = kb.create_task(
        conn, title="worktree path is not a git repo root", assignee="default",
        workspace_kind="worktree", workspace_path=str(unspawnable),
    )
    occupied = Path(_tmp) / "a-file"
    occupied.write_text("not a directory\n")
    transient = kb.create_task(
        conn, title="dir workspace path is a file", assignee="default",
        workspace_kind="dir", workspace_path=str(occupied),
    )

    kbd.dispatch_once(conn, spawn_fn=canary)

    report["permanent"] = card_state(conn, perm)
    report["unmapped"] = card_state(conn, transient)
    report["spawn_calls"] = spawn_calls
    report["recompute_ready_ticks"] = [
        (kb.recompute_ready(conn), _status(conn, perm)) for _ in range(3)
    ]
    report["drain_leaks_unblock"] = {
        "returned": kb.unblock_task(conn, perm),
        "status_after": _status(conn, perm),
    }
    report["operator_force_unblock"] = {
        "returned": kb.unblock_task(conn, perm, force=True),
        "status_after": _status(conn, perm),
    }
    report["transient_blocked"] = kb.block_task(
        conn, transient, reason="transient probe", kind="transient",
    )
    report["transient_unblock"] = {
        "returned": kb.unblock_task(conn, transient),
        "status_after": _status(conn, transient),
    }

checks = {
    "no_worker_started": spawn_calls == [],
    "permanent_blocked_on_first_attempt": report["permanent"]["status"] == "blocked",
    "permanent_counter_at_ceiling": report["permanent"]["consecutive_failures"]
    >= report["permanent"]["newest_failure_payload"].get("effective_limit", 0) >= 1,
    "permanent_park_documented": report["permanent"]["block_kind"] == "needs_input",
    "permanent_error_names_the_path": str(unspawnable) in (report["permanent"]["last_failure_error"] or ""),
    "permanent_cause_typed": report["permanent"]["newest_failure_payload"].get("permanent_spawn_cause") == "workspace_path",
    "recompute_ready_cannot_revive_it": all(status == "blocked" for _n, status in report["recompute_ready_ticks"]),
    "drain_leaks_unblock_refused": report["drain_leaks_unblock"] == {"returned": False, "status_after": "blocked"},
    "operator_force_unblock_still_works": report["operator_force_unblock"]["returned"] is True,
    "unmapped_error_keeps_its_budget": report["unmapped"]["status"] == "ready"
    and report["unmapped"]["consecutive_failures"] == 1
    and "permanent_spawn_cause" not in report["unmapped"]["newest_failure_payload"],
    "unmapped_error_can_be_requeued": report["transient_blocked"] is True
    and report["transient_unblock"]["returned"] is True,
}
report["checks"] = checks
report["ok"] = all(checks.values())

print(json.dumps(report, indent=2, sort_keys=True))
if OUT:
    Path(OUT).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
sys.exit(0 if report["ok"] else 1)
