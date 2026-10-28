"""Due-time parsing for time-gated kanban cards (the ``due_at`` contract).

The WRITE side lives in ``hermes_cli.kanban_db`` -- ``schedule_task(due_at=...)``
and ``block_task(due_at=...)`` store an absolute ``due_at`` (epoch seconds) --
and the sanctioned verbs that express it are ``hermes kanban schedule --due``
and ``hermes kanban block --due``. Omitting a due time is the HUMAN HOLD: a
card with ``due_at IS NULL`` waits for a person, never for a clock, by design.

The RELEASE leg for a fenced card is the deployed prod ``maintenance-sweep``
workflow's ``scheduled_wake`` node (``scripts/kanban_unblocker_nodes/
scheduled_wake.py`` reads ``due_at`` on ``scheduled`` and ``blocked`` cards).
It is THE one releaser for that cohort and this module deliberately wires
nothing: two releasers for one cohort is the two-implementations defect.

The waker that used to live here (``wake_due_cards``, its ``Band``/``WindowMap``
execution-window map and its ``Deferral``/``WakeOutcome`` result types) was
never wired to anything, duplicated the deployed window logic, and assumed
``kanban_db`` helpers (``list_due_tasks``/``defer_due``/``record_due_waker_tick``)
that do not exist. It is retired, so this module no longer imports
``kanban_db`` and is importable anywhere.

What stays is the pure parse/format pair the CLI verbs need:

* :func:`parse_due` -- ``+30m`` / ``+2h`` / ``+1d`` / epoch / ISO-8601 -> epoch seconds.
* :func:`format_due` -- epoch seconds -> a human string.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any
from typing import Optional


_DUE_RELATIVE_RE = re.compile(r"^\+\s*(\d+)\s*([smhd])$")
_DUE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DUE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z",
)


def parse_due(value: Any, *, now: Optional[int] = None, tzinfo=None) -> int:
    """Epoch seconds from a due-time string.

    Accepts ``+30m`` / ``+2h`` / ``+1d`` / ``+45s`` (relative to ``now``), a bare
    epoch (9+ digits) or an ISO-8601 timestamp (``2026-09-16T01:40``).

    A naive timestamp is read on the MAP's clock (the same clock the bands use),
    so ``--due 01:40`` and a band that starts at 01:00 agree about when 01:40 is.
    Raises :class:`ValueError` on anything else -- a mistyped due time must fail
    loudly rather than silently park a card forever.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty due time")
    base = int(now if now is not None else time.time())
    match = _DUE_RELATIVE_RE.match(text)
    if match:
        return base + int(match.group(1)) * _DUE_UNITS[match.group(2)]
    if text.isdigit() and len(text) >= 9:
        return int(text)
    for fmt in _DUE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tzinfo or datetime.now().astimezone().tzinfo)
        return int(parsed.timestamp())
    raise ValueError(
        f"cannot read {value!r} as a due time -- use ISO-8601 (2026-09-16T01:40), "
        f"a relative offset (+30m, +2h, +1d) or epoch seconds"
    )


def format_due(epoch: Optional[int], tzinfo=None) -> str:
    """Human rendering of a due time, in the map's timezone when we know it."""
    if not epoch:
        return "no due time"
    tz = tzinfo or datetime.now().astimezone().tzinfo
    return datetime.fromtimestamp(int(epoch), tz).strftime("%Y-%m-%d %H:%M %Z")

