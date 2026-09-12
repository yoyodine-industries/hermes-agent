"""Turn-end guard for kanban workers, which must end with ``kanban_complete`` or
``kanban_block``. Some models narrate the next step and stop with no tool calls;
Hermes treats that as a clean exit → ``rc=0`` → dispatcher ``protocol_violation``.
Policy-only: return a bounded synthetic nudge so the loop continues instead of exiting.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterable, Optional


# A lane handoff (`kanban_request_review` / `kanban_request_changes`) closes the
# dispatcher run for this session, so it is a terminal state, not a violation.
_TERMINAL_KANBAN_TOOLS = frozenset(
    {"kanban_complete", "kanban_block", "kanban_request_review", "kanban_request_changes"}
)

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """On when ``HERMES_KANBAN_TASK`` is set, unless ``HERMES_KANBAN_STOP_NUDGE`` disables it.

    The env var alone is not identity: a cron job fired in-process inside a worker,
    or a delegate child, inherits ``HERMES_KANBAN_*`` without owning the card, so
    the guard also requires ``is_dispatcher_owned_worker_context()`` — the single
    predicate every ``HERMES_KANBAN_*`` identity gate uses.
    """
    if (os.environ.get("HERMES_KANBAN_STOP_NUDGE") or "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    if not (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return False
    try:
        from agent import delegation_context  # module, so patching/test ContextVars both work

        return bool(delegation_context.is_dispatcher_owned_worker_context())
    except Exception:
        # Fail-safe: a broken import or ContextVar read must never disarm the guard.
        return True


def _tool_call_name(tc: Any) -> str:
    """Tool name from a dict or object tool call (``function.name`` first, then ``name``)."""
    if isinstance(tc, dict):
        fn = tc.get("function")
        return str((fn.get("name") if isinstance(fn, dict) else tc.get("name")) or "")
    fn = getattr(tc, "function", None)
    return str((getattr(fn, "name", "") if fn is not None else getattr(tc, "name", "")) or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    for msg in filter(lambda m: isinstance(m, dict), messages or ()):
        role = msg.get("role")
        if role == "assistant" and any(
            _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS for tc in msg.get("tool_calls") or []
        ):
            return True
        if role == "tool" and str(msg.get("name") or "") in _TERMINAL_KANBAN_TOOLS:
            return True
    return False


def _board_card_live_run(task_id: str) -> Optional[int]:
    """``tasks.current_run_id`` for ``task_id``, read straight from the board.

    ``None`` when the card has no live run or the board cannot be read; the caller
    then keeps the guard's pre-existing behaviour. Read-only by construction
    (``mode=ro``, no schema init/migration) so a turn-end check can never create
    the board. Mirrors ``kanban_db_notify``'s read-only open.
    """
    try:
        from hermes_cli import kanban_db as kb

        board = (os.environ.get("HERMES_KANBAN_BOARD") or "").strip() or None
        path = kb.kanban_db_path(board=board)
        if not path.exists():
            return None
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None or row[0] is None:
            return None
        return int(row[0])
    except Exception:
        return None


def _own_run_id() -> Optional[int]:
    """This session's dispatcher run id (``HERMES_KANBAN_RUN_ID``); None if unset/unparseable."""
    try:
        return int((os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip())
    except (TypeError, ValueError):
        return None


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Synthetic follow-up when a kanban worker exits without a terminal tool; ``None`` when
    the guard should not fire (not a kanban worker, already completed/blocked, budget exhausted)."""
    if (
        not kanban_stop_nudge_enabled()
        or attempts >= max_attempts
        or session_called_kanban_terminal(messages)
    ):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    live_run_id = _board_card_live_run(tid) if tid else None
    if live_run_id is not None and live_run_id != _own_run_id():
        return None  # the card's live run belongs to another lane's session
    tid = tid or "this task"

    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, OR `kanban_block(reason=...)` if you are blocked, OR "
        "`kanban_request_review` / `kanban_request_changes` when you are "
        "handing the card to the other lane.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = ["build_kanban_stop_nudge", "kanban_stop_nudge_enabled", "session_called_kanban_terminal"]
