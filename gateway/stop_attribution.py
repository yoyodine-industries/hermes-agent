"""Stop attribution — WHO asked a gateway to stop, WHY, and by WHICH delivery.

``logs/gateway-exit-diag.log`` is the ONE append-only artifact (the fleet already greps it).
Four tags share one ``stop_id`` (``uuid4().hex``), so a stop is reconstructable from the log
alone:

* ``gateway.stop_requested`` — written by the REQUESTER *before* it delivers anything (marker,
  signal, service-manager stop).  Carries ``target{pid,start_time,home}``, ``delivery{method}``
  and ``requester{pid,ppid,uid,argv(redacted),kind,reason_code,reason_text,lane,lull_gate}``.
* ``gateway.stop_seen`` — written by the gateway when it ACCEPTS a request (marker consumed or
  signal received): the same ``stop_id``, the requester verbatim, and the ``phase`` that saw it.
* ``gateway.forced_exit`` — appended by the shutdown/liveness watchdogs before ``os._exit``.
* ``gateway.previous_exit`` — appended by the next boot for EVERY prior life (clean, forced,
  unclean), from the lifecycle sentinel.

Best-effort by contract: a forensic write must never raise into (or block) a stop path, so every
public function swallows its own failures and returns a record/no-op instead.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional

# Bounded vocabulary (fixed by the ruling). A value outside it records as ``unknown`` rather
# than silently widening the vocabulary, so a reader can always enumerate a stop's cause.
STOP_KINDS = (
    "cli_stop", "replace_takeover", "update", "dashboard", "window", "maintenance",
    "launchd", "operator", "unknown",
)
DEFAULT_STOP_KIND = "unknown"
# Whether a zero-work lull was consulted before the delivery. ``not_consulted`` is the honest
# default: only a path that actually ran a gate may claim ``passed`` / ``bypassed``.
LULL_GATES = ("passed", "bypassed", "not_consulted")
DEFAULT_LULL_GATE = "not_consulted"
# Delivery methods in use today; a host may add an init system, hence the free-form ``raw``.
DELIVERY_METHODS = ("marker", "sigterm", "sigint", "launchd", "systemd", "s6", "dashboard",
                    "in_band", "unknown")

STOP_REQUESTED = "gateway.stop_requested"
STOP_SEEN = "gateway.stop_seen"
FORCED_EXIT = "gateway.forced_exit"
PREVIOUS_EXIT = "gateway.previous_exit"

# How much of the log tail is scanned to correlate a signal stop (or a SIGKILLed life) with the
# request that caused it. Bounded so the shutdown handler stays a fast path.
_TAIL_BYTES = 256 * 1024
_TAIL_MAX_RECORDS = 800
# A request older than this cannot plausibly explain a stop observed now. Wide enough to cover a
# gateway SIGKILLed at night and rebooted by the supervisor hours later.
DEFAULT_REQUEST_MAX_AGE_S = 6 * 3600.0
_ARGV_MAX_ARGS = 40
_ARGV_MAX_CHARS = 200
# Flags whose VALUE is a credential, and key hints that mark ``KEY=secret`` argv entries.
_SECRET_FLAGS = ("token", "api-key", "apikey", "password", "passwd", "secret", "auth",
                 "authorization", "bearer")
_SECRET_KEY_HINTS = ("token", "secret", "password", "passwd", "api_key", "apikey", "auth",
                     "credential", "private_key")


# ── small helpers ─────────────────────────────────────────────────────────────

def new_stop_id() -> str:
    """A fresh stop id (uuid4 hex): the thread every row of one stop carries."""
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: Any) -> Optional[float]:
    """Epoch seconds for an ISO-8601 record timestamp; None when absent/unparseable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _process_home() -> Path:
    """HERMES_HOME of this process (follows the env live, as every identity file does)."""
    from hermes_constants import get_hermes_home, get_process_hermes_home

    if os.environ.get("HERMES_HOME", "").strip():
        return get_process_hermes_home()
    return get_hermes_home()


def exit_diag_path(home: Optional[Path] = None) -> Path:
    """``<HERMES_HOME>/logs/gateway-exit-diag.log`` — the fleet's one stop artifact."""
    return (home if home is not None else _process_home()).joinpath("logs", "gateway-exit-diag.log")


def normalize_kind(kind: Any) -> str:
    value = str(kind or "").strip().lower()
    return value if value in STOP_KINDS else DEFAULT_STOP_KIND


