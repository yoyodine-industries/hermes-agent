"""The gateway's published cold-start lease: ``gateway_state.json`` ``startup``.

A cold start can legitimately take ~10 minutes — the multiplexer opens nine ``state.db``
stores (~10 GiB) inside ``GatewayRunner.__init__``, long before the event loop publishes
anything — and until this module existed the file a client reads to tell "coming up" from
"dead" carried the PREVIOUS life's record: a dead pid reported as ``running``. The only
other progress evidence was a 600 s startup-watchdog lease claimed unconditionally by a
migration chain that had nothing to migrate.

This module owns:

* the ONE phase vocabulary (``PHASES``) published under ``startup.phase``;
* the ``startup`` object itself::

      {"pid", "boot_started_at", "phase", "phase_started_at", "phase_expected_s",
       "ready_by", "renewals", "ready_at"}

  ``ready_by`` is epoch seconds — ``phase_started_at + phase_expected_s`` — and the SAME
  budget is handed to :func:`hermes_startup_watchdog.report_startup_progress`, so a client
  and the startup watchdog read one number;
* the heartbeat that refreshes ``updated_at`` every :data:`HEARTBEAT_INTERVAL_S` and renews
  a phase's own lease before ``ready_by`` lapses (rewriting ``ready_by``, bumping
  ``renewals``), for the startup window only — it stops at ready, after which the gateway's
  normal status writer owns the record.

Two invariants worth stating because they are easy to break:

* **Never invent progress.** A phase claims a lease only while it is genuinely doing the
  work named by the phase; a no-op migration chain claims nothing.
* **Never disturb the start.** Every call is best-effort and bounded: a failed progress
  write must not abort, or block past its own ``wait_timeout``, the startup it observes.

No second status artifact: ``gateway_state.json`` is the one.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PHASE_RUNTIME_IMPORT = "runtime_import"
PHASE_STORE_OPEN = "store_open"
PHASE_DATA_MIGRATIONS = "data_migrations"
PHASE_SERVICES_WIRING = "services_wiring"
PHASE_ADAPTERS = "adapters"
PHASE_READY = "ready"

#: The published vocabulary, in the order the phases occur. No other value may appear under
#: ``startup.phase``; the order also makes a publish monotonic (a later store open can never
#: pull the phase back from ``data_migrations``).
PHASES = (
    PHASE_RUNTIME_IMPORT,
    PHASE_STORE_OPEN,
    PHASE_DATA_MIGRATIONS,
    PHASE_SERVICES_WIRING,
    PHASE_ADAPTERS,
    PHASE_READY,
)
_PHASE_INDEX = {phase: index for index, phase in enumerate(PHASES)}

#: Honest worst case per phase, in seconds — measured, not guessed: the 565 s cold start was
#: ~300 s of store opening, the rest imports + adapter wiring. ``data_migrations`` keeps the
#: 600 s budget the migration chain has always used, now claimed ONLY when it has work.
PHASE_EXPECTED_S: Dict[str, float] = {
    PHASE_RUNTIME_IMPORT: 120.0,
    PHASE_STORE_OPEN: 300.0,
    PHASE_DATA_MIGRATIONS: 600.0,
    PHASE_SERVICES_WIRING: 120.0,
    PHASE_ADAPTERS: 180.0,
    PHASE_READY: 0.0,
}

#: A heartbeat that leaves ``updated_at`` older than 30 s makes readers call a live start
#: stale, so the writer ticks at half that.
HEARTBEAT_INTERVAL_S = 15.0

#: Floor on the renewal margin (see ``_renew_margin_s``) — a phase renews BEFORE its own
#: estimate lapses, so a client following ``ready_by`` never sees it pass while work goes on.
_MIN_RENEW_MARGIN_S = 30.0

#: Bounded wait for each progress write. The write is a small JSON merge on the existing
#: runtime-status writer; a stalled disk delays the start by at most this, never hangs it.
_WRITE_WAIT_S = 2.0

#: Join bound when stopping the heartbeat (it may be mid-write).
_HEARTBEAT_JOIN_S = 5.0

_lock = threading.RLock()
_lease: Optional[Dict[str, Any]] = None
_stop: Optional[threading.Event] = None
_thread: Optional[threading.Thread] = None


def _now() -> float:
    """Seam for tests; production is wall-clock epoch seconds (the unit ``ready_by`` uses)."""
    return time.time()


def _renew_margin_s(expected_s: float) -> float:
    """Renew once this much of the phase's own budget is left (half, floored)."""
    return max(_MIN_RENEW_MARGIN_S, float(expected_s) / 2.0)


