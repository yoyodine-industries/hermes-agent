"""Ephemeral scaffolding flags: messages the loop appends for recovery/retry only.

The durable transcript must never contain them — a resumed session would replay
synthetic turns and the prefix cache would diverge. ONE definition shared by the
agent loop (`agent.session_persistence`, `agent.turn_final_response`) and by the
SQLite writers (`hermes_state_messages`), which refuse them at the insert boundary.

Layering: root-level module with no imports, so both the `agent` package and the
`hermes_state*` writers can depend on it without a cycle.
"""

from __future__ import annotations

from typing import Any

EPHEMERAL_SCAFFOLDING_FLAGS = (
    "_empty_recovery_synthetic",
    "_empty_terminal_sentinel",
    "_thinking_prefill",
    "_verification_stop_synthetic",  # verify-on-stop nudge; the assistant candidate itself is NOT synthetic
    "_pre_verify_synthetic",
    "_kanban_stop_synthetic",  # kanban worker stop-guard
    "_dropped_toolcall_nudge",  # internal retry instruction; must not replay as user context
)


def is_ephemeral_scaffolding(msg: Any) -> bool:
    """True when ``msg`` is internal recovery scaffolding that must never reach the durable transcript."""
    return isinstance(msg, dict) and any(msg.get(flag) for flag in EPHEMERAL_SCAFFOLDING_FLAGS)
