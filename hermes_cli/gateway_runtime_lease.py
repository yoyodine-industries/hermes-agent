"""The client half of the gateway's published cold-start lease.

``gateway/startup_progress.py`` publishes a ``startup`` object inside ``gateway_state.json``
for the whole cold start, and refreshes that record's ``updated_at`` on a heartbeat. A client
that cannot reach the control socket reads THIS record to tell a start that is still working
from one that died, and gets its wait from the owner's own ``ready_by`` instead of a fixed
budget that a slow cold start can legitimately outrun.

This module is read-only and reads the one status artifact — never a second one, and it never
writes. The numbers mirror the owner's writer invariants:

* the heartbeat ticks every ``startup_progress.HEARTBEAT_INTERVAL_S`` (15 s), so three missed
  ticks — :data:`STALE_AFTER_S` — is the first honest sign the writer stopped;
* a phase renews before its own estimate lapses with a margin of at least 30 s, so a
  ``ready_by`` that passed more than :data:`READY_BY_GRACE_S` ago without a renewal is not
  "nearly ready", it is dead;
* :data:`CLIENT_CEILING_S` equals ``hermes_startup_watchdog._MAX_LEASE_S``: no client gives up
  on a start the watchdog is still honoring.

An older owner that publishes no ``startup`` record keeps today's behaviour: the ensure loop's
base budget, untouched (``LEGACY`` below).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import time
from typing import Any

#: Furthest a client may push its wait on a live lease: the startup watchdog's own ceiling.
CLIENT_CEILING_S = 900.0

#: Three missed 15 s heartbeats. Older ``updated_at`` and the writer is gone.
STALE_AFTER_S = 90.0

#: How long past ``ready_by`` an un-renewed lease may still be believed.
READY_BY_GRACE_S = 30.0

#: Poll cadence while waiting on a lease: one read per second, easing to one per five. The
#: 50 ms ensure poll would do tens of thousands of file reads over a ten-minute cold start.
POLL_MIN_S = 1.0
POLL_MAX_S = 5.0

#: Bound on one control-socket probe while a lease is being waited on. The socket answering is
#: the READY signal, so a probe must never consume the whole horizon: a wedged socket would
#: otherwise turn a stalled owner into ``starting: deadline`` without one lease re-read.
PROBE_TIMEOUT_S = 10.0

#: ``startup.phase`` values the owner's writer uses. Duplicated deliberately: a client must not
#: import the owner's startup machinery (which arms a heartbeat) just to read a phase name.
PHASE_READY = "ready"

LIVE = "live"
LEGACY = "none"
STALLED = "stalled"


@dataclass(frozen=True)
class StartupLease:
    """One published ``startup`` object, with its record's own freshness stamp."""

    pid: int
    phase: str
    ready_by: float
    renewals: int
    updated_at: float  # epoch seconds: ``gateway_state.json``'s own ``updated_at``
    boot_started_at: float | None = None
    phase_started_at: float | None = None

    def age_s(self, now: float) -> float:
        """Seconds since the record was last refreshed (the writer's liveness evidence)."""
        return max(0.0, now - self.updated_at)

    def elapsed_s(self, now: float) -> float:
        """Seconds since this life's start began, from the owner's own stamps."""
        started = self.boot_started_at or self.phase_started_at or self.updated_at
        return max(0.0, now - started)


