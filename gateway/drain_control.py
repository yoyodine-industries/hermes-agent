"""External drain-control marker contract (dashboard → gateway).

No control channel exists into a running gateway, so begin/cancel-drain writes
(or removes) ``{HERMES_HOME}/.drain_request.json`` and a gateway watcher reacts;
an ACTIVE marker means ``gateway_state -> "draining"``.  Two lenient staleness
signals (either suffices): epoch mismatch (HERMES_HOME is a durable volume on
Hermes Cloud, so a marker survives the restart a drain-gated action ends in and
would park the fresh gateway in ``draining`` forever) and expiry (same-epoch
orphan past :data:`DRAIN_REQUEST_MAX_AGE_SECONDS`; re-writing refreshes it).
Reading never raises: a malformed file reads as ``{}`` — still drain-active
(fail-safe toward quiescing).  Staleness rejects only on a *definite* verdict.
"""
from __future__ import annotations

import contextlib
import functools
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path
from typing import Any, Optional

from gateway.memory_status import _parse_iso
from hermes_constants import get_hermes_home
from utils import atomic_json_write

_log = logging.getLogger(__name__)

# ``drain_reason`` a graceful restart publishes for its after-turn wait (#139041). The owner still
# runs its in-flight turns, so a client may attach to answer a parked approval/clarify; new input is
# committed queued for the next owner and never claimed here. ``stop()`` withdraws it.
RESTART_WAIT_REASON = "restart_wait"


def open_restart_wait(runner) -> None:
    """Publish the restart's after-turn wait (``drain_reason``) unless a drain already named its own."""
    descriptor = getattr(runner, "session_runtime_descriptor", None)
    if descriptor is not None and not descriptor.get("drain_reason"):
        descriptor["drain_reason"] = RESTART_WAIT_REASON


def restart_wait_open(runner) -> bool:
    """True while *runner* is in a restart's after-turn wait (draining, not yet stopping)."""
    descriptor = getattr(runner, "session_runtime_descriptor", None) or {}
    return (bool(getattr(runner, "_draining", False)) and bool(getattr(runner, "_running", False))
            and descriptor.get("drain_reason") == RESTART_WAIT_REASON)


# The requests a client needs during that wait to find, open and answer the conversation parked on
# approval/clarify: the GET reads below (Desktop's session picker, the session detail and its
# transcript pages, and the health probes) and one write, a Webapp tab's mint of the one-use ticket
# its /api/ws dial-back needs (restart_wait_socket_ticket). Both only pass the drain's refusal; the
# listener's auth gate still runs on them. Every other write and route keeps that refusal. The
# prompt itself (approval.pending / *.respond) rides the WebSocket.
_RESTART_WAIT_READS = re.compile(
    r"/api/(?:health|status|profiles/sessions(?:/sidebar)?"
    r"|sessions/[^/]+(?:/messages(?:/around)?|/timeline)?)")


def restart_wait_read(runner, method, path) -> bool:
    """True for an allowlisted read request while *runner* is in a restart's after-turn wait."""
    return (method == "GET" and bool(_RESTART_WAIT_READS.fullmatch(path or ""))
            and restart_wait_open(runner))


def restart_wait_socket_ticket(runner, method, path, query) -> bool:
    """True for a gateway-socket ticket mint while *runner* is in a restart's after-turn wait.

    Exempts the request from the drain's refusal only: it does not authenticate it."""
    from urllib.parse import parse_qs
    from hermes_cli.web_session_sockets import GATEWAY_SOCKET
    return (method == "POST" and path == "/api/webapp/socket-ticket"
            and parse_qs(query or "").get("socket") == [GATEWAY_SOCKET] and restart_wait_open(runner))

# The WebSocket verbs an interactive client may still call during that wait: attach to and read the
# parked session, answer its prompt (or interrupt the turn so the restart can finish) and commit
# queued input for the next owner. Every other verb (config.set, session.save, rollback.restore,
# process.kill, reload.*, ...) would run on an owner that is shutting down, so it is refused with
# ``runtime_draining`` exactly like the REST writes above.
RESTART_WAIT_WS_METHODS = frozenset({
    "ping", "runtime.describe", "session.resume", "session.detach", "session.events.since",
    "session.info", "session.list", "prompt.submit", "prompt.receipt", "session.interrupt",
    "approval.pending", "approval.received", "approval.respond", "clarify.respond", "clarify.lock",
})


def ws_method_refused_while_draining(runner, method) -> bool:
    """True when an interactive WebSocket verb must be refused because *runner* is draining."""
    return bool(getattr(runner, "_draining", False)) and method not in RESTART_WAIT_WS_METHODS

_DRAIN_REQUEST_FILENAME = ".drain_request.json"
# Drain-gated lifecycle actions complete in minutes; an hour bounds the wedge a leaked
# marker can cause. Long drains refresh the marker instead of raising this.
# Max-age fallback for a same-epoch orphaned marker (#85433). Long-running drains refresh the marker via
# write_drain_request() (idempotent re-write bumps ``requested_at``) rather than raising this bound.
DRAIN_REQUEST_MAX_AGE_SECONDS = 3600.0