def normalize_lull_gate(lull_gate: Any) -> str:
    value = str(lull_gate or "").strip().lower()
    return value if value in LULL_GATES else DEFAULT_LULL_GATE


def redact_argv(argv: Optional[list[str]]) -> list[str]:
    """Copy of ``argv`` safe to persist: secret flag values and ``*_TOKEN=``-style assignments
    are replaced, each argument is length-capped, and the list is bounded.

    Argv is what makes a stop attributable (``hermes gateway stop``, a helper script, a cron
    shell). It is also the field most likely to carry a credential, so redaction happens here,
    at the single writer, instead of relying on every call site.
    """
    if not argv:
        return []
    out: list[str] = []
    redact_next = False
    for raw in list(argv)[:_ARGV_MAX_ARGS]:
        arg = str(raw)[:_ARGV_MAX_CHARS]
        if redact_next:
            out.append("<redacted>")
            redact_next = False
            continue
        key_part, sep, _value = arg.partition("=")
        bare = key_part.lstrip("-").lower()
        if sep and (bare in _SECRET_FLAGS or any(h in bare for h in _SECRET_KEY_HINTS)):
            out.append(f"{key_part}=<redacted>")
            continue
        if not sep and bare in _SECRET_FLAGS:
            out.append(arg)  # the flag itself is harmless; its value is the next entry
            redact_next = True
            continue
        out.append(arg)
    return out


def capture_requester(
    *, kind: str = DEFAULT_STOP_KIND,
    reason_code: Optional[str] = None,
    reason_text: Optional[str] = None,
    lane: Optional[str] = None,
    lull_gate: str = DEFAULT_LULL_GATE,
    argv: Optional[list[str]] = None,
) -> dict[str, Any]:
    """The requester identity of THIS process: who is asking the gateway to stop, and why."""
    return {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "argv": redact_argv(argv if argv is not None else list(sys.argv)),
        "kind": normalize_kind(kind),
        "reason_code": reason_code,
        "reason_text": (str(reason_text)[:400] if reason_text is not None else None),
        "lane": lane,
        "lull_gate": normalize_lull_gate(lull_gate),
    }


def normalize_requester(
    requester: Optional[dict[str, Any]], *, kind: str = DEFAULT_STOP_KIND,
    reason_code: Optional[str] = None, reason_text: Optional[str] = None,
    lane: Optional[str] = None, lull_gate: str = DEFAULT_LULL_GATE,
) -> dict[str, Any]:
    """Fill a caller-supplied requester from this process without discarding what it states.

    A supplied object keeps its own pid/ppid/argv (a door may report a helper it spawned, or the
    INCOMING ``--replace`` process); only fields it leaves out are captured here.
    """
    captured = capture_requester(kind=kind, reason_code=reason_code, reason_text=reason_text,
                                 lane=lane, lull_gate=lull_gate)
    if not isinstance(requester, dict) or not requester:
        return captured
    merged = dict(captured)
    for key, value in requester.items():
        if value is not None:
            merged[key] = value
    merged["argv"] = redact_argv(merged.get("argv"))
    merged["kind"] = normalize_kind(merged.get("kind"))
    merged["lull_gate"] = normalize_lull_gate(merged.get("lull_gate"))
    return merged


# ── the single writer ────────────────────────────────────────────────────────

def append_record(record: dict[str, Any], home: Optional[Path] = None) -> bool:
    """Append one JSON line to ``logs/gateway-exit-diag.log``; False (never raise) on failure.

    Append-only, one line per record: the file is grepped by operators and by flow verifiers, so
    a write that cannot land as exactly one line must not land at all.
    """
    try:
        if os.environ.get("HERMES_GATEWAY_EXIT_DIAG", "1") != "1":
            return False
        path = exit_diag_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception:
        return False


def target_identity(target_pid: Optional[int], home: Optional[Path] = None) -> dict[str, Any]:
    """``{pid, start_time, home}`` for the process being stopped (start_time may be None)."""
    start_time = None
    if target_pid is not None:
        with contextlib.suppress(Exception):
            from gateway.status import get_process_start_time

            start_time = get_process_start_time(int(target_pid))
    return {"pid": target_pid, "start_time": start_time,
            "home": str(home if home is not None else _process_home())}