@dataclass(frozen=True)
class LeaseWait:
    """What a client should do with the record it just read.

    ``kind`` is one of :data:`LIVE` (wait, and for how long: :func:`horizon_s`), :data:`LEGACY`
    (no usable record: keep the caller's own deadline) or :data:`STALLED` (stop, and report
    ``detail`` naming the phase).
    """

    kind: str
    lease: StartupLease | None = None
    renewals: int = 0
    detail: str | None = None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _record_updated_at(record: dict) -> float | None:
    """The status record's top-level ``updated_at`` as epoch seconds.

    The heartbeat refreshes this stamp on every ``startup`` write, so it — not a field inside
    ``startup`` — is the lease's liveness evidence. Unparseable means "cannot date this
    record", which is not proof of life: the caller keeps its own deadline.
    """
    from datetime import datetime

    from gateway.status import normalize_updated_at

    stamp = normalize_updated_at(record.get("updated_at"))
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def read_startup_lease(home: str | Path) -> StartupLease | None:
    """The ``startup`` object published in *home*'s ``gateway_state.json``, or None.

    None covers every shape that must NOT move a client's deadline: no file, no ``startup``
    object (an older owner), an unreadable or non-monotonic record, a record dated in the
    future, and a start that already reached ``ready`` (its window is closed; the control
    socket, not this lease, is then the authority).
    """
    from gateway.status import read_runtime_status

    record = read_runtime_status(Path(home) / "gateway_state.json")
    if not isinstance(record, dict):
        return None
    startup = record.get("startup")
    if not isinstance(startup, dict):
        return None
    phase = startup.get("phase")
    if not isinstance(phase, str) or not phase or phase == PHASE_READY:
        return None
    pid = startup.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    ready_by = _number(startup.get("ready_by"))
    renewals = startup.get("renewals", 0)
    if ready_by is None or isinstance(renewals, bool) or not isinstance(renewals, int):
        return None
    updated_at = _record_updated_at(record)
    if updated_at is None:
        return None
    boot_started_at = _number(startup.get("boot_started_at"))
    phase_started_at = _number(startup.get("phase_started_at"))
    return StartupLease(
        pid=pid, phase=phase, ready_by=ready_by, renewals=renewals, updated_at=updated_at,
        boot_started_at=boot_started_at, phase_started_at=phase_started_at,
    )


def horizon_s(lease: StartupLease, now: float) -> float:
    """Seconds from *now* to the furthest wait this lease justifies.

    ``min(ready_by + 30 s, now + CLIENT_CEILING_S)``, never negative — a lease whose deadline
    already passed still leaves the stall rules to decide (see :func:`assess`).
    """
    return max(0.0, min(lease.ready_by + READY_BY_GRACE_S, now + CLIENT_CEILING_S) - now)


def _stall_detail(lease: StartupLease, now: float) -> str:
    """``<phase>, <elapsed>s, last renewal <age>s`` — the worker's message names the phase."""
    return f"{lease.phase}, {lease.elapsed_s(now):.0f}s, last renewal {lease.age_s(now):.0f}s"


def assess(lease: StartupLease | None, *, owner_pid: int | None, renewals_seen: int | None,
           now: float) -> LeaseWait:
    """Decide whether *lease* still describes a start worth waiting for.

    ``owner_pid`` is the live gateway the filesystem proves (``gateway.pid``/``gateway.lock``),
    None when neither names a live owner. ``renewals_seen`` is this call's previous reading of
    ``startup.renewals``, None on the first poll.

    Still starting: the record is fresh (``updated_at`` within 90 s) AND (``ready_by`` is in the
    future, a renewal landed since the previous poll, or ``ready_by`` passed less than 30 s ago
    and the record is still being refreshed).

    Dead, a named verdict: no live owner at all, ``updated_at`` older than 90 s, or ``ready_by``
    passed more than 30 s ago with no renewal.
    """
    if lease is None:
        return LeaseWait(LEGACY)
    if owner_pid is None:
        # gateway.pid and gateway.lock name no live process: whatever this record says, the
        # start that published it is over.
        return LeaseWait(STALLED, lease=lease, renewals=lease.renewals,
                         detail=_stall_detail(lease, now))
    if lease.pid != owner_pid:
        # Another life's record (a crashed owner's, or one this call is not waiting on): it can
        # say nothing about THIS start. Keep the caller's own deadline, as for an older owner.
        return LeaseWait(LEGACY)
    if lease.age_s(now) > STALE_AFTER_S:
        return LeaseWait(STALLED, lease=lease, renewals=lease.renewals,
                         detail=_stall_detail(lease, now))
    renewed = renewals_seen is not None and lease.renewals > renewals_seen
    if lease.ready_by > now or renewed or lease.ready_by + READY_BY_GRACE_S >= now:
        return LeaseWait(LIVE, lease=lease, renewals=lease.renewals)
    return LeaseWait(STALLED, lease=lease, renewals=lease.renewals, detail=_stall_detail(lease, now))


def observe(home: str | Path, *, owner_pid: int | None, renewals_seen: int | None,
            now: float | None = None) -> LeaseWait:
    """Read *home*'s lease and assess it (the one call the ensure loop makes)."""
    return assess(read_startup_lease(home), owner_pid=owner_pid,
                  renewals_seen=renewals_seen, now=time.time() if now is None else now)