def _publish(lease: Dict[str, Any], *, wait_timeout: float = _WRITE_WAIT_S) -> None:
    """Merge ``lease`` into ``gateway_state.json`` through the one runtime-status writer."""
    try:
        from gateway.status import write_runtime_status

        write_runtime_status(startup=dict(lease), wait_timeout=wait_timeout)
    except Exception:
        # Progress publication must never abort a startup.
        logger.debug("Could not publish the startup lease", exc_info=True)


def _report_lease(lease: Dict[str, Any]) -> None:
    """Hand the phase's own budget to the startup watchdog (no-op when it is not armed)."""
    try:
        from hermes_startup_watchdog import report_startup_progress

        report_startup_progress(float(lease["phase_expected_s"]), phase=str(lease["phase"]))
    except Exception:
        logger.debug("Could not report the startup lease to the watchdog", exc_info=True)


def _start_heartbeat() -> None:
    global _stop, _thread
    stop = threading.Event()
    thread = threading.Thread(
        target=_heartbeat_loop, args=(stop,), daemon=True, name="gateway-startup-heartbeat")
    try:
        thread.start()
    except Exception:
        logger.debug("Could not start the startup heartbeat", exc_info=True)
        return
    _stop, _thread = stop, thread


def _stop_heartbeat() -> None:
    """Stop the heartbeat thread; call WITHOUT holding ``_lock`` (the ticker takes it)."""
    global _stop, _thread
    with _lock:
        stop, thread = _stop, _thread
        _stop = _thread = None
    if stop is not None:
        stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=_HEARTBEAT_JOIN_S)


def _heartbeat_loop(stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_INTERVAL_S):
        try:
            heartbeat_tick()
        except Exception:
            logger.debug("Startup heartbeat tick failed", exc_info=True)