# The refusal contract every draining path shares: one stable code and, when the drain DECLARED a
# window, its expected end — so a caller backs off correctly instead of retrying blind (t_81e506a1).
DRAIN_REFUSAL_CODE = "gateway_draining"
# Ceiling on the refusal's Retry-After: the house retry hint for a gateway that is merely away
# (``_CRON_FIRE_RETRY_AFTER_SECONDS`` in hermes_cli/web_routers/cron.py). A caller that needs the
# exact end reads ``expected_end_at`` from the body; the header only paces the next attempt.
DRAIN_RETRY_AFTER_MAX_SECONDS = 60.0
# Dedup for the expired-marker warning (the watcher re-reads every second); keyed by
# ``requested_at`` so a keep-alive re-write that later expires logs again.
_expiry_logged_for: Optional[str] = None


@functools.lru_cache(maxsize=1)
def current_instantiation_epoch() -> str:
    """Identity of THIS container / VM instantiation ("<boot_id>:<pid1_start>").

    Stable for the life of PID 1 (a gateway-only respawn keeps honouring an
    in-flight drain) but changes when the machine is recreated: boot_id on a VM
    reboot, PID 1's start time on ``docker restart``.  ``""`` when neither is
    readable (non-Linux, no ``/proc``) disables the epoch check — never fail-closed.
    """
    boot_id = pid1_start = ""
    with contextlib.suppress(OSError):
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    with contextlib.suppress(OSError, IndexError):
        # "<pid> (<comm>) <state> ...": comm may contain spaces/parens, so split on the
        # LAST ')'. starttime is field 22 (1-indexed) = tail index 19.
        pid1_start = Path("/proc/1/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[19]
    return f"{boot_id}:{pid1_start}" if (boot_id or pid1_start) else ""


def drain_request_path(home: Optional[Path] = None) -> Path:
    """Absolute path to the drain-request marker, respecting HERMES_HOME."""
    return Path(home if home is not None else get_hermes_home()) / _DRAIN_REQUEST_FILENAME


def _iso_utc(moment: datetime) -> str:
    """ISO-8601 UTC with a ``Z`` suffix — the machine-readable form the refusals publish."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _declared_window_seconds(window_seconds: Any) -> Optional[float]:
    """A usable positive window in seconds, or None for absent/garbage/non-positive input."""
    try:
        seconds = float(window_seconds)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def write_drain_request(
    *, principal: str = "drain-control", suppress_notification: bool = False,
    window_seconds: Any = None, home: Optional[Path] = None,
) -> dict[str, Any]:
    """Write the begin-drain marker atomically; returns the payload.

    Re-writing refreshes ``requested_at`` (keep-alive past the max-age).
    ``suppress_notification`` skips ONLY the home-channel "gateway shutting down"
    broadcast (the per-session interrupt ping is never suppressed); which drains
    are quiet is the caller's policy.  Stamped with the instantiation epoch so a
    copy surviving a machine restart on the durable volume reads as stale.

    ``window_seconds`` is the drain's OWN declared budget: the marker then carries
    ``expected_end_at``, which every drain refusal echoes so the caller knows when
    the gateway is back.  Absent (or non-positive) records no end at all — a refusal
    must never invent one.  Bounded by the max-age, since the marker itself stops
    being honoured there; a longer drain keep-alives the marker instead.
    """
    now = datetime.now(UTC)
    payload = {
        "action": "drain", "requested_at": now.isoformat(), "principal": principal,
        "epoch": current_instantiation_epoch(), "suppress_notification": bool(suppress_notification),
    }
    window = _declared_window_seconds(window_seconds)
    if window is not None:
        payload["expected_end_at"] = _iso_utc(now + timedelta(seconds=min(window, DRAIN_REQUEST_MAX_AGE_SECONDS)))
    atomic_json_write(drain_request_path(home), payload)
    return payload


def clear_drain_request(*, home: Optional[Path] = None) -> bool:
    """Remove the drain marker (cancel-drain, idempotent). Returns True if one existed."""
    path = drain_request_path(home)
    try:
        path.unlink()
        return True
    except OSError as e:
        if not isinstance(e, FileNotFoundError):
            _log.warning("drain-control: failed to remove %s: %s", path, e)
        return False


def _marker_is_expired(body: dict[str, Any]) -> bool:
    """True iff ``requested_at`` parses AND is older than the max-age.

    Missing/unparseable and future-dated (clock skew) timestamps are honoured.
    Logged once per marker, not per poll — the operator's breadcrumb for a leak.

    See #85433.
    """
    global _expiry_logged_for
    raw = body.get("requested_at")
    requested_at = _parse_iso(raw)
    if requested_at is None:
        return False
    age = (datetime.now(UTC) - requested_at).total_seconds()
    if age <= DRAIN_REQUEST_MAX_AGE_SECONDS:
        return False
    if _expiry_logged_for != raw:
        _expiry_logged_for = raw
        _log.warning(
            "drain-control: ignoring expired drain marker (requested_at=%s, age=%.0fs > max %.0fs, principal=%s) "
            "— the drain that wrote it was never cancelled; treating as stale so the gateway keeps accepting turns.",
            raw, age, DRAIN_REQUEST_MAX_AGE_SECONDS, body.get("principal"),
        )
    return True


def _active_drain_body(home: Optional[Path]) -> Optional[dict[str, Any]]:
    """Marker body if present AND not stale (definite epoch mismatch or expired), else None."""
    body = read_drain_request(home=home)
    if body is None:
        return None
    current, marker_epoch = current_instantiation_epoch(), body.get("epoch")
    if (current and marker_epoch and marker_epoch != current) or _marker_is_expired(body):
        return None
    return body


def drain_requested(*, home: Optional[Path] = None) -> bool:
    """True iff an active (present, same-epoch, unexpired) begin-drain marker exists.

    A marker whose ``epoch`` does not match the current instantiation epoch is treated as absent: it
    survived a container/VM restart (HERMES_HOME is a durable Fly volume on Hermes Cloud) and the lifecycle
    action that triggered the drain has already completed — honouring it would wedge the freshly-restarted
    gateway in ``draining`` (NS-570). A marker whose ``requested_at`` is older than
    :data:`DRAIN_REQUEST_MAX_AGE_SECONDS` is likewise treated as absent: it is a same-epoch orphan whose
    drain-gated action completed without a restart and was never cancelled (#85433). Both staleness checks
    are lenient (see :func:`_marker_epoch_is_stale` / :func:`_marker_is_expired`): a legacy/corrupt marker
    with no epoch and no timestamp, or an environment without ``/proc``, still reads as drain-active.
    """
    return _active_drain_body(home) is not None


def drain_notification_suppressed(*, home: Optional[Path] = None) -> bool:
    """True iff an ACTIVE marker asks to suppress the shutdown broadcast.

    Same activeness rule as :func:`drain_requested`, so an orphan can never silence
    a fresh gateway; a marker without the field reads False (fail toward louder).

    "Active" means exactly what :func:`drain_requested` means — a marker present AND stamped with the
    current instantiation epoch AND not past its max-age. A stale (other-epoch) marker that survived a
    machine restart on the durable HERMES_HOME volume, or an expired same-epoch orphan (#85433), is ignored
    here just as it is for drain state (NS-570): we must never let an orphaned marker's flag silence a
    *fresh* gateway's legitimate shutdown broadcast.
    """
    body = _active_drain_body(home)
    return bool(body and body.get("suppress_notification"))


def drain_window_end(*, home: Optional[Path] = None) -> Optional[datetime]:
    """The ACTIVE marker's declared window end, or None when this drain declared none.

    Only a marker written with ``window_seconds`` carries one; an in-process restart drain has no
    marker at all, so both read None. A refusal over such a drain must omit the time rather than
    invent one. Never raises.
    """
    body = _active_drain_body(home)
    return _parse_iso(body.get("expected_end_at")) if body else None


def drain_refusal(
    *, home: Optional[Path] = None, now: Optional[datetime] = None,
) -> tuple[dict[str, Any], int]:
    """``(body fields, Retry-After seconds)`` for EVERY refusal caused by a drain.

    One source of truth, so no path can word a drain 503 differently: ``code`` is always
    :data:`DRAIN_REFUSAL_CODE`, and ``expected_end_at`` / ``seconds_remaining`` appear only when the
    drain declared a window (:func:`write_drain_request`). ``Retry-After`` paces the next attempt:
    the remaining whole seconds (never below 1, so a window that already passed means retry now),
    capped at :data:`DRAIN_RETRY_AFTER_MAX_SECONDS`, and that cap when nothing was declared.
    """
    end = drain_window_end(home=home)
    fields: dict[str, Any] = {"code": DRAIN_REFUSAL_CODE}
    if end is None:
        return fields, int(DRAIN_RETRY_AFTER_MAX_SECONDS)
    remaining = (end - (now or datetime.now(UTC))).total_seconds()
    fields["expected_end_at"] = _iso_utc(end)
    fields["seconds_remaining"] = max(0, math.ceil(remaining))
    return fields, int(min(max(1, fields["seconds_remaining"]), DRAIN_RETRY_AFTER_MAX_SECONDS))


def read_drain_request(*, home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Return the marker payload, ``{}`` if present but unparseable, ``None`` if absent. Never raises."""
    path = drain_request_path(home)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        if not isinstance(e, FileNotFoundError):
            _log.warning("drain-control: failed to read %s: %s", path, e)
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}
