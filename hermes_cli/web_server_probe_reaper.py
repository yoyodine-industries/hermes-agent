"""Idle state.db header-probe reaper for the dashboard backend.

A dashboard listing request opens each profile's ``state.db`` read-only and closes it, but
``hermes_state_dbfile`` keeps one O_RDONLY *probe* fd per store for the life of the process (the
lock-safe byte-read path: closing a probe can cancel the process's POSIX locks, so it is cached).
The consequence is that a backend which once listed a profile stays a FOREIGN HOLDER of that
store for as long as it runs, even when it serves nothing -- and ``hermes_state_holders`` then
reports the store as held, which is what makes an owner-applied ``auto_vacuum`` rewrite
(``vacuum_skipped_holders``) skip it forever.

This loop is the bounded half of that cache: it releases the entries nothing has read for
``HERMES_STATE_PROBE_IDLE_SECONDS`` (default 60s). The release itself is lock-safe by construction
(``hermes_state_registry.release_idle_header_probes``): a probe whose inode still has another
descriptor in this process stays, so an open chat session or the gateway's writer is never
disturbed -- the dashboard becomes a holder of only the stores it is actually serving.
"""

from __future__ import annotations

import asyncio
import logging
import os

from hermes_state_dbfile import HEADER_PROBE_IDLE_SECONDS

_log = logging.getLogger(__name__)

DEFAULT_REAP_INTERVAL_SECONDS = 15.0
_IDLE_SECONDS_ENV = "HERMES_STATE_PROBE_IDLE_SECONDS"


def header_probe_idle_seconds() -> float:
    """The idle window, override-able for operators and tests; a non-positive value is ignored."""
    raw = os.environ.get(_IDLE_SECONDS_ENV, "").strip()
    try:
        value = float(raw)
    except ValueError:
        return HEADER_PROBE_IDLE_SECONDS
    return value if value > 0 else HEADER_PROBE_IDLE_SECONDS


def reap_idle_header_probes() -> int:
    """One sweep; returns how many cached probes were closed (0 when everything is live/idle-free)."""
    from hermes_state_registry import release_idle_header_probes

    released = release_idle_header_probes(header_probe_idle_seconds())
    if released:
        _log.debug("released %d idle state.db header probe fd(s)", released)
    return released


async def _header_probe_reaper_loop(
    interval_s: float = DEFAULT_REAP_INTERVAL_SECONDS,
    initial_delay_s: float = DEFAULT_REAP_INTERVAL_SECONDS,
) -> None:
    """Release idle header probes forever; one bounded sweep per interval, off the event loop."""
    await asyncio.sleep(initial_delay_s)
    while True:
        try:
            await asyncio.to_thread(reap_idle_header_probes)
        except Exception as exc:  # a sweep must never take the backend down
            _log.debug("idle header-probe reap skipped: %s", exc)
        await asyncio.sleep(interval_s)