def begin_startup(pid: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Publish the first lease for THIS life — called at the PID-file claim, before the stores.

    Writes ``gateway_state="starting"`` / ``exit_reason=None`` plus the ``startup`` object, and
    cycles the lifecycle sentinel to ``phase="starting"`` for this pid, so no reader sees the
    previous (dead) life as ``running`` during the cold start. Idempotent per process.
    """
    global _lease
    already_running = False
    with _lock:
        if _lease is not None and _lease.get("phase") != PHASE_READY:
            already_running = True
            started = dict(_lease)
        else:
            now = _now()
            budget = PHASE_EXPECTED_S[PHASE_RUNTIME_IMPORT]
            _lease = {
                "pid": int(pid if pid is not None else os.getpid()),
                "boot_started_at": now,
                "phase": PHASE_RUNTIME_IMPORT,
                "phase_started_at": now,
                "phase_expected_s": budget,
                "ready_by": now + budget,
                "renewals": 0,
                "ready_at": None,
            }
            started = dict(_lease)
    if already_running:
        return started
    # gateway_state/exit_reason are written HERE, not by the heartbeat: this first write is what
    # replaces the dead life's "running" with an honest "starting" for the new pid.
    try:
        from gateway.status import write_runtime_status

        write_runtime_status(
            gateway_state="starting", exit_reason=None, startup=dict(started),
            wait_timeout=_WRITE_WAIT_S)
    except Exception:
        logger.debug("Could not publish the first startup lease", exc_info=True)
    try:
        from gateway.lifecycle_ledger import claim_starting

        claim_starting()
    except Exception:
        logger.debug("Could not cycle the lifecycle sentinel to starting", exc_info=True)
    _report_lease(started)
    _start_heartbeat()
    return started


def _enter_phase(lease: Dict[str, Any], phase: str, own_budget: float, now: float) -> Dict[str, Any]:
    """Move ``lease`` to ``phase``, holding a deadline that never moves earlier.

    A phase's own worst case is ``own_budget``, but the deadline already published to clients and
    to the watchdog may be further out (a cheap phase following a long one). Shortening it would
    tell a waiting client "not before T" and then "ready by T-300", so the phase inherits the
    outstanding deadline instead: ``phase_expected_s`` is the lease it actually holds, and
    ``ready_by == phase_started_at + phase_expected_s`` still holds exactly.
    """
    outstanding = float(lease.get("ready_by") or 0.0) - now
    budget = max(float(own_budget), outstanding) if outstanding > 0 else float(own_budget)
    lease.update(
        phase=phase, phase_started_at=now, phase_expected_s=budget, ready_by=now + budget)
    return dict(lease)


def advance_startup_phase(phase: str, expected_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Publish ``phase`` at a startup boundary, with its own budget, and lease the watchdog.

    Monotonic: an earlier phase (or a repeat of the current one) is ignored, so store N's open
    cannot pull the published phase back from ``data_migrations`` once store 1 has entered it.

    When no gateway cold start is in flight (a CLI command opening the same stores), nothing is
    published, but the watchdog still gets the phase's honest budget — that is what replaces the
    old unconditional 600 s lease.
    """
    if phase not in _PHASE_INDEX:
        logger.debug("Ignoring unknown startup phase %r (not in %s)", phase, PHASES)
        return None
    budget = float(PHASE_EXPECTED_S[phase] if expected_s is None else expected_s)
    with _lock:
        lease = _lease
        if lease is not None and lease.get("phase") == PHASE_READY:
            return None  # the startup window is closed; the loop's writer owns the record now
        if lease is None:
            no_startup = True
            snapshot: Optional[Dict[str, Any]] = None
        else:
            no_startup = False
            current_index = _PHASE_INDEX.get(str(lease.get("phase")), -1)
            if _PHASE_INDEX[phase] <= current_index:
                return dict(lease)
            snapshot = _enter_phase(lease, phase, budget, _now())
    if no_startup:
        _report_lease({"phase": phase, "phase_expected_s": budget})
        return None
    _publish(snapshot or {})
    _report_lease(snapshot or {})
    return snapshot


def heartbeat_tick() -> Optional[Dict[str, Any]]:
    """One heartbeat: refresh ``updated_at``; renew the lease before its estimate lapses.

    A renewal rewrites ``ready_by`` to ``now + phase_expected_s`` and bumps ``renewals``, and
    re-arms the watchdog for the same budget — the renewal IS the liveness evidence.
    """
    with _lock:
        lease = _lease
        if lease is None or lease.get("phase") == PHASE_READY:
            return None
        now = _now()
        budget = float(lease.get("phase_expected_s") or 0.0)
        renewed = False
        if now >= float(lease.get("ready_by") or 0.0) - _renew_margin_s(budget):
            lease["renewals"] = int(lease.get("renewals") or 0) + 1
            lease["ready_by"] = now + budget
            renewed = True
        snapshot = dict(lease)
    _publish(snapshot)
    if renewed:
        _report_lease(snapshot)
    return snapshot


def mark_startup_ready() -> Optional[Dict[str, Any]]:
    """Stamp ``phase="ready"`` + ``ready_at`` and stand the heartbeat down.

    The ready phase holds no new time: it inherits the outstanding deadline through
    :func:`_enter_phase` (whose ``ready_by`` never moves backwards) while ``ready_at`` carries the
    moment the start actually completed. The gateway's normal status writer owns the record from
    here; this module writes nothing more for the life.
    """
    global _lease
    with _lock:
        lease = _lease
        if lease is None:
            return None
        now = _now()
        _enter_phase(lease, PHASE_READY, 0.0, now)
        lease["ready_at"] = now
        snapshot = dict(lease)
    _publish(snapshot)
    _stop_heartbeat()
    return snapshot


def stop_startup_progress() -> None:
    """Stop the heartbeat on an aborted start (never stamps ready). Idempotent."""
    _stop_heartbeat()


def startup_snapshot() -> Optional[Dict[str, Any]]:
    """The current lease as published (``None`` when no cold start is in flight)."""
    with _lock:
        return dict(_lease) if _lease is not None else None


def _reset_for_tests() -> None:
    """Drop the module state (test isolation only)."""
    global _lease
    stop_startup_progress()
    with _lock:
        _lease = None
