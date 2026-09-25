"""How a kanban failure notice reads to an operator.

A notification travels on a per-subscription cursor, so it can be delivered long
after the event it describes: a closed desktop session, a gateway restart, a chat
that was offline. A notice that reports the failed run in the present tense — "the
dispatcher will retry", "it gave up" — is then read as the card's state *now*, and
a board that has already recovered reads as down. A late notice must therefore
carry two facts: which run failed and when it ended, and the card's status at
delivery.

``failure_notice_text`` is the single renderer for the failure kinds (``crashed``
/ ``gave_up`` / ``timed_out``) so the gateway chat relay and the desktop poller
cannot drift apart on the wording. The remaining kinds are still rendered per
surface.
"""

from __future__ import annotations

import time
from typing import Any, Optional

#: Kinds that report a run that did NOT finish successfully.
FAILURE_KINDS = ("crashed", "gave_up", "timed_out")


def _clock(ts: Any) -> str:
    """Local wall-clock ``HH:MM:SS`` for a unix timestamp (``""`` when unusable)."""
    try:
        stamp = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    try:
        return time.strftime("%H:%M:%S", time.localtime(stamp))
    except (OSError, ValueError, OverflowError):
        return ""


def _line(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text.splitlines()[0][:limit] if text else ""


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _run_clause(run_id: Optional[int], event_ts: Any) -> str:
    """``"; run 3997 ended 13:16:57"`` — the failing run, and when it stopped.

    ``event_ts`` is the moment the board recorded the stop; for these kinds the
    run's ``ended_at`` and the event row are written in one transaction, so the
    event timestamp is the run's end time. Empty when the event names no run: the
    caller then states the time alone rather than inventing a run.
    """
    if not run_id:
        return ""
    when = _clock(event_ts)
    return f"; run {int(run_id)} ended {when}" if when else f"; run {int(run_id)}"


def _card_clause(task: Any, task_id: str, latest_run: Any) -> str:
    """``"; card: running since 13:17:57 (run 3998)"`` — the state the reader is in now.

    Read from the task row as it stood at delivery, so a notice whose run has
    already been superseded says so instead of implying the failure is current.
    """
    status = str(getattr(task, "status", "") or "").strip().lower()
    if not status:
        # No task row (deleted card): claim nothing rather than guess.
        return ""
    if status == "running":
        run_id = getattr(task, "current_run_id", None)
        started = ""
        if run_id and latest_run is not None and getattr(latest_run, "id", None) == run_id:
            started = _clock(getattr(latest_run, "started_at", None))
        if run_id and started:
            return f"; card: running since {started} (run {int(run_id)})"
        if run_id:
            return f"; card: running (run {int(run_id)})"
        return "; card: running"
    if status == "ready":
        return "; card: ready (will be retried)"
    if status == "blocked":
        return (
            "; card: blocked (fix the cause, then "
            f"`hermes kanban unblock {task_id}`; logs: `hermes kanban log {task_id}`)"
        )
    return f"; card: {status}"


def _failure_lead(kind: str, payload: dict, stamp: str) -> str:
    """What failed, in the past tense — never a claim about the card's live state."""
    at = f" at {stamp}" if stamp else ""
    if kind == "crashed":
        return f"its worker crashed (pid gone){at}"
    if kind == "gave_up":
        failures = _int_or_zero(payload.get("failures"))
        count = f"{failures} failed runs" if failures else "repeated failures"
        last = _line(payload.get("error"), 160)
        return f"it gave up after {count}" + (f" (last: {last})" if last else "") + at
    limit = _int_or_zero(payload.get("limit_seconds"))
    minutes = max(1, round(limit / 60)) if limit else 0
    span = f"the {minutes}-minute limit" if minutes else "its time limit"
    return f"its worker timed out past {span}{at}"


def failure_notice_text(
    kind: str,
    payload: Optional[dict],
    *,
    task: Any,
    task_id: str,
    event_ts: Any = None,
    event_run_id: Optional[int] = None,
    latest_run: Any = None,
) -> str:
    """Body of a failure notice: the run that failed and when it ended, then the card's state now.

    ``task`` and ``latest_run`` are the rows read at DELIVERY time (``None`` when
    the board no longer has them) — an event claimed after a restart is rendered
    against the board as it is, not as the event left it.
    """
    if kind not in FAILURE_KINDS:
        raise ValueError(f"not a failure kind: {kind!r}")
    payload = payload or {}
    run_id = event_run_id or None
    if run_id is None and latest_run is not None:
        latest_id = getattr(latest_run, "id", None)
        if latest_id and latest_id != getattr(task, "current_run_id", None):
            # ``gave_up`` names no run: point at the newest one the card has left.
            run_id = latest_id
    run_clause = _run_clause(run_id, event_ts)
    stamp = "" if run_clause else _clock(event_ts)
    return (
        _failure_lead(kind, payload, stamp)
        + run_clause
        + _card_clause(task, task_id, latest_run)
    )