def write_stop_request(
    target_pid: int,
    *,
    kind: str = DEFAULT_STOP_KIND,
    reason_code: Optional[str] = None,
    reason_text: Optional[str] = None,
    lane: Optional[str] = None,
    lull_gate: str = DEFAULT_LULL_GATE,
    delivery: str = "marker",
    requester: Optional[dict[str, Any]] = None,
    write_marker: bool = True,
    home: Optional[Path] = None,
    marker_home: Optional[Path] = None,
) -> str:
    """Record ONE stop request and (by default) deliver it as a planned-stop marker.

    The single door every stopper calls BEFORE delivering anything. Returns the ``stop_id`` that
    threads this stop's rows, so a caller that also signals the target can quote it and a reader
    can join the request to the gateway's ``stop_seen`` / ``forced_exit`` / ``previous_exit``.

    Order matters: the row is written first, so a gateway killed a microsecond later is still
    attributable. Never raises.
    """
    stop_id = new_stop_id()
    try:
        req = normalize_requester(requester, kind=kind, reason_code=reason_code,
                                  reason_text=reason_text, lane=lane, lull_gate=lull_gate)
        method = str(delivery or "").strip().lower() or "unknown"
        append_record({
            "ts": _now_iso(),
            "tag": STOP_REQUESTED,
            "stop_id": stop_id,
            "target": target_identity(target_pid, home),
            "delivery": {"method": method if method in DELIVERY_METHODS else "unknown",
                         "raw": None if method in DELIVERY_METHODS else method},
            "requester": req,
        }, home)
        if write_marker:
            if marker_home is None:
                from gateway.status import write_planned_stop_marker

                write_planned_stop_marker(int(target_pid), requester=req, stop_id=stop_id)
            else:
                # Cross-home stop (another profile's gateway, or an owner under another home):
                # the marker must land where THAT gateway reads it.
                from gateway.status import write_planned_stop_marker_in

                write_planned_stop_marker_in(marker_home, int(target_pid), requester=req,
                                             stop_id=stop_id)
    except Exception:
        pass
    return stop_id


# ── correlation: the gateway's side of the same stop ─────────────────────────

