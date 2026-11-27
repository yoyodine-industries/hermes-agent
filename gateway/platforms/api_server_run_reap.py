"""Bounded reap of stalled ``/v1/runs`` tasks that hold the agent-work gate.

t_2fd3bc0b28. The gate counts one slot per non-terminal entry in
``_active_run_tasks``. A task whose turn never returns is therefore *held by
definition*: the orphan fence in ``_sweep_orphaned_runs_once`` skips it precisely
because a live handle exists, and nothing else has a bound on how long that handle
may sit. Measured on the live gateway: 16 of 16 slots held for ~12 hours each while
the yoyoflow ledger carried five open runs, and every peer message answered
``Too many concurrent runs (max 16)``.

The repair is a bounded reap, never a higher ceiling -- raising the limit converts a
visible block into a slower one. A run that has made no progress past a declared
bound is cancelled, its row is closed with a named code, and the slot count is
logged before and after so the reap is evidence rather than a claim.

What is deliberately NOT reaped:

* a run past the bound whose row says ``waiting_for_approval`` -- a human gate can
  legitimately sit idle for hours, and killing it would lose the approval;
* a run inside the bound, however quiet -- quiet is not stalled;
* a durable row that has no task in this process -- that is orphaned work, and the
  existing fence (``_fence_unheld_run_status``) already closes it.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "API_SERVER_RUN_STALL_REAP_SECONDS",
    "REAP_ERROR_CODE",
    "REAP_SKIP_STATUSES",
    "reap_stalled_runs",
    "stalled_run_holders",
    "stall_reap_bound",
]

# A run that has shown no progress for this long is stalled. Declared, not inferred:
# 3600s is an order of magnitude above the longest ordinary turn and two orders below
# the observed leak (measured holders sat at 41,978s and 42,000s).
API_SERVER_RUN_STALL_REAP_SECONDS = 3600.0

# A run parked on a human decision is idle by design; the bound does not apply.
REAP_SKIP_STATUSES = frozenset({"waiting_for_approval"})

# The row's error code, so a reader can tell a reap from an agent failure.
REAP_ERROR_CODE = "run_stalled_reaped"


def stall_reap_bound(adapter: Any = None) -> float:
    """The declared stall bound, in seconds (env-overridable for a bounded trial)."""
    raw = os.environ.get("API_SERVER_RUN_STALL_REAP_SECONDS")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            logger.warning(
                "[api_server] API_SERVER_RUN_STALL_REAP_SECONDS=%r is not a positive "
                "number; using the declared %ss bound", raw, API_SERVER_RUN_STALL_REAP_SECONDS)
    return API_SERVER_RUN_STALL_REAP_SECONDS


def _last_progress(status: Dict[str, Any]) -> float:
    """The newest moment this run is known to have moved.

    ``updated_at`` is written by every status transition, including the streaming
    events a live run emits, so a run that is still working never looks stalled.
    """
    created = float(status.get("created_at") or 0.0)
    updated = float(status.get("updated_at") or 0.0)
    return max(created, updated)


def stalled_run_holders(adapter: Any, *, now: Optional[float] = None,
                        bound: Optional[float] = None) -> List[Dict[str, Any]]:
    """Read-only census of the run slots that are stalled past the bound.

    Side-effect free: this is what a report prints, and what a saturated gate can be
    explained with -- term, run id, session, status, and how long it has been held.
    """
    if now is None:
        now = time.time()
    if bound is None:
        bound = stall_reap_bound(adapter)
    statuses = getattr(adapter, "_run_statuses", None) or {}
    agents = getattr(adapter, "_active_run_agents", None) or {}
    holders: List[Dict[str, Any]] = []
    for run_id, task in list((getattr(adapter, "_active_run_tasks", None) or {}).items()):
        try:
            if task.done():
                continue
        except Exception:  # noqa: BLE001 - an unreadable task is not evidence of life
            pass
        status = statuses.get(run_id) or {}
        state = str(status.get("status") or "")
        if state in REAP_SKIP_STATUSES:
            continue
        idle = now - _last_progress(status)
        if idle <= bound:
            continue
        holders.append({
            "run_id": run_id,
            "session_id": status.get("session_id"),
            "status": state or "unknown",
            "idle_seconds": round(idle, 3),
            "has_agent": run_id in agents,
        })
    return holders


def _snapshot_count(adapter: Any) -> Optional[int]:
    """The gate's published count, or None when it cannot be read (never fatal)."""
    snapshot = getattr(adapter, "_agent_gate_snapshot", None)
    if not callable(snapshot):
        return None
    try:
        snap = snapshot()
    except Exception:  # noqa: BLE001 - the census must never block the reap
        return None
    if not isinstance(snap, dict):
        return None
    try:
        return int(snap.get("count") or 0)
    except (TypeError, ValueError):
        return None


def reap_stalled_runs(adapter: Any, *, now: Optional[float] = None,
                      bound: Optional[float] = None) -> Dict[str, Any]:
    """Cancel and close every stalled run slot; log the count before and after.

    Returns ``{"before", "after", "reaped", "bound"}``. Idempotent: a second pass over
    the same clock reaps nothing, because the first pass removed the task and closed
    the row.
    """
    if now is None:
        now = time.time()
    if bound is None:
        bound = stall_reap_bound(adapter)
    before = _snapshot_count(adapter)
    holders = stalled_run_holders(adapter, now=now, bound=bound)
    reaped: List[Dict[str, Any]] = []
    retire = getattr(adapter, "_retire_live_run", None)
    set_status = getattr(adapter, "_set_run_status", None)
    for holder in holders:
        run_id = holder["run_id"]
        task = (getattr(adapter, "_active_run_tasks", None) or {}).get(run_id)
        try:
            if task is not None and not task.done():
                task.cancel()
        except Exception:  # noqa: BLE001 - a cancel that raises must still close the row
            logger.debug("[api_server] cancel raised for stalled run %s", run_id, exc_info=True)
        if callable(set_status):
            try:
                set_status(
                    run_id,
                    "failed",
                    error=(
                        "The run made no progress for "
                        f"{int(holder['idle_seconds'])}s (bound {int(bound)}s); the slot was "
                        "reaped so the agent-work gate could admit other work."
                    ),
                    error_code=REAP_ERROR_CODE,
                    reaped=True,
                    last_event="run.failed",
                )
            except Exception:  # noqa: BLE001
                logger.exception("[api_server] failed to close reaped run %s", run_id)
        if callable(retire):
            try:
                retire(run_id)
            except Exception:  # noqa: BLE001
                logger.exception("[api_server] failed to retire reaped run %s", run_id)
        reaped.append(holder)
    after = _snapshot_count(adapter)
    if reaped:
        logger.warning(
            "[api_server] reaped %d stalled run slot(s), bound=%ss: count %s -> %s; holders=%s",
            len(reaped), int(bound), before, after,
            [(h["run_id"], h["status"], h["idle_seconds"]) for h in reaped],
        )
    return {"before": before, "after": after, "reaped": reaped, "bound": bound}
