"""Result shapes for the process tool (moved out of ``tools.process_registry``).

Verbatim home for the small result helpers the registry hands back: the not-found shape and the
ANSI-stripped output tail. Extracted so ``tools/process_registry.py`` can offset the growth the
workflow-departure plumbing needs there (FILE_LINES ratchet, t_4b9843a4); bodies unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only (the registry is the import home)
    from tools.process_registry import ProcessSession


def _not_found(session_id: str) -> dict:
    return {"status": "not_found", "error": f"No process with ID {session_id}"}


def _output_tail(session: ProcessSession, n: int) -> str:
    """Last *n* chars of the session output with ANSI sequences stripped."""
    from tools.ansi_strip import strip_ansi

    return strip_ansi(session.output_buffer[-n:])


def format_uptime_short(seconds: int) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    mins, secs = divmod(s, 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m"