def _tail_records(home: Optional[Path] = None,
                  path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Parse the last ``_TAIL_BYTES`` of the exit-diag log, newest last. Never raises."""
    target = path if path is not None else exit_diag_path(home)
    try:
        with target.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - _TAIL_BYTES)
            fh.seek(start)
            chunk = fh.read()
    except OSError:
        return []
    lines = chunk.splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # the first line may be a partial record
    records: list[dict[str, Any]] = []
    for raw in lines[-_TAIL_MAX_RECORDS:]:
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def find_stop_request(
    target_pid: Optional[int], *, home: Optional[Path] = None, since_ts: Optional[float] = None,
    max_age_s: float = DEFAULT_REQUEST_MAX_AGE_S, path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Newest unconsumed ``gateway.stop_requested`` row naming ``target_pid``, or None.

    This is how a SIGNAL stop learns its ``stop_id``: a signal carries no payload, so the
    requester's row is the only in-band-free way to correlate. A request whose ``stop_id``
    already appears on a ``gateway.stop_seen`` row is spent, so a re-issued stop cannot be
    attributed to an older request.
    """
    if target_pid is None:
        return None
    records = _tail_records(home, path)
    spent = {rec.get("stop_id") for rec in records if rec.get("tag") == STOP_SEEN}
    now = time.time()
    for record in reversed(records):
        if record.get("tag") != STOP_REQUESTED:
            continue
        if (record.get("target") or {}).get("pid") != target_pid:
            continue
        stop_id = record.get("stop_id")
        if not stop_id or stop_id in spent:
            continue
        issued = _parse_iso(record.get("ts"))
        if issued is not None:
            if max_age_s and (now - issued) > max_age_s:
                continue
            if since_ts is not None and issued < since_ts:
                continue
        return record
    return None


def _unknown_requester(*, reason_code: Optional[str] = None) -> dict[str, Any]:
    """A requester we could NOT identify — honest, never blank, never invented."""
    return capture_requester(kind=DEFAULT_STOP_KIND, reason_code=reason_code,
                            reason_text="no stop request record matched this stop",
                            lane=None, lull_gate="not_consulted")


def _signal_name(sig: Any) -> str:
    """Human-readable signal name (``signal#N`` for a number without a known name)."""
    import signal as _signal

    names = {int(getattr(_signal, name)): name for name in
             ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2")
             if getattr(_signal, name, None) is not None}
    try:
        return names.get(int(sig), f"signal#{int(sig)}")
    except (TypeError, ValueError):
        return str(sig)


def announce_stop_seen(
    planned_stop_request: Optional[dict[str, Any]] = None, *,
    received_signal: Any = None, home: Optional[Path] = None,
) -> dict[str, Any]:
    """The gateway ACCEPTED a stop: append ``gateway.stop_seen`` and thread the ``stop_id`` into
    the lifecycle sentinel so the forced-exit and next-boot rows carry the same id.

    ``planned_stop_request`` is the consumed marker (``None`` for a signal-only stop). Returns
    ``{stop_id, requester, phase, signal}``; never raises.
    """
    marker = planned_stop_request if isinstance(planned_stop_request, dict) else None
    requested: Optional[dict[str, Any]] = None
    if marker is None:
        with contextlib.suppress(Exception):
            requested = find_stop_request(os.getpid(), home=home)
    source = marker or requested or {}

    stop_id = source.get("stop_id") if isinstance(source.get("stop_id"), str) else None
    if not stop_id:
        stop_id = new_stop_id()
    requester = source.get("requester")
    if not isinstance(requester, dict) or not requester:
        requester = _unknown_requester(
            reason_code="signal" if received_signal is not None else "unattributed_stop")
    requester = normalize_requester(requester)
    phase = "marker" if marker is not None else (
        f"signal:{_signal_name(received_signal)}" if received_signal is not None else "handler")
    result = {
        "stop_id": stop_id,
        "requester": requester,
        "phase": phase,
        # ``marker`` only when a marker really was consumed: a bare handler call is not evidence of one.
        "signal": (_signal_name(received_signal) if received_signal is not None
                   else ("marker" if marker is not None else "unknown")),
    }
    with contextlib.suppress(Exception):
        append_record({
            "ts": _now_iso(),
            "tag": STOP_SEEN,
            "stop_id": stop_id,
            "target": target_identity(os.getpid(), home),
            "requester": requester,
            "phase": phase,
        }, home)
    with contextlib.suppress(Exception):
        from gateway.lifecycle_ledger import record_stop_seen

        record_stop_seen(stop_id, requester=requester, home=home)
    return result


def record_forced_exit(
    exit_code: Optional[int], reason: str, *, stop_id: Optional[str] = None,
    active_agents: Optional[int] = None, active_cron_jobs: Optional[int] = None,
    home: Optional[Path] = None,
) -> None:
    """Append ``gateway.forced_exit`` for a watchdog hard exit. Never raises."""
    try:
        if not stop_id:
            with contextlib.suppress(Exception):
                from gateway.lifecycle_ledger import read_sentinel_stop_id

                stop_id = read_sentinel_stop_id(home=home)
        append_record({
            "ts": _now_iso(),
            "tag": FORCED_EXIT,
            "stop_id": stop_id,
            "pid": os.getpid(),
            "exit_code": exit_code,
            "reason": reason,
            "active_agents": active_agents,
            "active_cron_jobs": active_cron_jobs,
        }, home)
    except Exception:
        pass


def record_previous_exit(
    sentinel: Optional[dict[str, Any]], *, home: Optional[Path] = None,
    prior_stop_id: Optional[str] = None,
) -> None:
    """Append ``gateway.previous_exit`` for the life described by ``sentinel``. Never raises.

    EVERY prior life is reported — clean, forced and unclean — because the operator's question
    ("who stopped the gateway, and why?") does not change with the exit code, and today a clean
    or watchdog-forced life leaves no record at all.
    """
    try:
        prior = sentinel if isinstance(sentinel, dict) else {}
        if not prior:
            return
        phase = prior.get("phase")
        exit_reason = prior.get("exit_reason")
        if exit_reason is None:
            exit_reason = "unclean (no exit path ran)" if phase == "running" else None
        append_record({
            "ts": _now_iso(),
            "tag": PREVIOUS_EXIT,
            "stop_id": prior.get("stop_id") or prior_stop_id,
            "pid": prior.get("pid"),
            "started_at": prior.get("started_at"),
            "exited_at": prior.get("exited_at"),
            "exit_code": prior.get("exit_code"),
            "exit_reason": exit_reason,
            "prior_phase": phase,
            "recorder_pid": os.getpid(),
        }, home)
    except Exception:
        pass
